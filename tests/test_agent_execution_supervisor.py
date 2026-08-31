# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from staffing_support import repo_bench

from local_first_agent_os.agent_execution_supervisor import (
    StreamingCommandSupervisor,
    _execute_bounded_blocking_call,
    has_meaningful_agent_progress,
    normalize_jsonl_line,
)
from local_first_agent_os.coordination import (
    AppendExecutionEvent,
    AttachExecutionArtifact,
    CompleteExecutionLease,
    CreateExecutionCheckpoint,
    DispatchKind,
    HeartbeatExecutionLease,
    OpenExecutionLease,
    RequestExecutionCancel,
    parse_coordination_result,
)
from local_first_agent_os.coordination.checkpoints import (
    append_execution_event,
    attach_execution_artifact,
    create_execution_checkpoint,
    list_execution_artifacts,
    list_execution_checkpoints,
    list_execution_events,
)
from local_first_agent_os.coordination.execution import (
    complete_execution_lease,
    heartbeat_execution_lease,
    list_execution_leases,
    open_execution_lease,
    request_execution_cancel,
)
from local_first_agent_os.coordination.store import set_root
from local_first_agent_os.pow_wow import (
    CliPowWowExecutor,
    PowWowExecutionContext,
    PowWowTaskSpec,
)
from local_first_agent_os.pow_wow.types import ExecutionAttemptLease
from local_first_agent_os.project_access import AccessMode, ProjectAccessPolicy
from local_first_agent_os.project_center import LinkedProject
from local_first_agent_os.staffing import Harness, JudgmentRole
from local_first_agent_os.vocabulary import DispatchTier


class _Artifacts:
    def __init__(self) -> None:
        self.contents: dict[str, str] = {}
        self.workflow_ids: list[str | None] = []

    def write_text(self, *, role: str, text: str, workflow_id: str | None, **_: Any) -> Any:
        artifact_id = f"{role}:{len(self.contents) + 1}"
        self.contents[artifact_id] = text
        self.workflow_ids.append(workflow_id)
        return SimpleNamespace(artifact_id=artifact_id)

    def read_text(self, artifact_id: str) -> str:
        return self.contents[artifact_id]


class _BlockingArtifacts(_Artifacts):
    def write_text(self, **kwargs: Any) -> Any:
        time.sleep(2)
        return super().write_text(**kwargs)


class _FastTerminationSupervisor(StreamingCommandSupervisor):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            **kwargs,
            heartbeat_seconds=0.02,
            warning_seconds=0.05,
            termination_grace_seconds=0.05,
        )


def _coord(command: Any) -> Any:
    if isinstance(command, AppendExecutionEvent):
        payload = append_execution_event(
            command.lease_id,
            command.sequence,
            command.occurred_at,
            command.source,
            command.kind,
            dict(command.payload),
            command.payload_sha256,
        )
    elif isinstance(command, HeartbeatExecutionLease):
        payload = heartbeat_execution_lease(command.lease_id, command.worker_id)
    elif isinstance(command, RequestExecutionCancel):
        payload = request_execution_cancel(command.lease_id, command.reason, command.requested_by)
    elif isinstance(command, AttachExecutionArtifact):
        payload = attach_execution_artifact(
            command.lease_id,
            command.artifact_id,
            command.role,
            command.schema_version,
        )
    elif isinstance(command, CreateExecutionCheckpoint):
        payload = create_execution_checkpoint(
            command.lease_id,
            reason=command.reason,
            status=command.status,
            saga_id=command.saga_id,
            pow_wow_id=command.pow_wow_id,
            worktree_path=command.worktree_path,
            source_repo_path=command.source_repo_path,
            base_head_sha=command.base_head_sha,
            transcript_artifact_id=command.transcript_artifact_id,
            patch_artifact_id=command.patch_artifact_id,
            git_status_artifact_id=command.git_status_artifact_id,
            test_summary_artifact_id=command.test_summary_artifact_id,
            task_contract=command.task_contract,
            event_summary=command.event_summary,
            submit_review=command.submit_review,
            error=command.error,
        )
    else:  # pragma: no cover - a new supervisor command must be mapped explicitly
        raise AssertionError(type(command))
    assert payload["ok"], payload
    return parse_coordination_result(command, payload)


def _lease(tmp_path: Path) -> ExecutionAttemptLease:
    set_root(str(tmp_path))
    opened = open_execution_lease("supervisor-test", "worker-1", timeout_seconds=30)
    return ExecutionAttemptLease(
        idempotency_key="supervisor-test",
        worker_id="worker-1",
        lease_id=opened["lease"]["lease_id"],
        created=True,
        open_status="ACTIVE",
    )


def _git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "file.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "file.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)


def test_streams_jsonl_and_persists_transcript(tmp_path: Path, monkeypatch) -> None:
    lease = _lease(tmp_path)
    artifacts = _Artifacts()
    supervisor = StreamingCommandSupervisor(
        coordination_command=_coord, artifact_writer=artifacts, heartbeat_seconds=0.02
    )
    code = (
        "import json,time; "
        "event={'type':'item.completed','item':"
        "{'type':'agent_message','text':'done'}}; "
        "print(json.dumps(event), flush=True); "
        "time.sleep(.05)"
    )

    result = __import__("asyncio").run(
        supervisor.run(
            [sys.executable, "-u", "-c", code],
            tmp_path,
            lease=lease,
            harness="codex",
            timeout_seconds=2,
        )
    )

    assert result.capture.exit_code == 0
    assert result.checkpoint_id is None
    assert result.transcript_artifact_id in artifacts.contents
    assert artifacts.workflow_ids == [None]
    kinds = [item["kind"] for item in list_execution_events(lease.lease_id or "")["events"]]
    assert "item.completed:agent_message" in kinds
    assert "lease.heartbeat" in kinds


def test_large_jsonl_event_does_not_hide_later_final_message(tmp_path: Path, monkeypatch) -> None:
    lease = _lease(tmp_path)
    artifacts = _Artifacts()
    supervisor = StreamingCommandSupervisor(
        coordination_command=_coord, artifact_writer=artifacts, heartbeat_seconds=0.02
    )
    code = (
        "import json; "
        "print(json.dumps({'type':'item.completed','item':"
        "{'type':'command_execution','aggregated_output':'x'*70000}}), flush=True); "
        "print(json.dumps({'type':'item.completed','item':"
        "{'type':'agent_message','text':'final verdict'}}), flush=True)"
    )

    result = asyncio.run(
        supervisor.run(
            [sys.executable, "-u", "-c", code],
            tmp_path,
            lease=lease,
            harness="codex",
            timeout_seconds=2,
        )
    )

    assert result.capture.exit_code == 0
    events = list_execution_events(lease.lease_id or "", limit=1000)["events"]
    agent_messages = [event for event in events if event["kind"] == "item.completed:agent_message"]
    assert len(agent_messages) == 1
    assert agent_messages[0]["payload"]["item"]["text"] == "final verdict"


def test_deadline_kills_process_group_and_checkpoints_patch(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    lease = _lease(tmp_path)
    artifacts = _Artifacts()
    supervisor = StreamingCommandSupervisor(
        coordination_command=_coord,
        artifact_writer=artifacts,
        heartbeat_seconds=0.02,
        warning_seconds=0.05,
        termination_grace_seconds=0.1,
    )
    code = (
        "from pathlib import Path; import time; "
        "Path('file.txt').write_text('changed\\n'); time.sleep(10)"
    )

    result = __import__("asyncio").run(
        supervisor.run(
            [sys.executable, "-u", "-c", code],
            repo,
            lease=lease,
            harness="codex",
            timeout_seconds=0.15,
            source_repo_path=repo,
            task_contract="bounded test",
        )
    )

    assert result.capture.exit_code == 124
    assert result.checkpoint_reason == "deadline"
    assert result.checkpoint_id
    assert result.preserve_worktree is True
    assert artifacts.workflow_ids and set(artifacts.workflow_ids) == {None}
    assert any("changed" in text for key, text in artifacts.contents.items() if "patch" in key)


def test_deadline_sigkills_term_resistant_process_and_finishes_checkpoint(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    lease = _lease(tmp_path)
    artifacts = _Artifacts()
    supervisor = StreamingCommandSupervisor(
        coordination_command=_coord,
        artifact_writer=artifacts,
        heartbeat_seconds=0.02,
        warning_seconds=0.05,
        termination_grace_seconds=0.05,
    )
    code = (
        "from pathlib import Path; import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path('file.txt').write_text('changed despite timeout\\n'); "
        "time.sleep(10)"
    )

    result = asyncio.run(
        supervisor.run(
            [sys.executable, "-u", "-c", code],
            repo,
            lease=lease,
            harness="codex",
            timeout_seconds=0.2,
            source_repo_path=repo,
            task_contract="force the SIGKILL recovery path",
        )
    )

    assert result.capture.exit_code == 124
    assert result.checkpoint_reason == "deadline"
    assert result.checkpoint_id
    events = list_execution_events(lease.lease_id or "", limit=1000)["events"]
    kinds = [event["kind"] for event in events]
    assert kinds.count("process.sigkill") == 1
    assert kinds.index("process.sigkill") < kinds.index("process.exited")
    assert kinds.index("process.exited") < kinds.index("agent.finished")
    artifact_roles = {
        item["role"]
        for item in list_execution_artifacts(lease.lease_id or "")["execution_artifacts"]
    }
    assert artifact_roles == {
        "agent_execution_transcript",
        "agent_checkpoint_git_status",
        "agent_checkpoint_patch",
        "agent_checkpoint_test_summary",
    }
    checkpoints = [
        item
        for item in list_execution_checkpoints()["checkpoints"]
        if item["lease_id"] == lease.lease_id
    ]
    assert len(checkpoints) == 1
    assert checkpoints[0]["checkpoint_id"] == result.checkpoint_id


def test_sigkill_does_not_hang_when_escaped_descendant_holds_output_pipes(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    lease = _lease(tmp_path)
    artifacts = _Artifacts()
    supervisor = StreamingCommandSupervisor(
        coordination_command=_coord,
        artifact_writer=artifacts,
        heartbeat_seconds=0.02,
        warning_seconds=0.05,
        termination_grace_seconds=0.05,
    )
    escaped_pid_path = repo / "escaped-child.pid"
    escaped_code = "import time; time.sleep(30)"
    code = (
        "from pathlib import Path; import signal,subprocess,sys,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"child=subprocess.Popen([sys.executable,'-u','-c',{escaped_code!r}], "
        "start_new_session=True); "
        "Path('escaped-child.pid').write_text(str(child.pid)); "
        "print('parent ready', flush=True); time.sleep(30)"
    )

    try:
        result = asyncio.run(
            asyncio.wait_for(
                supervisor.run(
                    [sys.executable, "-u", "-c", code],
                    repo,
                    lease=lease,
                    harness="codex",
                    timeout_seconds=0.2,
                    source_repo_path=repo,
                    task_contract="escaped descendant must not pin process.wait",
                ),
                timeout=2,
            )
        )
    finally:
        if escaped_pid_path.exists():
            escaped_pid = int(escaped_pid_path.read_text(encoding="utf-8"))
            with contextlib.suppress(ProcessLookupError):
                os.kill(escaped_pid, signal.SIGKILL)

    assert result.capture.exit_code == 124
    assert result.checkpoint_reason == "deadline"
    assert result.checkpoint_id
    events = list_execution_events(lease.lease_id or "", limit=1000)["events"]
    kinds = [event["kind"] for event in events]
    assert kinds.count("process.sigkill") == 1
    assert kinds.count("process.wait_abandoned") == 1
    assert kinds.index("process.wait_abandoned") < kinds.index("process.exited")
    assert kinds.index("process.exited") < kinds.index("agent.finished")


def test_normal_exit_does_not_hang_when_escaped_descendant_holds_output_pipes(
    tmp_path: Path, monkeypatch
) -> None:
    lease = _lease(tmp_path)
    artifacts = _Artifacts()
    supervisor = StreamingCommandSupervisor(
        coordination_command=_coord,
        artifact_writer=artifacts,
        heartbeat_seconds=0.02,
        stream_drain_timeout_seconds=0.05,
    )
    escaped_pid_path = tmp_path / "escaped-child.pid"
    escaped_code = "import time; time.sleep(30)"
    code = (
        "from pathlib import Path; import json,subprocess,sys; "
        f"child=subprocess.Popen([sys.executable,'-u','-c',{escaped_code!r}], "
        "start_new_session=True); "
        "Path('escaped-child.pid').write_text(str(child.pid)); "
        "print(json.dumps({'type':'item.completed','item':"
        "{'type':'agent_message','text':'done'}}), flush=True)"
    )

    try:
        result = asyncio.run(
            asyncio.wait_for(
                supervisor.run(
                    [sys.executable, "-u", "-c", code],
                    tmp_path,
                    lease=lease,
                    harness="codex",
                    timeout_seconds=5,
                ),
                timeout=2,
            )
        )
    finally:
        if escaped_pid_path.exists():
            escaped_pid = int(escaped_pid_path.read_text(encoding="utf-8"))
            with contextlib.suppress(ProcessLookupError):
                os.kill(escaped_pid, signal.SIGKILL)

    assert result.capture.exit_code == 0
    assert result.checkpoint_id is None
    events = list_execution_events(lease.lease_id or "", limit=1000)["events"]
    kinds = [event["kind"] for event in events]
    assert kinds.count("stream.drain_abandoned") == 1
    assert kinds.index("stream.drain_abandoned") < kinds.index("process.exited")
    assert kinds.index("process.exited") < kinds.index("agent.finished")


_BENCH = repo_bench()
"""The seating this file's executor runs under, and the gate judges it by.

`CliPowWowExecutor` defaults to `DEFAULT_BENCH`, which is the no-config fallback
and is deliberately free to disagree with `configs/staffing.toml`, while
`capability_gate.policy_principal` resolves a spawned agent's vendor name to a
seat by reading that config. Taking the default put the fake in one seating and
judged it by another. Production has no such gap - `dispatcher_runner` builds the
executor with `load_bench(...)` - so the test passes the same bench in.
"""


def _senior_is(vendor: Harness) -> bool:
    """Whether that bench seats `vendor` as the implementer."""

    return _BENCH[DispatchTier.SENIOR].harness is vendor


def test_executor_preserves_worktree_when_checkpoint_persistence_fails(
    tmp_path: Path, monkeypatch
) -> None:
    set_root(str(tmp_path / "coordination"))
    repo = tmp_path / "target"
    repo.mkdir()
    _git_repo(repo)
    artifacts = _Artifacts()
    opened_worktree: list[Path] = []

    def coordinate(command: Any) -> Any:
        if isinstance(command, CreateExecutionCheckpoint):
            raise RuntimeError("checkpoint persistence unavailable")
        if isinstance(command, AppendExecutionEvent) and command.kind == "process.started":
            # The supervisor persists process.started after spawning the agent
            # and before it starts the deadline timer, and this callable runs
            # in a thread bounded by coordination_timeout_seconds (30s).
            # Blocking here until the fake claude signals readiness keeps
            # interpreter startup out of the 1s deadline budget, so machine
            # load cannot leave SURVIVED.txt unwritten or the SIGTERM guard
            # uninstalled when the deadline fires.
            gate_deadline = time.monotonic() + 20
            while not opened_worktree or not (opened_worktree[0] / "READY").exists():
                if time.monotonic() >= gate_deadline:
                    raise AssertionError("term-resistant-claude never signaled readiness")
                time.sleep(0.01)
        if isinstance(command, OpenExecutionLease):
            assert command.worktree_path is not None
            opened_worktree[:] = [Path(command.worktree_path)]
            payload = open_execution_lease(
                command.idempotency_key,
                command.worker_id,
                intent_id=command.intent_id,
                task_id=command.task_id,
                agent_tier=command.agent_tier,
                agent_name=command.agent_name,
                worktree_path=command.worktree_path,
                command_json=json.dumps(command.command),
                compensation_json=json.dumps(command.compensation),
                timeout_seconds=command.timeout_seconds,
            )
        elif isinstance(command, CompleteExecutionLease):
            payload = complete_execution_lease(
                command.lease_id,
                command.status.value,
                result_json=json.dumps(command.result),
                error=command.error,
            )
        else:
            return _coord(command)
        assert payload["ok"], payload
        return parse_coordination_result(command, payload)

    claude = tmp_path / "term-resistant-claude"
    claude.write_text(
        "#!/usr/bin/env python3\n"
        "import signal, sys, time\n"
        "from pathlib import Path\n"
        # This fake stands in for whichever vendor the bench seats as senior, and
        # a codex-seated spawn is preceded by `codex login status`.
        "if 'login' in sys.argv:\n"
        "    print('logged in')\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "Path('SURVIVED.txt').write_text('preserve me\\n', encoding='utf-8')\n"
        # The readiness sentinel stays in the leased worktree. The process
        # boundary correctly forbids the old test from writing it beside the
        # source repository. Observing it means the
        # SIGTERM guard and SURVIVED.txt are already in place.
        "Path('READY').write_text('ready\\n', encoding='utf-8')\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    claude.chmod(0o755)
    target = LinkedProject(
        id="checkpoint-failure-target",
        kind="test",
        path=repo,
        status="active",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="checkpoint persistence failure target",
        verification_commands=[],
    )
    context = PowWowExecutionContext(
        saga_id="checkpoint-failure-saga",
        goal="prove failed checkpoint persistence preserves the worktree",
        directive="integration proof",
        target_project_id=target.id,
        target_project_path=str(repo),
        target_project_kind=target.kind,
        target_project_status=target.status,
        target_project_read_only=False,
    )
    task = PowWowTaskSpec(
        task_name="checkpoint_failure_implementation",
        role="implementer",
        judgment=JudgmentRole(name="implementer", tier=DispatchTier.SENIOR),
        dispatch_kind=DispatchKind.CODE,
        description="write one file and wait past the deadline",
    )
    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        cleanup_policy="remove",
        timeout_seconds=1,
        coordination_command=coordinate,
        artifact_writer=artifacts,
        # This fake implements, so it goes to whichever vendor the bench seats
        # as senior; the other seat keeps its real binary name and is never
        # spawned by this pow-wow.
        bench=_BENCH,
        claude_bin=str(claude) if _senior_is(Harness.CLAUDE) else "claude",
        codex_bin=str(claude) if _senior_is(Harness.CODEX) else "codex",
        supervisor_factory=_FastTerminationSupervisor,
    )

    result = executor.dispatch_pow_wow(
        "checkpoint-failure-pow-wow",
        target,
        (task,),
        context,
    )
    run_artifact = next(
        artifact.content
        for artifact in result.tasks[0].artifacts
        if artifact.artifact_type == "cli_agent_run"
    )
    worktree_path = Path(run_artifact["worktree"]["worktree_path"])
    try:
        supervisor_payload = run_artifact["streaming_supervisor"]
        assert supervisor_payload["checkpoint_reason"] == "supervisor_error"
        assert supervisor_payload["checkpoint_id"] is None
        assert supervisor_payload["preserve_worktree"] is True
        assert "checkpoint persistence unavailable" in supervisor_payload["supervisor_error"]
        assert worktree_path.exists()
        assert (worktree_path / "SURVIVED.txt").read_text(encoding="utf-8") == ("preserve me\n")
        # The deadline, not agent exit, must end the run, and the resistant
        # agent must force SIGTERM through the grace window into SIGKILL.
        lease_id = run_artifact["execution_lease"]["lease_id"]
        kinds = [event["kind"] for event in list_execution_events(lease_id, limit=1000)["events"]]
        assert "deadline.reached" in kinds
        assert kinds.count("process.sigkill") == 1
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree_path)],
            check=True,
            capture_output=True,
            text=True,
        )


def test_operator_cancel_is_seen_on_heartbeat_and_checkpoints(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    lease = _lease(tmp_path)
    artifacts = _Artifacts()
    supervisor = StreamingCommandSupervisor(
        coordination_command=_coord,
        artifact_writer=artifacts,
        heartbeat_seconds=0.02,
        termination_grace_seconds=0.1,
    )

    async def exercise() -> Any:
        running = asyncio.create_task(
            supervisor.run(
                [sys.executable, "-u", "-c", "import time; time.sleep(10)"],
                repo,
                lease=lease,
                harness="codex",
                timeout_seconds=5,
                source_repo_path=repo,
                task_contract="bounded cancel test",
            )
        )
        await asyncio.sleep(0.08)
        request_execution_cancel(lease.lease_id or "", "operator requested stop", "test-operator")
        return await running

    result = asyncio.run(exercise())

    assert result.capture.exit_code == 130
    assert result.checkpoint_reason == "operator_cancel"
    assert result.checkpoint_id
    kinds = [item["kind"] for item in list_execution_events(lease.lease_id or "")["events"]]
    assert "operator_cancel.reached" in kinds


def test_normalizer_redacts_private_and_secret_fields() -> None:
    kind, payload = normalize_jsonl_line(
        harness="claude",
        source="stdout",
        line=json.dumps(
            {
                "type": "assistant",
                "token": "secret-token",
                "content": [
                    {"type": "thinking", "thinking": "private"},
                    {"type": "text", "text": "password=hunter2 visible"},
                ],
            }
        ).encode(),
    )

    assert kind == "assistant.message"
    assert payload["token"] == "[REDACTED]"
    serialized = json.dumps(payload)
    assert "private" not in serialized
    assert "hunter2" not in serialized


def test_only_visible_non_warning_stdout_counts_as_meaningful_progress() -> None:
    assert has_meaningful_agent_progress("stdout", "item.completed:agent_message") is True
    assert has_meaningful_agent_progress("lifecycle", "lease.heartbeat") is False
    assert has_meaningful_agent_progress("stderr", "stderr") is False
    assert has_meaningful_agent_progress("stdout", "deadline.warning") is False
    assert has_meaningful_agent_progress("stdout", "unknown") is False


def test_heartbeats_do_not_mask_stall_and_junior_can_continue(tmp_path: Path, monkeypatch) -> None:
    lease = _lease(tmp_path)
    calls: list[dict[str, object]] = []

    def assess(evidence: Mapping[str, object]) -> dict[str, object]:
        calls.append(dict(evidence))
        return {
            "recommendation": "CONTINUE",
            "rationale": "The process remains bounded; wait for its next visible result.",
            "continuations": [],
        }

    supervisor = StreamingCommandSupervisor(
        coordination_command=_coord,
        artifact_writer=_Artifacts(),
        heartbeat_seconds=0.01,
        quiet_seconds=0.03,
        stalled_seconds=0.07,
        progress_assessor=assess,
    )
    result = asyncio.run(
        supervisor.run(
            [
                sys.executable,
                "-u",
                "-c",
                (
                    "import json,time; "
                    "event={'type':'item.completed','item':"
                    "{'type':'agent_message','text':'same warning'}}; "
                    "print(json.dumps(event),flush=True); "
                    "print(json.dumps(event),flush=True); time.sleep(.14)"
                ),
            ],
            tmp_path,
            lease=lease,
            harness="codex",
            timeout_seconds=1,
            task_contract="review the patch",
        )
    )

    assert result.capture.exit_code == 0
    assert result.progress_recommendation == "CONTINUE"
    assert len(calls) == 1
    events = list_execution_events(lease.lease_id or "")["events"]
    kinds = [event["kind"] for event in events]
    assert "lease.heartbeat" in kinds
    assert kinds.index("activity.quiet") < kinds.index("activity.stalled_suspected")
    assert "progress_assessment.completed" in kinds
    assert kinds.count("activity.progress") == 2  # process start + first unique output
    leases = list_execution_leases()["leases"]
    current = next(item for item in leases if item["lease_id"] == lease.lease_id)
    assert current["activity_status"] == "STALLED_SUSPECTED"
    assert current["progress_assessment_status"] == "COMPLETED"
    assert current["progress_assessment_decision"]["recommendation"] == "CONTINUE"
    assert current["last_meaningful_progress_sequence"] is not None


def test_blocking_call_timeout_does_not_wait_for_daemon_thread() -> None:
    started = time.monotonic()

    async def call() -> None:
        try:
            await _execute_bounded_blocking_call(time.sleep, 2, timeout_seconds=0.05)
        except TimeoutError:
            return
        raise AssertionError("blocking call unexpectedly completed")

    asyncio.run(call())
    assert time.monotonic() - started < 0.5


def test_blocked_artifact_write_cannot_prevent_terminal_result(tmp_path: Path, monkeypatch) -> None:
    lease = _lease(tmp_path)
    supervisor = StreamingCommandSupervisor(
        coordination_command=_coord,
        artifact_writer=_BlockingArtifacts(),
        heartbeat_seconds=0.01,
        artifact_write_timeout_seconds=0.05,
    )
    started = time.monotonic()
    result = asyncio.run(
        supervisor.run(
            [sys.executable, "-u", "-c", "print('done', flush=True)"],
            tmp_path,
            lease=lease,
            harness="codex",
            timeout_seconds=1,
        )
    )

    assert time.monotonic() - started < 1
    assert result.capture.exit_code == 0
    assert result.transcript_artifact_id is None
    assert result.persistence_status.value == "FAILED"


def test_junior_checkpoint_recommendation_is_enforced_only_by_supervisor(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    lease = _lease(tmp_path)

    def assess(_: Mapping[str, object]) -> dict[str, object]:
        return {
            "recommendation": "CHECKPOINT",
            "rationale": "No visible progress and no working-tree delta.",
            "continuations": [],
        }

    supervisor = StreamingCommandSupervisor(
        coordination_command=_coord,
        artifact_writer=_Artifacts(),
        heartbeat_seconds=0.01,
        quiet_seconds=0.03,
        stalled_seconds=0.07,
        termination_grace_seconds=0.05,
        progress_assessor=assess,
    )
    result = asyncio.run(
        supervisor.run(
            [sys.executable, "-u", "-c", "import time; time.sleep(10)"],
            repo,
            lease=lease,
            harness="codex",
            timeout_seconds=2,
            source_repo_path=repo,
            task_contract="review the patch",
        )
    )

    assert result.capture.exit_code == 130
    assert result.checkpoint_reason == "stalled_progress"
    assert result.progress_recommendation == "CHECKPOINT"
    assert result.checkpoint_id
    kinds = [event["kind"] for event in list_execution_events(lease.lease_id or "")["events"]]
    assert "stalled_progress.reached" in kinds
    assert "checkpoint.created" in kinds


def test_a_provider_refusal_preserves_the_worktree_without_parking_the_intent(
    tmp_path: Path, monkeypatch
) -> None:
    """A quota refusal keeps its evidence and leaves the lifecycle alone.

    The run that motivated this lost a milestone's work twice over: the agent
    exited on its own, so none of the four checkpoint reasons applied, no
    snapshot was taken, and the worktree was removed by the default cleanup
    policy. The only record of how far the work got was the supervised event
    stream, which an earlier session had to reconstruct a diff out of by hand.

    The second half of the assertion is the part that took the most care.
    `create_execution_checkpoint` moves an intent out of CLAIMED and enqueues a
    junior review of it, so reusing the checkpoint path here would have parked a
    dispatch that is about to settle FAILED. The snapshot is evidence; the
    checkpoint is a lifecycle transition; only the first belongs to this case.
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    lease = _lease(tmp_path)
    artifacts = _Artifacts()
    supervisor = StreamingCommandSupervisor(
        coordination_command=_coord,
        artifact_writer=artifacts,
        heartbeat_seconds=0.02,
    )
    # Writes real work, then dies the way a refused provider dies: on its own,
    # nonzero, with the refusal on stderr.
    code = (
        "import sys; from pathlib import Path; "
        "Path('file.txt').write_text('half-finished work\\n'); "
        'sys.stderr.write("You\'ve hit your session limit \\u00b7 resets 3:10pm\\n"); '
        "sys.exit(1)"
    )

    result = asyncio.run(
        supervisor.run(
            [sys.executable, "-u", "-c", code],
            repo,
            lease=lease,
            harness="claude",
            timeout_seconds=30,
            source_repo_path=repo,
            task_contract="bounded test",
        )
    )

    assert result.agent_failure == "USAGE_LIMIT"
    assert result.preserve_worktree is True
    assert any(
        "half-finished work" in text for key, text in artifacts.contents.items() if "patch" in key
    )
    # No checkpoint row, so nothing moved the intent or asked for a review.
    assert result.checkpoint_reason is None
    assert result.checkpoint_id is None
    assert not [
        row
        for row in list_execution_checkpoints()["checkpoints"]
        if row["lease_id"] == lease.lease_id
    ]
    # The real exit survives; it is not relabelled as a timeout or a cancel.
    assert result.capture.exit_code == 1


def test_an_ordinary_failure_still_cleans_up_after_itself(tmp_path: Path, monkeypatch) -> None:
    """The narrowness is the point: only a refused provider keeps its tree.

    A run that failed on its own merits gets no snapshot and no preserved
    worktree, because the next attempt has nothing to learn from it and a
    worktree per failed run is disk that nobody reclaims.
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    lease = _lease(tmp_path)
    artifacts = _Artifacts()
    supervisor = StreamingCommandSupervisor(
        coordination_command=_coord,
        artifact_writer=artifacts,
        heartbeat_seconds=0.02,
    )
    code = "import sys; sys.stderr.write('AssertionError: the tests disagree\\n'); sys.exit(1)"

    result = asyncio.run(
        supervisor.run(
            [sys.executable, "-u", "-c", code],
            repo,
            lease=lease,
            harness="claude",
            timeout_seconds=30,
            source_repo_path=repo,
            task_contract="bounded test",
        )
    )

    assert result.agent_failure == "INTERNAL_ASSERTION"
    assert result.preserve_worktree is False
    assert result.checkpoint_id is None
