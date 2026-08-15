# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What decides whether closing the last terminal stops the runtime.

The decision has two halves and they are tested apart: the ledger read that
produces the answer, and the shell hook that acts on it. The hook is driven with
a fake command so that all three answers - including the one that cost a live
runtime - are reachable without a database.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from local_first_agent_os.coordination import runtime_activity as module
from local_first_agent_os.coordination.runtime_activity import (
    RuntimeActivityAnswer,
    RuntimeActivityBusy,
    RuntimeActivityIdle,
    RuntimeActivityUnknown,
    RuntimeWorkFact,
    RuntimeWorkKind,
    answer_of,
    read_runtime_activity,
    render_runtime_activity,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class _FakeConnection:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = rows
        self.rolled_back = False
        self.closed = False

    def execute(self, sql: str, params: object = None) -> _FakeConnection:
        self.sql = sql
        return self

    def fetchall(self) -> list[dict[str, str]]:
        return self._rows

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def _with_rows(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, str]]) -> _FakeConnection:
    connection = _FakeConnection(rows)
    monkeypatch.setattr(module, "connect", lambda **_: connection)
    return connection


def test_a_claimed_intent_is_busy_and_names_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_rows(
        monkeypatch,
        [{"fact_kind": "dispatch_intent", "identifier": "intent-7", "status": "CLAIMED"}],
    )

    activity = read_runtime_activity()

    assert activity == RuntimeActivityBusy(
        facts=(
            RuntimeWorkFact(
                kind=RuntimeWorkKind.DISPATCH_INTENT, identifier="intent-7", status="CLAIMED"
            ),
        )
    )
    assert render_runtime_activity(activity) == "busy\ndispatch_intent intent-7 is CLAIMED"


def test_no_live_rows_is_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _with_rows(monkeypatch, [])

    activity = read_runtime_activity()

    assert isinstance(activity, RuntimeActivityIdle)
    assert render_runtime_activity(activity) == "idle"
    assert connection.rolled_back and connection.closed


def test_an_unreadable_ledger_is_unknown_and_not_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(**_: object) -> object:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(module, "connect", refuse)

    activity = read_runtime_activity()

    assert isinstance(activity, RuntimeActivityUnknown)
    assert answer_of(activity) is RuntimeActivityAnswer.UNKNOWN
    assert "connection refused" in render_runtime_activity(activity)


def test_more_live_rows_than_are_listed_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_rows(
        monkeypatch,
        [
            {"fact_kind": "execution_lease", "identifier": f"lease-{index}", "status": "ACTIVE"}
            for index in range(module._MAX_REPORTED_FACTS + 1)
        ],
    )

    activity = read_runtime_activity()

    assert isinstance(activity, RuntimeActivityBusy)
    assert activity.truncated
    assert len(activity.facts) == module._MAX_REPORTED_FACTS
    assert "more live rows" in render_runtime_activity(activity)


def test_the_query_reads_status_columns_of_the_three_named_tables() -> None:
    sql = module._LIVE_WORK_QUERY

    assert "dispatch_intents" in sql and "agent_execution_leases" in sql and "work_units" in sql
    assert "'CLAIMED'" in sql and "'ACTIVE'" in sql and "'RUNNING'" in sql
    # A queued intent has no executor, so a stop defers it rather than
    # destroying it. Counting it would hold runtimes up for nothing.
    assert "'PENDING'" not in sql
    assert "JOIN" not in sql.upper()


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fake_runtime(tmp_path: Path, *, activity_output: str, activity_exit: int = 0) -> Path:
    """A checkout whose stop script and activity command only record themselves.

    The hook resolves both from its own directory, so the hook under test is
    copied rather than imitated: the script exercised here is byte-for-byte the
    one the terminal runs.
    """

    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    (root / "bin").mkdir()
    hook = root / "scripts" / "pi_terminal_session.sh"
    hook.write_text(
        (REPO_ROOT / "scripts" / "pi_terminal_session.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    hook.chmod(0o755)
    _write_executable(
        root / "scripts" / "stop-agent-runtime.sh",
        "#!/usr/bin/env bash\nprintf 'stopped\\n' >> \"$FAKE_STOP_RECORD\"\n",
    )
    answer_file = root / "activity-answer"
    answer_file.write_text(activity_output, encoding="utf-8")
    # A command that fails has produced diagnostics, not an answer, so a
    # non-zero run writes to the stream diagnostics go to.
    stream = "" if activity_exit == 0 else " >&2"
    _write_executable(
        root / "bin" / "uv",
        f"""#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  *runtime-activity*)
    cat "{answer_file}"{stream}
    exit {activity_exit}
    ;;
esac
exit 0
""",
    )
    return root


def _leave_last_terminal(tmp_path: Path, root: Path) -> tuple[Path, Path, str]:
    daemon_dir = tmp_path / "daemon"
    daemon_dir.mkdir()
    # This process is the one registered terminal, so it is live to `prune_sessions`
    # and leaving it takes the count to zero.
    (daemon_dir / "sessions").write_text(f"{os.getpid()}\n", encoding="utf-8")
    stop_record = tmp_path / "stop-record"
    stop_log = tmp_path / "stop.log"
    completed = subprocess.run(
        ["bash", str(root / "scripts" / "pi_terminal_session.sh"), "leave", str(os.getpid())],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{root / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "LOCAL_AGENT_DAEMON_DIR": str(daemon_dir),
            "LOCAL_AGENT_STOP_RUNTIME_LOG": str(stop_log),
            "FAKE_STOP_RECORD": str(stop_record),
        },
    )
    assert completed.returncode == 0, completed.stderr
    return stop_record, stop_log, completed.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="the hook is a bash script")
def test_the_last_terminal_leaving_a_busy_runtime_does_not_stop_it(tmp_path: Path) -> None:
    root = _fake_runtime(tmp_path, activity_output="busy\ndispatch_intent intent-7 is CLAIMED\n")

    stop_record, stop_log, stderr = _leave_last_terminal(tmp_path, root)

    assert not stop_record.exists()
    recorded = stop_log.read_text(encoding="utf-8")
    assert "Runtime left running (busy): work is in flight." in recorded
    assert "dispatch_intent intent-7 is CLAIMED" in recorded
    assert "Stop anyway:" in recorded
    assert str(stop_log) in stderr


@pytest.mark.skipif(sys.platform == "win32", reason="the hook is a bash script")
def test_the_last_terminal_leaving_an_idle_runtime_stops_it(tmp_path: Path) -> None:
    root = _fake_runtime(tmp_path, activity_output="idle\n")

    stop_record, stop_log, _ = _leave_last_terminal(tmp_path, root)

    assert stop_record.read_text(encoding="utf-8") == "stopped\n"
    assert "Runtime left running" not in stop_log.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="the hook is a bash script")
def test_an_unreadable_ledger_refuses_the_stop_distinctly_from_busy(tmp_path: Path) -> None:
    root = _fake_runtime(
        tmp_path,
        activity_output="unknown\nthe coordination ledger could not be read: no route to host\n",
    )

    stop_record, stop_log, _ = _leave_last_terminal(tmp_path, root)

    assert not stop_record.exists()
    recorded = stop_log.read_text(encoding="utf-8")
    assert "Runtime left running (unknown):" in recorded
    assert "work is in flight." not in recorded
    assert "no route to host" in recorded


@pytest.mark.skipif(sys.platform == "win32", reason="the hook is a bash script")
def test_a_check_that_cannot_run_at_all_refuses_the_stop(tmp_path: Path) -> None:
    root = _fake_runtime(
        tmp_path, activity_output="ImportError: no such command\n", activity_exit=1
    )

    stop_record, stop_log, _ = _leave_last_terminal(tmp_path, root)

    assert not stop_record.exists()
    recorded = stop_log.read_text(encoding="utf-8")
    assert "Runtime left running (unknown):" in recorded
    assert "the runtime activity check could not run" in recorded
    assert "ImportError: no such command" in recorded


@pytest.mark.skipif(sys.platform == "win32", reason="the hook is a bash script")
def test_a_word_the_hook_does_not_know_refuses_the_stop(tmp_path: Path) -> None:
    root = _fake_runtime(tmp_path, activity_output="maybe\n")

    stop_record, _, _ = _leave_last_terminal(tmp_path, root)

    assert not stop_record.exists()
