# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from local_first_agent_os.chrome_devtools import (
    ChromeControlFailure,
    ChromeControlService,
    ChromeDevToolsError,
    ChromeDevToolsErrorCode,
    redact_chrome_text,
)
from local_first_agent_os.tools import ChromeDevToolsTool

# One fake JSON-RPC child with composable behaviors selected by a
# comma-separated mode list in argv[1]:
#   child    - spawn a descendant process before serving
#   exit     - exit(3) before answering initialize
#   timeout  - never answer initialize
#   stderr   - flood stderr before answering initialize
#   secret   - print a credential-looking line to stderr
#   noise    - emit malformed lines, notifications, and unrelated responses
#   hangcall - answer the first tools/call, hang every later one
#   attacherr - answer tools/call with a tool-level isError attach failure
_FAKE_MCP_SOURCE = """
import json
import subprocess
import sys
import time

MODES = set(sys.argv[1].split(","))
child = None
if "child" in MODES:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
if "exit" in MODES:
    sys.exit(3)


def emit(obj):
    print(json.dumps(obj), flush=True)


calls = 0
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        if "noise" in MODES:
            print("this is not json", flush=True)
            emit({"jsonrpc": "2.0", "method": "notifications/message", "params": {}})
            emit({"jsonrpc": "2.0", "id": 9999, "result": {"unrelated": True}})
        if "timeout" in MODES:
            time.sleep(60)
            continue
        if "stderr" in MODES:
            for index in range(2000):
                print(f"diagnostic-{index}", file=sys.stderr)
            sys.stderr.flush()
        if "secret" in MODES:
            print("password=hunter2 cookie: session=abc123", file=sys.stderr)
            sys.stderr.flush()
        version = str(child.pid) if child is not None else "fake-1"
        emit(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "serverInfo": {"name": "fake-chrome-mcp", "version": version},
                },
            }
        )
    elif method == "tools/call":
        calls += 1
        if "hangcall" in MODES and calls >= 2:
            time.sleep(60)
            continue
        if "attacherr" in MODES:
            emit(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "isError": True,
                        "content": [
                            {
                                "type": "text",
                                "text": "Could not connect to Chrome. password=supersecret",
                            }
                        ],
                    },
                }
            )
            continue
        if "noise" in MODES:
            print("garbage between responses", flush=True)
        name = request["params"]["name"]
        text = (
            "## Pages\\n1: Fake https://example.test [selected]"
            if name == "list_pages"
            else "ok"
        )
        emit(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": {"content": [{"type": "text", "text": text}]},
            }
        )
""".strip()


def _fake_mcp_script(tmp_path: Path, mode: str = "normal") -> Path:
    script = tmp_path / f"fake_chrome_mcp_{mode.replace(',', '_')}.py"
    script.write_text(_FAKE_MCP_SOURCE + "\n", encoding="utf-8")
    return script


def _fake_settings(runtime, script: Path, mode: str = "normal"):
    settings = runtime.settings.model_copy(deep=True)
    settings.chrome_devtools_transport = "mcp"
    settings.chrome_devtools_command = sys.executable
    settings.chrome_devtools_command_args = [str(script), mode]
    settings.chrome_devtools_start_args = []
    settings.chrome_devtools_attach_mode = "launch"
    settings.chrome_devtools_launch_args = []
    settings.chrome_devtools_startup_timeout_seconds = 1.0
    settings.chrome_devtools_attach_timeout_seconds = 1.0
    settings.chrome_devtools_call_timeout_seconds = 1.0
    settings.chrome_devtools_stop_timeout_seconds = 1.0
    settings.chrome_devtools_idle_shutdown_seconds = 0
    return settings


def _service(runtime, tmp_path, mode: str = "normal", **overrides) -> ChromeControlService:
    script = _fake_mcp_script(tmp_path, mode)
    settings = _fake_settings(runtime, script, mode)
    for key, value in overrides.items():
        setattr(settings, key, value)
    return ChromeControlService(settings, mutation_allowed=lambda: True)


def test_service_reuses_one_process_and_reports_v2(runtime, tmp_path) -> None:
    service = _service(runtime, tmp_path)
    try:
        started = service.start_action("wf-start", [])
        first_status = service.supervisor.status()
        service.ensure_ready(explicit=False)
        command = service.call_tool_command(["list_pages"])
        second_status = service.supervisor.status()

        assert started["schema_version"] == "chrome_control_result.v2"
        assert started["v1"]["schema_version"] == "chrome_control_result.v1"
        assert started["v1"]["status"] == "completed"
        assert command["returncode"] == 0
        assert first_status["pid"] == second_status["pid"]
        assert second_status["spawn_count"] == 1
    finally:
        stopped = service.stop_action("wf-stop", [])
    assert stopped["process_cleanup"]["direct_child_reaped"] is True
    assert stopped["process_cleanup"]["process_group_reaped"] is True


def test_initialize_timeout_is_structured_and_reaped(runtime, tmp_path) -> None:
    service = _service(runtime, tmp_path, "timeout", chrome_devtools_startup_timeout_seconds=0.1)

    with pytest.raises(ChromeControlFailure) as raised:
        service.start_action("wf-timeout", [])

    result = raised.value.result
    assert result["schema_version"] == "chrome_control_result.v2"
    assert result["status"] == "failed"
    assert result["error"]["code"] == ChromeDevToolsErrorCode.INITIALIZE_TIMEOUT.value
    assert result["process_cleanup"]["direct_child_reaped"] is True
    assert service.supervisor.status()["state"] == "failed"
    service.close()


def test_child_exit_before_initialize_is_classified(runtime, tmp_path) -> None:
    service = _service(runtime, tmp_path, "exit")

    with pytest.raises(ChromeControlFailure) as raised:
        service.start_action("wf-exit", [])

    result = raised.value.result
    assert result["error"]["code"] == ChromeDevToolsErrorCode.CHILD_EXITED.value
    assert result["status"] == "failed"
    assert result["process_cleanup"]["process_group_reaped"] is True
    service.close()


def test_malformed_output_and_notifications_are_tolerated(runtime, tmp_path) -> None:
    service = _service(runtime, tmp_path, "noise")
    try:
        started = service.start_action("wf-noise", [])
        assert started["status"] == "completed"
        command = service.call_tool_command(["list_pages"])
        assert command["returncode"] == 0
        assert "example.test" in command["stdout"]
    finally:
        service.close()


def test_stderr_flood_does_not_deadlock_initialize(runtime, tmp_path) -> None:
    service = _service(runtime, tmp_path, "stderr")
    try:
        result = service.start_action("wf-stderr", [])
        assert result["status"] == "completed"
        assert "diagnostic-1999" in service.supervisor.build_process_evidence()["stderr_tail"]
    finally:
        service.close()


def test_per_call_timeout_after_initialize_fails_generation(runtime, tmp_path) -> None:
    service = _service(runtime, tmp_path, "hangcall")
    service.start_action("wf-hang", [])

    with pytest.raises(ChromeDevToolsError) as raised:
        service.call_tool_command(["list_pages"])

    assert raised.value.code is ChromeDevToolsErrorCode.TOOL_TIMEOUT
    assert raised.value.cleanup is not None
    assert raised.value.cleanup.process_group_reaped is True
    assert service.supervisor.status()["state"] == "failed"

    with pytest.raises(ChromeDevToolsError) as blocked:
        service.ensure_ready(explicit=False)
    assert blocked.value.status == "blocked"
    assert service.supervisor.spawn_count == 1
    service.close()


def test_concurrent_callers_share_one_generation(runtime, tmp_path) -> None:
    service = _service(runtime, tmp_path)
    errors: list[Exception] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            service.ensure_ready(explicit=False)
            service.call_tool_command(["list_pages"])
        except Exception as exc:  # noqa: BLE001 - collected for assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert service.supervisor.spawn_count == 1
    assert service.supervisor.generation == 1
    service.close()


def test_cancellation_during_initialize(runtime, tmp_path) -> None:
    service = _service(runtime, tmp_path, "timeout", chrome_devtools_startup_timeout_seconds=30.0)
    failures: list[ChromeControlFailure] = []

    def starter() -> None:
        try:
            service.start_action("wf-cancel-init", [])
        except ChromeControlFailure as exc:
            failures.append(exc)

    thread = threading.Thread(target=starter)
    thread.start()
    time.sleep(0.4)
    began = time.monotonic()
    service.close()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert time.monotonic() - began < 10
    assert failures
    assert failures[0].result["error"]["code"] == ChromeDevToolsErrorCode.CANCELLED.value
    assert service.supervisor.status()["state"] in {"failed", "stopped"}


def test_cancellation_during_call_via_stop_action(runtime, tmp_path) -> None:
    service = _service(runtime, tmp_path, "hangcall", chrome_devtools_call_timeout_seconds=30.0)
    service.start_action("wf-cancel-call", [])
    errors: list[ChromeDevToolsError] = []

    def caller() -> None:
        try:
            service.call_tool_command(["list_pages"])
        except ChromeDevToolsError as exc:
            errors.append(exc)

    thread = threading.Thread(target=caller)
    thread.start()
    time.sleep(0.4)
    began = time.monotonic()
    stopped = service.stop_action("wf-stop", [])
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert time.monotonic() - began < 10
    assert stopped["process_cleanup"]["process_group_reaped"] is True
    assert errors
    assert errors[0].code is ChromeDevToolsErrorCode.CANCELLED


def test_stop_reaps_spawned_descendant(runtime, tmp_path) -> None:
    service = _service(runtime, tmp_path, "child")
    service.start_action("wf-child", [])
    child_pid = int(service.supervisor.status()["server_version"])
    result = service.stop_action("wf-stop", [])

    assert result["process_cleanup"]["process_group_reaped"] is True
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_failed_start_reaps_spawned_descendant(runtime, tmp_path) -> None:
    service = _service(
        runtime,
        tmp_path,
        "child,timeout",
        chrome_devtools_startup_timeout_seconds=0.2,
    )

    with pytest.raises(ChromeControlFailure) as raised:
        service.start_action("wf-child-timeout", [])

    assert raised.value.result["process_cleanup"]["process_group_reaped"] is True
    assert service.supervisor.status()["state"] == "failed"
    service.close()


def test_attach_error_response_is_classified_blocked_and_redacted(runtime, tmp_path) -> None:
    service = _service(runtime, tmp_path, "attacherr")

    with pytest.raises(ChromeControlFailure) as raised:
        service.start_action("wf-attacherr", [])

    result = raised.value.result
    assert result["status"] == "blocked"
    assert result["error"]["code"] == ChromeDevToolsErrorCode.BROWSER_ATTACH_FAILED.value
    assert "supersecret" not in result["error"]["message"]
    assert result["process_cleanup"]["process_group_reaped"] is True
    assert service.supervisor.status()["state"] == "failed"
    service.close()


def test_start_status_stop_are_idempotent(runtime, tmp_path) -> None:
    service = _service(runtime, tmp_path)

    before = service.read_chrome_status("wf-status", [])
    assert before["supervisor"]["state"] == "stopped"
    assert before["supervisor"]["spawn_count"] == 0

    first = service.start_action("wf-start-1", [])
    second = service.start_action("wf-start-2", [])
    assert first["supervisor"]["pid"] == second["supervisor"]["pid"]
    assert service.supervisor.spawn_count == 1

    stopped_once = service.stop_action("wf-stop-1", [])
    stopped_twice = service.stop_action("wf-stop-2", [])
    assert stopped_once["process_cleanup"]["process_group_reaped"] is True
    assert stopped_twice["process_cleanup"]["process_group_reaped"] is True
    assert service.read_chrome_status("wf-status-2", [])["supervisor"]["state"] == "stopped"


def test_start_target_selector_switches_generation(runtime, tmp_path) -> None:
    service = _service(runtime, tmp_path)
    try:
        first = service.start_action("wf-isolated", ["isolated"])
        assert first["attach_mode"] == "launch"
        assert service.supervisor.spawn_count == 1

        second = service.start_action("wf-tabs", ["tabs"])
        assert second["attach_mode"] == "auto_connect"
        assert service.attach_mode == "auto_connect"
        assert service.supervisor.spawn_count == 2

        with pytest.raises(ValueError, match="target must be one of"):
            service.start_action("wf-bad", ["sideways"])
    finally:
        service.close()


def test_mutating_action_is_blocked_before_process_start(runtime, tmp_path) -> None:
    script = _fake_mcp_script(tmp_path)
    tool = ChromeDevToolsTool(_fake_settings(runtime, script), mutation_allowed=lambda: False)

    with pytest.raises(ChromeControlFailure) as raised:
        tool.run("wf-blocked", {"action": "open", "args": ["https://example.test"]})

    assert raised.value.result["status"] == "blocked"
    assert raised.value.result["error"]["code"] == "mutation_not_allowed"
    assert tool._mcp_service.supervisor.spawn_count == 0
    tool.close()


def test_observational_action_needs_no_mutation_approval(runtime, tmp_path) -> None:
    script = _fake_mcp_script(tmp_path)
    tool = ChromeDevToolsTool(_fake_settings(runtime, script), mutation_allowed=lambda: False)
    try:
        result = tool.run("wf-list", {"action": "list", "args": []})
        assert result["status"] == "completed"
        assert result["schema_version"] == "chrome_control_result.v2"
    finally:
        tool.close()


def test_failure_generation_does_not_auto_restart(runtime, tmp_path) -> None:
    service = _service(runtime, tmp_path, "timeout", chrome_devtools_startup_timeout_seconds=0.1)
    with pytest.raises(ChromeControlFailure):
        service.start_action("wf-first", [])

    with pytest.raises(ChromeDevToolsError) as raised:
        service.ensure_ready(explicit=False)

    assert raised.value.status == "blocked"
    assert service.supervisor.spawn_count == 1
    service.close()


def test_evidence_is_bounded_and_redacted(runtime, tmp_path) -> None:
    service = _service(runtime, tmp_path, "secret")
    try:
        service.start_action("wf-secret", [])
        stderr_tail = service.supervisor.build_process_evidence()["stderr_tail"]
        assert "hunter2" not in stderr_tail
        assert "abc123" not in stderr_tail
        assert "[redacted]" in stderr_tail
        limit = runtime.settings.chrome_devtools_log_tail_chars
        assert len(stderr_tail) <= limit
    finally:
        service.close()


def test_redact_chrome_text_masks_secrets_and_home() -> None:
    home = str(Path.home())
    text = f"Authorization: Bearer sekrit token=abc profile at {home}/Library/Chrome"
    redacted = redact_chrome_text(text, 4000)
    assert "sekrit" not in redacted
    assert "token=abc" not in redacted
    assert home not in redacted
    assert "~/Library/Chrome" in redacted


def test_latest_spec_is_refused_before_spawn(runtime, tmp_path) -> None:
    script = _fake_mcp_script(tmp_path)
    settings = _fake_settings(runtime, script)
    settings.chrome_devtools_command_args = ["-y", "chrome-devtools-mcp@latest"]
    service = ChromeControlService(settings, mutation_allowed=lambda: True)

    with pytest.raises(ChromeControlFailure) as raised:
        service.start_action("wf-latest", [])

    result = raised.value.result
    assert result["status"] == "blocked"
    assert result["error"]["code"] == ChromeDevToolsErrorCode.COMMAND_NOT_RESOLVED.value
    assert service.supervisor.spawn_count == 0
    service.close()
