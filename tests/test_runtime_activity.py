# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What decides whether the runtime may be stopped.

The decision has three parts and they are tested apart: the ledger read that
produces the answer, the shell hook that acts on it when the last terminal
leaves, and `stop-agent-runtime.sh`, which asks the same question because it is
now the only way to stop the runtime at all. Both scripts are driven with a fake
command so that all three answers - including the one that cost a live runtime -
are reachable without a database.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from local_first_agent_os.contracts import DispatchIntentStatus, LeaseStatus
from local_first_agent_os.coordination import runtime_activity as module
from local_first_agent_os.coordination.dispatch import (
    claim_next_dispatch_intent,
    submit_dispatch_intent,
)
from local_first_agent_os.coordination.runtime_activity import (
    RuntimeActivityAnswer,
    RuntimeActivityBusy,
    RuntimeActivityIdle,
    RuntimeActivityUnknown,
    RuntimeProcessPresence,
    RuntimeWorkFact,
    RuntimeWorkKind,
    answer_of,
    read_runtime_activity,
    render_runtime_activity,
)
from local_first_agent_os.work_units.lifecycle import WorkUnitStatus

REPO_ROOT = Path(__file__).resolve().parents[1]


class _FakeConnection:
    def __init__(
        self,
        rows: list[dict[str, str]],
        *,
        rollback_failure: Exception | None = None,
        close_failure: Exception | None = None,
    ) -> None:
        self._rows = rows
        self._rollback_failure = rollback_failure
        self._close_failure = close_failure
        self.rolled_back = False
        self.closed = False

    def execute(self, sql: str, params: object = None) -> _FakeConnection:
        self.sql = sql
        return self

    def fetchall(self) -> list[dict[str, str]]:
        return self._rows

    def rollback(self) -> None:
        self.rolled_back = True
        if self._rollback_failure is not None:
            raise self._rollback_failure

    def close(self) -> None:
        self.closed = True
        if self._close_failure is not None:
            raise self._close_failure


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


def test_a_malformed_live_row_is_unknown_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _with_rows(
        monkeypatch,
        [{"fact_kind": "new_unclassified_kind", "identifier": "fact-1", "status": "LIVE"}],
    )

    activity = read_runtime_activity()

    assert isinstance(activity, RuntimeActivityUnknown)
    assert "new_unclassified_kind" in activity.reason
    assert connection.rolled_back and connection.closed


def test_a_failed_rollback_is_unknown_and_the_connection_is_still_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection([], rollback_failure=RuntimeError("connection died"))
    monkeypatch.setattr(module, "connect", lambda **_: connection)

    activity = read_runtime_activity()

    assert isinstance(activity, RuntimeActivityUnknown)
    assert "connection died" in render_runtime_activity(activity)
    assert connection.closed


def test_live_process_statuses_are_derived_from_exhaustive_classifiers() -> None:
    assert {
        status
        for status in DispatchIntentStatus
        if module._dispatch_intent_process(status) is RuntimeProcessPresence.LIVE
    } == {DispatchIntentStatus.CLAIMED, DispatchIntentStatus.IN_PROGRESS}
    assert {
        status
        for status in LeaseStatus
        if module._execution_lease_process(status) is RuntimeProcessPresence.LIVE
    } == {LeaseStatus.ACTIVE, LeaseStatus.CANCEL_REQUESTED}
    assert {
        status
        for status in WorkUnitStatus
        if module._work_unit_process(status) is RuntimeProcessPresence.LIVE
    } == {WorkUnitStatus.RUNNING, WorkUnitStatus.CANCELLING}


def test_the_query_reads_status_columns_of_the_three_named_tables() -> None:
    sql = module._LIVE_WORK_QUERY

    assert "dispatch_intents" in sql and "agent_execution_leases" in sql and "work_units" in sql
    assert "'CLAIMED'" in sql and "'ACTIVE'" in sql and "'RUNNING'" in sql
    # A queued intent has no executor, so a stop defers it rather than
    # destroying it. Counting it would hold runtimes up for nothing.
    assert "'PENDING'" not in sql
    assert "JOIN" not in sql.upper()
    assert f"LIMIT {module._MAX_REPORTED_FACTS + 1}" in sql


def test_more_live_rows_than_the_report_bound_are_marked_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "fact_kind": RuntimeWorkKind.DISPATCH_INTENT.value,
            "identifier": f"intent-{index}",
            "status": DispatchIntentStatus.CLAIMED.value,
        }
        for index in range(module._MAX_REPORTED_FACTS + 1)
    ]
    _with_rows(monkeypatch, rows)

    activity = read_runtime_activity()

    assert isinstance(activity, RuntimeActivityBusy)
    assert len(activity.facts) == module._MAX_REPORTED_FACTS
    assert activity.truncated
    assert f"more live rows than the {module._MAX_REPORTED_FACTS} listed" in (
        render_runtime_activity(activity)
    )


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


def _real_activity_checkout(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A harmless stop boundary whose activity command is the real Python CLI."""

    root = tmp_path / "real-checkout"
    scripts = root / "scripts"
    fake_bin = root / "bin"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    hook = scripts / "pi_terminal_session.sh"
    hook.write_bytes((REPO_ROOT / "scripts" / "pi_terminal_session.sh").read_bytes())
    hook.chmod(0o755)
    stop_record = tmp_path / "real-stop-record"
    _write_executable(
        scripts / "stop-agent-runtime.sh",
        f"#!/usr/bin/env bash\nprintf 'stopped\\n' > {shlex.quote(str(stop_record))}\n",
    )
    python = shlex.quote(sys.executable)
    source = shlex.quote(str(REPO_ROOT / "src"))
    _write_executable(
        fake_bin / "uv",
        f"""#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  *runtime-activity*)
    export PYTHONPATH={source}${{PYTHONPATH:+:$PYTHONPATH}}
    exec {python} -m local_first_agent_os.cli runtime-activity
    ;;
esac
exit 0
""",
    )
    return root, hook, stop_record


def _leave_last_terminal_with_real_activity(
    tmp_path: Path,
    *,
    environment_overrides: dict[str, str] | None = None,
) -> tuple[Path, Path, subprocess.CompletedProcess[str]]:
    root, hook, stop_record = _real_activity_checkout(tmp_path)
    daemon_dir = tmp_path / "real-daemon"
    daemon_dir.mkdir()
    (daemon_dir / "sessions").write_text(f"{os.getpid()}\n", encoding="utf-8")
    stop_log = tmp_path / "real-stop.log"
    environment = {
        **os.environ,
        "PATH": f"{root / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "AGENT_COORDINATION_ROOT": str(REPO_ROOT),
        "LOCAL_AGENT_DAEMON_DIR": str(daemon_dir),
        "LOCAL_AGENT_STOP_RUNTIME_LOG": str(stop_log),
        **(environment_overrides or {}),
    }

    completed = subprocess.run(
        ["bash", str(hook), "leave", str(os.getpid())],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )

    return stop_record, stop_log, completed


@pytest.mark.skipif(sys.platform == "win32", reason="the hook is a bash script")
def test_a_real_idle_ledger_allows_the_last_terminal_to_stop(tmp_path: Path) -> None:
    stop_record, stop_log, completed = _leave_last_terminal_with_real_activity(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert stop_record.read_text(encoding="utf-8") == "stopped\n"
    assert "Runtime left running" not in stop_log.read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform == "win32", reason="the hook is a bash script")
def test_a_real_claimed_intent_survives_the_last_terminal_leaving(tmp_path: Path) -> None:
    submitted = submit_dispatch_intent(tier="senior", prompt="prove the runtime stays alive")
    claimed = claim_next_dispatch_intent("integration-worker", tier="senior")["intent"]
    assert claimed is not None
    assert claimed["intent_id"] == submitted["intent_id"]

    stop_record, stop_log, completed = _leave_last_terminal_with_real_activity(tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert not stop_record.exists()
    recorded = stop_log.read_text(encoding="utf-8")
    assert "Runtime left running (busy)" in recorded
    assert f"dispatch_intent {submitted['intent_id']} is CLAIMED" in recorded


@pytest.mark.skipif(sys.platform == "win32", reason="the hook is a bash script")
def test_a_real_unreadable_ledger_refuses_the_last_terminal_stop(tmp_path: Path) -> None:
    unreachable_url = "postgresql://postgres:postgres@127.0.0.1:1/local_agent"

    stop_record, stop_log, completed = _leave_last_terminal_with_real_activity(
        tmp_path,
        environment_overrides={
            "AGENT_COORDINATION_DATABASE_URL": unreachable_url,
            "LOCAL_AGENT_COORDINATION_DATABASE_URL": unreachable_url,
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert not stop_record.exists()
    recorded = stop_log.read_text(encoding="utf-8")
    assert "Runtime left running (unknown)" in recorded
    assert "Runtime left running (busy)" not in recorded
    assert "could not be read" in recorded


# --------------------------------------------------------------------------
# The stop script's own guard.
#
# The hook above only protects the path where a closing terminal fired the stop.
# Nothing starts or stops this runtime automatically any more, so the script is
# the path, and these cover it directly.
# --------------------------------------------------------------------------


def _fake_stop_checkout(tmp_path: Path, *, activity_output: str, activity_exit: int = 0) -> Path:
    """A checkout holding the real stop script and stubs for everything it reaches.

    The script is copied rather than imitated, so what runs here is byte-for-byte
    what an operator runs. Every external command it shells to is stubbed to do
    nothing, which is what lets the idle case run the script to completion
    without stopping anything on this machine.
    """

    root = tmp_path / "checkout"
    (root / "scripts").mkdir(parents=True)
    (root / "bin").mkdir()
    for name in ("stop-agent-runtime.sh", "resident-loop-owners.sh"):
        destination = root / "scripts" / name
        destination.write_text(
            (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
        destination.chmod(0o755)

    answer_file = root / "activity-answer"
    answer_file.write_text(activity_output, encoding="utf-8")
    # A command that fails has produced diagnostics, not an answer.
    stream = "" if activity_exit == 0 else " >&2"
    _write_executable(
        root / "bin" / "uv",
        f"""#!/usr/bin/env bash
set -euo pipefail
case "$*" in
  *runtime-activity*)
    printf 'asked\n' >> "$FAKE_ASK_RECORD"
    cat "{answer_file}"{stream}
    exit {activity_exit}
    ;;
esac
exit 0
""",
    )
    # curl records itself because the session memory flush is the first write the
    # script performs, so "curl was never called" is how a refusal proves it
    # refused before moving anything.
    _write_executable(
        root / "bin" / "curl",
        "#!/usr/bin/env bash\nprintf 'curled\\n' >> \"$FAKE_CURL_RECORD\"\nexit 1\n",
    )
    _write_executable(root / "bin" / "launchctl", "#!/usr/bin/env bash\nexit 113\n")
    _write_executable(root / "bin" / "lsof", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(root / "bin" / "pgrep", "#!/usr/bin/env bash\nexit 1\n")
    _write_executable(root / "bin" / "docker", "#!/usr/bin/env bash\nexit 0\n")
    return root


def _run_stop(
    tmp_path: Path, root: Path, *arguments: str
) -> tuple[subprocess.CompletedProcess[str], Path, Path]:
    daemon_dir = tmp_path / "daemon"
    daemon_dir.mkdir(exist_ok=True)
    ask_record = tmp_path / "ask-record"
    curl_record = tmp_path / "curl-record"
    completed = subprocess.run(
        ["bash", str(root / "scripts" / "stop-agent-runtime.sh"), *arguments],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{root / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "LOCAL_AGENT_DAEMON_DIR": str(daemon_dir),
            "FAKE_ASK_RECORD": str(ask_record),
            "FAKE_CURL_RECORD": str(curl_record),
        },
    )
    return completed, ask_record, curl_record


@pytest.mark.skipif(sys.platform == "win32", reason="the stop script is a bash script")
def test_the_stop_script_refuses_a_busy_ledger(tmp_path: Path) -> None:
    root = _fake_stop_checkout(
        tmp_path, activity_output="busy\ndispatch_intent intent-7 is CLAIMED\n"
    )

    completed, _, curl_record = _run_stop(tmp_path, root)

    assert completed.returncode == 1
    assert "Refusing to stop (busy): work is in flight." in completed.stderr
    assert "dispatch_intent intent-7 is CLAIMED" in completed.stderr
    assert "--force" in completed.stderr
    assert "Local agent runtime stopped." not in completed.stdout
    # The refusal came before the session memory flush, so nothing was written.
    assert not curl_record.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="the stop script is a bash script")
def test_the_stop_script_runs_on_an_idle_ledger(tmp_path: Path) -> None:
    root = _fake_stop_checkout(tmp_path, activity_output="idle\n")

    completed, ask_record, _ = _run_stop(tmp_path, root)

    assert completed.returncode == 0, completed.stderr
    assert "Local agent runtime stopped." in completed.stdout
    assert "Refusing to stop" not in completed.stderr
    assert ask_record.read_text(encoding="utf-8") == "asked\n"


@pytest.mark.skipif(sys.platform == "win32", reason="the stop script is a bash script")
def test_the_stop_script_refuses_an_unreadable_ledger_distinctly_from_busy(
    tmp_path: Path,
) -> None:
    root = _fake_stop_checkout(
        tmp_path,
        activity_output="unknown\nthe coordination ledger could not be read: no route to host\n",
    )

    completed, _, _ = _run_stop(tmp_path, root)

    assert completed.returncode == 1
    assert "Refusing to stop (unknown):" in completed.stderr
    assert "work is in flight." not in completed.stderr
    assert "no route to host" in completed.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="the stop script is a bash script")
def test_the_stop_script_refuses_when_the_check_cannot_run(tmp_path: Path) -> None:
    root = _fake_stop_checkout(
        tmp_path, activity_output="ImportError: no such command\n", activity_exit=1
    )

    completed, _, _ = _run_stop(tmp_path, root)

    assert completed.returncode == 1
    assert "Refusing to stop (unknown):" in completed.stderr
    assert "the runtime activity check could not run" in completed.stderr
    assert "ImportError: no such command" in completed.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="the stop script is a bash script")
def test_the_stop_script_refuses_a_word_it_does_not_know(tmp_path: Path) -> None:
    root = _fake_stop_checkout(tmp_path, activity_output="maybe\n")

    completed, _, _ = _run_stop(tmp_path, root)

    assert completed.returncode == 1
    assert "Refusing to stop (maybe):" in completed.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="the stop script is a bash script")
def test_the_stop_script_force_stops_without_asking(tmp_path: Path) -> None:
    """The override skips the question rather than overriding its answer.

    An operator who has already decided should not wait on a ledger read, least
    of all in the case where the ledger is the thing that is broken.
    """

    root = _fake_stop_checkout(
        tmp_path, activity_output="busy\ndispatch_intent intent-7 is CLAIMED\n"
    )

    completed, ask_record, _ = _run_stop(tmp_path, root, "--force")

    assert completed.returncode == 0, completed.stderr
    assert "Local agent runtime stopped." in completed.stdout
    assert not ask_record.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="the stop script is a bash script")
def test_the_stop_script_rejects_an_unrecognised_argument(tmp_path: Path) -> None:
    root = _fake_stop_checkout(tmp_path, activity_output="idle\n")

    completed, _, curl_record = _run_stop(tmp_path, root, "--frce")

    assert completed.returncode == 2
    assert "Unknown argument: --frce" in completed.stderr
    assert not curl_record.exists()
