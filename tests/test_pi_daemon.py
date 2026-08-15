# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import sys
import time
from http.server import BaseHTTPRequestHandler
from threading import Event, Thread
from typing import Any, cast

import httpx

from local_first_agent_os import pi_command
from local_first_agent_os.pi_daemon import (
    PiDaemonClient,
    _PiHTTPServer,
    build_pi_daemon_http_handler,
)
from local_first_agent_os.progress_events import emit_progress


class _Settings:
    def __init__(self, base_url: str, idle_timeout: float | None = None):
        self.pi_daemon_base_url = base_url
        if idle_timeout is not None:
            self.pi_stream_idle_timeout_seconds = idle_timeout


class _SilentHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.send_response(200)
        self.send_header("content-type", "application/x-ndjson")
        self.end_headers()
        time.sleep(1)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def test_pi_daemon_client_streams_query_events() -> None:
    captured: dict[str, Any] = {}

    def runner(text, **kwargs):
        kwargs["text"] = text
        captured.update(kwargs)
        yield "hello"
        yield {"status": "completed", "artifacts": []}

    handler = build_pi_daemon_http_handler(
        runner,
        lambda result: f"rendered:{result['status']}",
        result_payload_resolver=lambda result: {
            "action": "test",
            "status": result["status"],
        },
    )
    server = _PiHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = cast(tuple[str, int], server.server_address)
        client = PiDaemonClient(_Settings(f"http://{host}:{port}"))  # type: ignore[arg-type]
        events = list(
            client.stream_query(
                text="hello",
                workspace_id="general",
                session_id="shell-test",
                context=None,
                max_window_tokens=None,
                streaming=True,
            )
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert captured["text"] == "hello"
    assert captured["workspace_id"] == "general"
    assert captured["shell_session_id"] == "shell-test"
    assert events == [
        {"type": "delta", "text": "hello"},
        {
            "type": "result",
            "result": {"status": "completed", "artifacts": []},
            "rendered": "rendered:completed",
            "directive_payload": {"action": "test", "status": "completed"},
        },
        {"type": "done"},
    ]


def test_pi_daemon_projects_in_process_progress_events() -> None:
    def runner(_text, **_kwargs):
        emit_progress(
            "starting staff turn: review_change",
            phase="task_started",
            task_name="review_change",
            tier="staff",
        )
        yield {"status": "completed", "artifacts": []}

    handler = build_pi_daemon_http_handler(runner, lambda result: str(result))
    server = _PiHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = cast(tuple[str, int], server.server_address)
        events = list(
            PiDaemonClient(_Settings(f"http://{host}:{port}")).stream_query(  # type: ignore[arg-type]
                text="/dispatch",
                workspace_id="general",
                session_id="progress-test",
                context=None,
                max_window_tokens=None,
                streaming=False,
            )
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    projected = next(event for event in events if event.get("phase") == "task_started")
    assert projected["message"] == "starting staff turn: review_change"
    assert projected["task_name"] == "review_change"
    assert projected["tier"] == "staff"


def test_pi_daemon_streams_status_while_dispatch_is_blocked() -> None:
    release = Event()

    def runner(_text, **_kwargs):
        assert release.wait(timeout=2)
        yield {"status": "completed", "artifacts": []}

    handler = build_pi_daemon_http_handler(
        runner,
        lambda result: str(result),
        stream_heartbeat_seconds=0.02,
    )
    server = _PiHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    client = PiDaemonClient(_Settings(f"http://{host}:{port}"))  # type: ignore[arg-type]
    events: list[dict[str, Any]] = []

    def query() -> None:
        events.extend(
            client.stream_query(
                text="/dispatch",
                workspace_id="general",
                session_id="test-status",
                context=None,
                max_window_tokens=None,
                streaming=False,
            )
        )

    query_thread = Thread(target=query, daemon=True)
    query_thread.start()
    try:
        for _ in range(50):
            if len(events) >= 2:
                break
            Event().wait(0.01)
        assert events[0]["type"] == "status"
        assert any("still running" in event.get("message", "") for event in events)
    finally:
        release.set()
        query_thread.join(timeout=2)
        server.shutdown()
        thread.join(timeout=2)


def test_pi_client_fails_when_daemon_stream_is_silent() -> None:
    server = _PiHTTPServer(("127.0.0.1", 0), _SilentHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    settings = cast(Any, _Settings(f"http://{host}:{port}", idle_timeout=0.05))
    client = PiDaemonClient(settings)
    started = time.monotonic()
    try:
        try:
            list(
                client.stream_query(
                    text="/dispatch",
                    workspace_id="general",
                    session_id="test-silent",
                    context=None,
                    max_window_tokens=None,
                    streaming=False,
                )
            )
        except Exception as exc:
            assert "produced no event" in str(exc)
        else:
            raise AssertionError("silent Pi daemon stream did not time out")
        assert time.monotonic() - started < 0.5
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_pi_client_renders_status_to_stdout(capsys) -> None:
    code = pi_command._render_daemon_events(
        iter([{"type": "status", "message": "dispatch active"}, {"type": "done"}]),
        json_output=False,
    )
    assert code == 0
    assert capsys.readouterr().out == "[pi] dispatch active\n"


def test_pi_daemon_health_exposes_runtime_backend() -> None:
    handler = build_pi_daemon_http_handler(
        lambda *_args, **_kwargs: iter(()),
        lambda result: str(result),
        health_payload={"coordination_backend": "postgres", "runtime_revision": "test"},
    )
    server = _PiHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = cast(tuple[str, int], server.server_address)
        response = httpx.get(f"http://{host}:{port}/health")
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert response.json() == {
        "status": "ok",
        "coordination_backend": "postgres",
        "runtime_revision": "test",
    }


def test_pi_daemon_exposes_prometheus_metrics() -> None:
    handler = build_pi_daemon_http_handler(
        lambda *_args, **_kwargs: iter(()),
        lambda result: str(result),
    )
    server = _PiHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = cast(tuple[str, int], server.server_address)
        response = httpx.get(f"http://{host}:{port}/metrics", timeout=1)
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# HELP" in response.text


def test_inspection_directives_bypass_long_running_dispatch_lock() -> None:
    dispatch_started = Event()
    release_dispatch = Event()

    def runner(text, **_kwargs):
        if text == "/dispatch":
            dispatch_started.set()
            assert release_dispatch.wait(timeout=3)
        yield {"status": "completed", "artifacts": []}

    handler = build_pi_daemon_http_handler(runner, lambda result: str(result))
    server = _PiHTTPServer(("127.0.0.1", 0), handler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    settings = _Settings(f"http://{host}:{port}")

    def query(text: str, finished: Event | None = None) -> None:
        list(
            PiDaemonClient(settings).stream_query(  # type: ignore[arg-type]
                text=text,
                workspace_id="general",
                session_id=f"test:{text}",
                context=None,
                max_window_tokens=None,
                streaming=False,
            )
        )
        if finished is not None:
            finished.set()

    dispatch_thread = Thread(target=query, args=("/dispatch",), daemon=True)
    dispatch_thread.start()
    assert dispatch_started.wait(timeout=1)
    inspection_threads: list[Thread] = []
    try:
        for command in ("/ledger", "/read /ledger --saga-id saga-1", "/review-merge"):
            inspection_finished = Event()
            thread = Thread(
                target=query,
                args=(command, inspection_finished),
                daemon=True,
            )
            inspection_threads.append(thread)
            thread.start()
            assert inspection_finished.wait(timeout=1)
            assert dispatch_thread.is_alive()
    finally:
        release_dispatch.set()
        dispatch_thread.join(timeout=2)
        for thread in inspection_threads:
            thread.join(timeout=2)
        server.shutdown()
        server_thread.join(timeout=2)


def test_chained_or_mutating_queries_remain_serialized_behind_dispatch() -> None:
    dispatch_started = Event()
    release_dispatch = Event()

    def runner(text, **_kwargs):
        if text == "/dispatch":
            dispatch_started.set()
            assert release_dispatch.wait(timeout=3)
        yield {"status": "completed", "artifacts": []}

    handler = build_pi_daemon_http_handler(runner, lambda result: str(result))
    server = _PiHTTPServer(("127.0.0.1", 0), handler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    settings = _Settings(f"http://{host}:{port}")

    def query(text: str, finished: Event | None = None) -> None:
        list(
            PiDaemonClient(settings).stream_query(  # type: ignore[arg-type]
                text=text,
                workspace_id="general",
                session_id=f"test:{text}",
                context=None,
                max_window_tokens=None,
                streaming=False,
            )
        )
        if finished is not None:
            finished.set()

    dispatch_thread = Thread(target=query, args=("/dispatch",), daemon=True)
    dispatch_thread.start()
    assert dispatch_started.wait(timeout=1)
    blocked_threads: list[Thread] = []
    blocked_events: list[Event] = []
    try:
        for command in (
            "plain model query",
            "/approve-merge approval-1",
            "/ledger && /approve-merge approval-1",
            "/ledger /approve-merge approval-1",
        ):
            finished = Event()
            thread = Thread(target=query, args=(command, finished), daemon=True)
            blocked_threads.append(thread)
            blocked_events.append(finished)
            thread.start()
        assert not any(finished.wait(timeout=0.1) for finished in blocked_events)
    finally:
        release_dispatch.set()
        dispatch_thread.join(timeout=2)
        for thread in blocked_threads:
            thread.join(timeout=2)
        server.shutdown()
        server_thread.join(timeout=2)
    assert all(finished.is_set() for finished in blocked_events)


def test_pi_client_suppresses_duplicate_streamed_answer(capsys) -> None:
    code = pi_command._render_daemon_events(
        iter(
            [
                {"type": "delta", "text": "answer"},
                {
                    "type": "result",
                    "result": {"artifacts": [{"role": "answer"}]},
                    "rendered": "answer",
                },
                {"type": "done"},
            ]
        ),
        json_output=False,
    )
    assert code == 0
    assert capsys.readouterr().out == "answer\n"


def test_pi_client_preserves_error_exit_code(capsys) -> None:
    code = pi_command._render_daemon_events(
        iter([{"type": "error", "error": "model not loaded", "exit_code": 2}]),
        json_output=False,
    )
    assert code == 2
    assert "model not loaded" in capsys.readouterr().err


def test_pi_client_defaults_error_exit_code_to_one(capsys) -> None:
    code = pi_command._render_daemon_events(
        iter([{"type": "error", "error": "boom"}]),
        json_output=False,
    )
    assert code == 1


def test_pi_client_renders_nonstreamed_answer(capsys) -> None:
    code = pi_command._render_daemon_events(
        iter(
            [
                {
                    "type": "result",
                    "result": {"artifacts": [{"role": "answer"}]},
                    "rendered": "answer",
                },
                {"type": "done"},
            ]
        ),
        json_output=False,
    )
    assert code == 0
    assert capsys.readouterr().out == "answer\n"


def test_asr_directive_requires_foreground_terminal() -> None:
    assert pi_command._requires_foreground_terminal("/start /asr")
    assert pi_command._requires_foreground_terminal("/start /audio")
    assert pi_command._requires_foreground_terminal("/start /ocr /start /asr")
    assert not pi_command._requires_foreground_terminal("/start /dispatcher")
    assert not pi_command._requires_foreground_terminal("/start /ocr")
    assert pi_command._requires_foreground_terminal("/ocr /absolute/image.png")
    assert not pi_command._requires_foreground_terminal("/start /hard-ocr")
    assert pi_command._requires_foreground_terminal("/hard-ocr /absolute/image.png")
    assert pi_command._is_ocr_capture_command("/ocr /absolute/image.png")
    assert pi_command._is_ocr_capture_command("/hard-ocr /absolute/image.png")
    assert not pi_command._is_ocr_capture_command("/start /ocr")
    assert not pi_command._is_ocr_capture_command("/start /hard-ocr")


def test_walkthru_directive_is_detected_for_foreground_interview() -> None:
    assert pi_command._is_walkthru_command("/start /new-project --walkthru")
    assert pi_command._is_walkthru_command(
        "/start /new-project --walkthru gawd-walkthru-123456abcdef --status"
    )
    # The bare form is the walkthru now, so the interview has to pick it up.
    assert pi_command._is_walkthru_command("/start /new-project")
    assert not pi_command._is_walkthru_command("/start /new-project --no-walkthru")
    assert not pi_command._is_walkthru_command("/start /new-project /tmp/draft.txt")


def test_walkthru_driver_waits_for_answer_and_review_before_advancing(capsys) -> None:
    sent: list[str] = []
    replies = iter(["My project", "", "/pause"])
    states = iter(
        [
            {
                "state": "awaiting_review",
                "walkthru_id": "gawd-walkthru-123456abcdef",
            },
            {
                "state": "awaiting_answer",
                "walkthru_id": "gawd-walkthru-123456abcdef",
            },
        ]
    )

    def send(command: str):
        sent.append(command)
        return 0, next(states)

    code = pi_command._run_walkthru_until_terminal_state(
        {
            "state": "awaiting_answer",
            "walkthru_id": "gawd-walkthru-123456abcdef",
        },
        send=send,
        read_operator_input=lambda _prompt: next(replies),
    )

    assert code == 0
    assert sent == [
        "/start /new-project --walkthru gawd-walkthru-123456abcdef --answer 'My project'",
        "/start /new-project --walkthru gawd-walkthru-123456abcdef --accept",
    ]
    assert "is saved" in capsys.readouterr().out


def test_walkthru_driver_finishes_after_final_review() -> None:
    sent: list[str] = []

    def send(command: str):
        sent.append(command)
        return 0, {
            "state": "finished",
            "walkthru_id": "gawd-walkthru-123456abcdef",
        }

    code = pi_command._run_walkthru_until_terminal_state(
        {
            "state": "ready_to_finish",
            "walkthru_id": "gawd-walkthru-123456abcdef",
        },
        send=send,
        read_operator_input=lambda _prompt: "",
    )

    assert code == 0
    assert sent == ["/start /new-project --walkthru gawd-walkthru-123456abcdef --finish"]


def test_walkthru_driver_retries_a_durably_saved_pending_summary() -> None:
    sent: list[str] = []

    def send(command: str):
        sent.append(command)
        return 0, {
            "state": "awaiting_review",
            "walkthru_id": "gawd-walkthru-123456abcdef",
        }

    code = pi_command._run_walkthru_until_terminal_state(
        {
            "state": "awaiting_summary",
            "walkthru_id": "gawd-walkthru-123456abcdef",
            "pending_answer": {"verbatim": "Four hours total"},
        },
        send=send,
        read_operator_input=lambda _prompt: "p",
    )

    assert code == 0
    assert sent == [
        "/start /new-project --walkthru gawd-walkthru-123456abcdef --answer 'Four hours total'"
    ]


def test_pi_client_forwards_directive_specific_flags(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run_daemon_query(text, **kwargs):
        captured["text"] = text
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pi",
            "/saga",
            "--executor",
            "cli",
            "--worktree-root",
            "/tmp/work trees",
            "build",
        ],
    )
    monkeypatch.setattr(pi_command, "_run_daemon_query", fake_run_daemon_query)

    pi_command.main()

    assert captured["text"] == "/saga --executor cli --worktree-root '/tmp/work trees' build"


def test_pi_main_keeps_walkthru_in_one_foreground_process(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class FakeStdin:
        @staticmethod
        def isatty() -> bool:
            return True

    def fake_interview(text, **kwargs):
        captured["text"] = text
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(sys, "stdin", FakeStdin())
    monkeypatch.setattr(
        sys,
        "argv",
        ["pi", "/start", "/new-project", "--walkthru"],
    )
    monkeypatch.setattr(pi_command, "_run_walkthru_interview", fake_interview)

    pi_command.main()

    assert captured["text"] == "/start /new-project --walkthru"


def test_asr_directive_bypasses_daemon(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        pi_command,
        "_run_direct_query",
        lambda text, **_kwargs: calls.append(text) or 0,
    )

    code = pi_command._run_daemon_query(
        "/start /asr",
        workspace_id="general",
        context_file=None,
        max_window_tokens=None,
        session_id=None,
        json_output=False,
        streaming=True,
    )

    assert code == 0
    assert calls == ["/start /asr"]


def test_ocr_capture_bypasses_daemon_and_announces_foreground(
    monkeypatch,
    capsys,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        pi_command,
        "_run_direct_query",
        lambda text, **_kwargs: calls.append(text) or 0,
    )

    code = pi_command._run_daemon_query(
        "/ocr /absolute/image.png",
        workspace_id="general",
        context_file=None,
        max_window_tokens=None,
        session_id=None,
        json_output=False,
        streaming=True,
    )

    assert code == 0
    assert calls == ["/ocr /absolute/image.png"]
    assert "OCR is running in the foreground" in capsys.readouterr().out


def test_hard_ocr_capture_bypasses_daemon_and_announces_foreground(
    monkeypatch,
    capsys,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        pi_command,
        "_run_direct_query",
        lambda text, **_kwargs: calls.append(text) or 0,
    )

    code = pi_command._run_daemon_query(
        "/hard-ocr /absolute/image.png",
        workspace_id="general",
        context_file=None,
        max_window_tokens=None,
        session_id=None,
        json_output=False,
        streaming=True,
    )

    assert code == 0
    assert calls == ["/hard-ocr /absolute/image.png"]
    assert "OCR is running in the foreground" in capsys.readouterr().out
