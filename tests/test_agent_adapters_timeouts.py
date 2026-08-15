# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from local_first_agent_os.agent_adapters import AgentTask, ClaudeCodeAdapter

# Real wall-clock time, spent in full on every run, and it has to cover
# interpreter spawn before it covers anything this test is about. At 0.5s it
# cleared a spawn on an idle machine and lost to one under full-suite load,
# where the child had not written its pid by the time the adapter gave up and
# the assertion below read a file that did not exist yet. The number is a
# margin over the slowest spawn the suite produces, not a duration anything
# here is measuring, so widening it costs seconds and buys determinism.
TIMEOUT_SECONDS = 3.0


def test_claude_adapter_timeout_terminates_child(tmp_path: Path) -> None:
    pid_path = tmp_path / "child.pid"
    executable = tmp_path / "fake-claude"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, time\n"
        f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    task = AgentTask(
        task_id="timeout-test",
        pow_wow_id="pow-wow",
        saga_id="saga",
        role="staff",
        prompt="wait",
        timeout_seconds=TIMEOUT_SECONDS,  # type: ignore[arg-type]
    )

    result = asyncio.run(ClaudeCodeAdapter(claude_bin=str(executable)).run(task))

    assert not result.success
    # Derived rather than written out, so the budget and the message it produces
    # cannot drift apart the next time the number moves.
    assert result.error == f"Timeout after {TIMEOUT_SECONDS}s"
    pid = int(pid_path.read_text(encoding="utf-8"))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError(f"timed-out adapter child {pid} is still running")
