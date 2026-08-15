# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import shlex
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from local_first_agent_os.browser_acceptance import (
    BrowserAcceptanceRequest,
    BrowserAcceptanceRunner,
    BrowserAcceptanceStatus,
    BrowserViewport,
    LocalPreviewSession,
    PreviewStartError,
)


def _server_command() -> str:
    return f"{shlex.quote(sys.executable)} -m http.server {{port}} --bind 127.0.0.1"


def _write_page(root: Path, body: str, name: str = "index.html") -> None:
    (root / name).write_text(
        f"<!doctype html><html><head><title>Fixture</title></head><body>{body}</body></html>",
        encoding="utf-8",
    )


def _request(
    target_url: str,
    *,
    paths: tuple[str, ...] = ("/",),
    viewports: tuple[BrowserViewport, ...] | None = None,
    timeout_seconds: int = 10,
):
    return BrowserAcceptanceRequest(
        target_url=target_url,
        viewports=viewports
        or (
            BrowserViewport(name="mobile", width=375, height=812),
            BrowserViewport(name="desktop", width=1440, height=1000),
        ),
        required_paths=paths,
        allowed_hosts=("127.0.0.1",),
        required_selectors=("main", "#logo"),
        bounded_selectors=("#logo",),
        timeout_seconds=timeout_seconds,
    )


def test_complete_local_preview_persists_every_capture(runtime, tmp_path) -> None:
    _write_page(
        tmp_path,
        "<header><div id='logo'>Fixture logo</div></header><main>Ready</main>",
    )
    _write_page(
        tmp_path,
        "<header><div id='logo'>Fixture logo</div></header><main>Contact</main>",
        "contact.html",
    )
    session = LocalPreviewSession(command_template=_server_command(), cwd=tmp_path)
    with session:
        run = BrowserAcceptanceRunner(runtime.artifact_store).run(
            _request(session.target_url, paths=("/", "/contact.html")),
            workflow_id="browser-success",
        )
    preview = session.build_process_evidence()

    assert run.evidence.status is BrowserAcceptanceStatus.PASSED
    assert len(run.evidence.captures) == 4
    assert all(capture.screenshot_artifact_id for capture in run.evidence.captures)
    assert all(capture.trace_artifact_id for capture in run.evidence.captures)
    assert all(
        runtime.repository.get_artifact(capture.trace_artifact_id or "") is not None
        for capture in run.evidence.captures
    )
    assert runtime.repository.get_artifact(run.request_artifact_id) is not None
    assert runtime.repository.get_artifact(run.evidence_artifact_id) is not None
    assert preview.direct_child_reaped is True
    assert preview.process_group_reaped is True


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "<style>body{width:1200px}</style><div id='logo'>Logo</div><main>Wide</main>",
            "overflow",
        ),
        (
            "<header style='height:20px;overflow:hidden'><div id='logo' "
            "style='height:100px'>Logo</div></header><main>Clipped</main>",
            "clipped",
        ),
        (
            "<div id='logo'>Logo</div><main>Console</main>"
            "<script>console.error('fixture boom')</script>",
            "console",
        ),
    ],
)
def test_visual_and_console_failures_are_terminal(
    runtime, tmp_path, body: str, expected: str
) -> None:
    _write_page(tmp_path, body)
    session = LocalPreviewSession(command_template=_server_command(), cwd=tmp_path)
    with session:
        run = BrowserAcceptanceRunner(runtime.artifact_store).run(
            _request(session.target_url),
            workflow_id=f"browser-{expected}",
        )

    assert run.evidence.status is BrowserAcceptanceStatus.FAILED
    payload = run.evidence.model_dump_json()
    assert expected in payload


def test_failed_http_resource_is_recorded(runtime, tmp_path) -> None:
    _write_page(
        tmp_path,
        "<div id='logo'>Logo</div><main><img src='/missing.png'></main>",
    )
    session = LocalPreviewSession(command_template=_server_command(), cwd=tmp_path)
    with session:
        run = BrowserAcceptanceRunner(runtime.artifact_store).run(
            _request(session.target_url),
            workflow_id="browser-network",
        )

    assert run.evidence.status is BrowserAcceptanceStatus.FAILED
    assert any(capture.failed_requests for capture in run.evidence.captures)


def test_uncaught_page_exception_is_terminal(runtime, tmp_path) -> None:
    _write_page(
        tmp_path,
        "<div id='logo'>Logo</div><main>Hydration</main>"
        "<script>setTimeout(() => { throw new Error('hydration exploded') }, 0)</script>",
    )
    session = LocalPreviewSession(command_template=_server_command(), cwd=tmp_path)
    with session:
        request = _request(session.target_url).model_copy(update={"capture_console": False})
        run = BrowserAcceptanceRunner(runtime.artifact_store).run(
            request,
            workflow_id="browser-pageerror",
        )

    assert run.evidence.status is BrowserAcceptanceStatus.FAILED
    assert any("hydration exploded" in value for value in run.evidence.captures[0].page_errors)


def test_redirect_outside_allowlist_is_terminal(runtime, tmp_path) -> None:
    _write_page(
        tmp_path,
        "<div id='logo'>Logo</div><main>Redirecting</main>"
        "<script>window.location.replace('http://example.com/outside')</script>",
    )
    session = LocalPreviewSession(command_template=_server_command(), cwd=tmp_path)
    with session:
        run = BrowserAcceptanceRunner(runtime.artifact_store).run(
            _request(session.target_url),
            workflow_id="browser-redirect-blocked",
        )

    assert run.evidence.status is BrowserAcceptanceStatus.FAILED
    assert "blocked navigation outside allowlist" in run.evidence.model_dump_json()


def test_capture_timeout_is_terminal(runtime, tmp_path) -> None:
    server = tmp_path / "slow_server.py"
    server.write_text(
        """from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
import time

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/slow.js':
            time.sleep(8)
            payload = b''
            content_type = 'text/javascript'
        else:
            payload = (
                b\"<div id='logo'>Logo</div><main>Slow</main>\"
                b\"<script src='/slow.js'></script>\"
            )
            content_type = 'text/html'
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass

ThreadingHTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
""",
        encoding="utf-8",
    )
    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(server))} {{port}}"
    session = LocalPreviewSession(command_template=command, cwd=tmp_path)
    with session:
        run = BrowserAcceptanceRunner(runtime.artifact_store).run(
            _request(
                session.target_url,
                viewports=(BrowserViewport(name="mobile", width=375, height=812),),
                timeout_seconds=5,
            ),
            workflow_id="browser-timeout",
        )

    assert run.evidence.status is BrowserAcceptanceStatus.FAILED
    assert "Timeout" in run.evidence.model_dump_json()


def test_cancellation_stops_before_remaining_captures(runtime, tmp_path, monkeypatch) -> None:
    _write_page(tmp_path, "<div id='logo'>Logo</div><main>Ready</main>")
    _write_page(
        tmp_path,
        "<div id='logo'>Logo</div><main>Contact</main>",
        "contact.html",
    )
    cancel_event = threading.Event()
    runner = BrowserAcceptanceRunner(runtime.artifact_store)
    capture = runner._capture_page_evidence

    def capture_then_cancel(**kwargs: Any):
        evidence = capture(**kwargs)
        cancel_event.set()
        return evidence

    monkeypatch.setattr(runner, "_capture_page_evidence", capture_then_cancel)
    session = LocalPreviewSession(command_template=_server_command(), cwd=tmp_path)
    with session:
        run = runner.run(
            _request(session.target_url, paths=("/", "/contact.html")),
            workflow_id="browser-cancelled",
            cancel_event=cancel_event,
        )

    assert run.evidence.status is BrowserAcceptanceStatus.CANCELLED
    assert len(run.evidence.captures) == 1
    assert run.evidence.captures[0].trace_artifact_id is not None


def test_trace_persistence_failure_cannot_pass(runtime, tmp_path) -> None:
    _write_page(tmp_path, "<div id='logo'>Logo</div><main>Ready</main>")

    class _FailingTraceWriter:
        def write_json(self, **kwargs: Any):
            return runtime.artifact_store.write_json(**kwargs)

        def write_bytes(self, **kwargs: Any):
            if kwargs["role"] == "browser_trace":
                raise OSError("fixture trace persistence failure")
            return runtime.artifact_store.write_bytes(**kwargs)

    session = LocalPreviewSession(command_template=_server_command(), cwd=tmp_path)
    with session:
        run = BrowserAcceptanceRunner(_FailingTraceWriter()).run(
            _request(session.target_url),
            workflow_id="browser-trace-failure",
        )

    assert run.evidence.status is BrowserAcceptanceStatus.FAILED
    assert all(capture.screenshot_artifact_id for capture in run.evidence.captures)
    assert all(capture.trace_artifact_id is None for capture in run.evidence.captures)
    assert "trace persistence failed" in run.evidence.model_dump_json()


def test_screenshot_persistence_failure_cannot_pass(runtime, tmp_path) -> None:
    _write_page(tmp_path, "<div id='logo'>Logo</div><main>Ready</main>")

    class _FailingScreenshotWriter:
        def write_json(self, **kwargs: Any):
            return runtime.artifact_store.write_json(**kwargs)

        def write_bytes(self, **_kwargs: Any):
            raise OSError("fixture screenshot persistence failure")

    session = LocalPreviewSession(command_template=_server_command(), cwd=tmp_path)
    with session:
        run = BrowserAcceptanceRunner(_FailingScreenshotWriter()).run(
            _request(session.target_url),
            workflow_id="browser-screenshot-failure",
        )

    assert run.evidence.status is BrowserAcceptanceStatus.FAILED
    assert all(capture.screenshot_artifact_id is None for capture in run.evidence.captures)
    assert "screenshot failed" in run.evidence.model_dump_json()


def test_preview_process_death_is_bounded_and_reaped(tmp_path) -> None:
    command = f"{shlex.quote(sys.executable)} -c 'raise SystemExit(7)'"
    session = LocalPreviewSession(
        command_template=command,
        cwd=tmp_path,
        startup_timeout_seconds=2,
    )

    with pytest.raises(PreviewStartError) as raised, session:
        raise AssertionError("unreachable")

    evidence = raised.value.evidence
    assert evidence.ready is False
    assert evidence.returncode == 7
    assert evidence.direct_child_reaped is True
    assert evidence.process_group_reaped is True


def test_preview_environment_rejects_credential_shaped_keys(tmp_path) -> None:
    with pytest.raises(ValueError, match="VERCEL_TOKEN"):
        LocalPreviewSession(
            command_template=_server_command(),
            cwd=tmp_path,
            environment={"VERCEL_TOKEN": "must-not-cross-boundary"},
        )

    session = LocalPreviewSession(
        command_template=_server_command(),
        cwd=tmp_path,
        environment={"BUSINESS_RECORD_ID": "planet-pest-mgmt"},
    )
    assert session.environment == {"BUSINESS_RECORD_ID": "planet-pest-mgmt"}
