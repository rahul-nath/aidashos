# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The MCP readiness adapter stays an adapter.

run_first_run_check exists for MCP clients without a shell; the checks
themselves live in scripts/first-run-check.sh alone. These tests pin the
adapter behavior (payload shape, ANSI stripping, the rooted-checkout guard)
with a stand-in subprocess, so no test spends time or network on the real
probes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from local_first_agent_os.coordination import machine_readiness
from local_first_agent_os.coordination.store import set_root


@pytest.fixture
def restore_root():
    yield
    set_root(None)


def test_refuses_a_root_that_is_not_a_checkout(tmp_path: Path, restore_root) -> None:
    set_root(str(tmp_path))
    result = machine_readiness.run_first_run_check()
    assert result["ok"] is False
    assert result["error"] == "MissingScript"


def test_reports_ready_and_strips_ansi(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="  \x1b[32mok\x1b[0m       uv 0.10.0\n",
            stderr="",
        )

    monkeypatch.setattr(machine_readiness.subprocess, "run", fake_run)
    result = machine_readiness.run_first_run_check()
    assert result == {
        "ok": True,
        "ready": True,
        "exit_code": 0,
        "report": "ok       uv 0.10.0",
    }


def test_blocked_report_keeps_the_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=1,
            stdout="  \x1b[31mblocked\x1b[0m  junior model missing\n",
            stderr="fix: ./scripts/boot/31-fetch-model-gemma4.sh\n",
        )

    monkeypatch.setattr(machine_readiness.subprocess, "run", fake_run)
    result = machine_readiness.run_first_run_check()
    assert result["ok"] is True
    assert result["ready"] is False
    assert result["exit_code"] == 1
    assert "blocked  junior model missing" in result["report"]
    assert "31-fetch-model-gemma4.sh" in result["report"]


def test_timeout_surfaces_as_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)

    monkeypatch.setattr(machine_readiness.subprocess, "run", fake_run)
    result = machine_readiness.run_first_run_check()
    assert result["ok"] is False
    assert result["error"] == "Timeout"


def test_operator_server_exposes_the_tool_and_the_agent_server_does_not() -> None:
    from local_first_agent_os.coordination.cli import AGENT_READABLE_TOOLS, build_mcp_server

    assert "run_first_run_check" not in AGENT_READABLE_TOOLS
    server = build_mcp_server()
    import anyio

    tool_names = {tool.name for tool in anyio.run(server.list_tools)}
    assert "run_first_run_check" in tool_names
