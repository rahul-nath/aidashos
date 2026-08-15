# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Opt-in host smoke test for the supervised Chrome DevTools MCP lifecycle.

Runs the real pinned npx command and launches a dedicated isolated headless
Chrome, so it never touches the operator's signed-in browser. Enable with:

    LOCAL_AGENT_CHROME_HOST_SMOKE=1 uv run pytest -q tests/test_chrome_devtools_host_smoke.py
"""

from __future__ import annotations

import os
import shutil

import pytest

from local_first_agent_os.chrome_devtools import (
    ChromeControlService,
    capture_descendant_process_snapshot,
    read_pid_command_map,
)

pytestmark = [
    pytest.mark.skipif(
        os.getenv("LOCAL_AGENT_CHROME_HOST_SMOKE") != "1",
        reason="host smoke is opt-in; set LOCAL_AGENT_CHROME_HOST_SMOKE=1",
    ),
    pytest.mark.skipif(shutil.which("npx") is None, reason="npx is not on PATH"),
]


def _chrome_mcp_process_count() -> int:
    return sum(
        1
        for _pid, (_ppid, command) in read_pid_command_map().items()
        if "chrome-devtools-mcp" in command
    )


def test_host_smoke_launch_list_stop_reaps_family(runtime) -> None:
    settings = runtime.settings.model_copy(deep=True)
    settings.chrome_devtools_transport = "mcp"
    settings.chrome_devtools_command = "npx"
    settings.chrome_devtools_command_args = ["-y", "chrome-devtools-mcp@1.6.0"]
    settings.chrome_devtools_start_args = ["--no-usage-statistics"]
    settings.chrome_devtools_attach_mode = "launch"
    settings.chrome_devtools_launch_args = ["--isolated", "--headless"]
    settings.chrome_devtools_startup_timeout_seconds = 60.0
    settings.chrome_devtools_attach_timeout_seconds = 60.0
    settings.chrome_devtools_call_timeout_seconds = 30.0
    settings.chrome_devtools_stop_timeout_seconds = 10.0
    settings.chrome_devtools_idle_shutdown_seconds = 0

    count_before = _chrome_mcp_process_count()
    service = ChromeControlService(settings, mutation_allowed=lambda: True)
    family: dict[int, str] = {}
    direct_pid: int | None = None
    try:
        started = service.start_action("wf-host-smoke", ["isolated"])
        assert started["status"] == "completed"
        supervisor_status = started["supervisor"]
        direct_pid = supervisor_status["pid"]
        assert direct_pid is not None
        assert supervisor_status["server_version"]

        listing = service.call_tool_command(["list_pages"])
        assert listing["returncode"] == 0
        assert listing["stdout"].strip(), "expected at least one known page"

        # Snapshot the full process family before stop so the reap proof
        # covers every descendant the test created (npx, node, Chrome).
        family = capture_descendant_process_snapshot(direct_pid)
        family[direct_pid] = "direct-child"
    finally:
        stopped = service.stop_action("wf-host-smoke-stop", [])

    assert stopped["process_cleanup"]["direct_child_reaped"] is True
    assert stopped["process_cleanup"]["process_group_reaped"] is True
    current = read_pid_command_map()
    survivors = [
        pid
        for pid, command in family.items()
        if pid in current and (command == "direct-child" or current[pid][1] == command)
    ]
    assert not survivors, f"test-created processes survived: {survivors}"
    assert _chrome_mcp_process_count() == count_before
