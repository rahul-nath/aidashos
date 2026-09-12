# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Checkpoint storage is independent from the process result it must retain."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from test_agent_execution_supervisor import _Artifacts, _coord, _git_repo, _lease

from local_first_agent_os.agent_execution_supervisor import (
    StreamingCommandSupervisor,
    SupervisedCommandResult,
)
from local_first_agent_os.coordination.checkpoints import (
    list_execution_artifacts,
    list_execution_checkpoints,
    list_execution_events,
)
from local_first_agent_os.coordination.contracts import (
    AcknowledgementResult,
    AppendExecutionEvent,
    CoordinationCommand,
    CoordinationResult,
    CreateExecutionCheckpoint,
    LedgerRecord,
    parse_coordination_result,
)
from local_first_agent_os.coordination.execution import request_execution_cancel
from local_first_agent_os.coordination.outcomes import (
    CheckpointReason,
    InfrastructureFailure,
    PersistenceStatus,
    SupervisorStatus,
)

_PROCESS = """from pathlib import Path
import signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print('original stdout evidence', flush=True)
print('original stderr evidence', file=sys.stderr, flush=True)
Path('file.txt').write_text('retained execution patch\\n')
Path('READY').touch()
time.sleep(30)
"""


def _supervise(
    tmp_path: Path,
    *,
    checkpoint: Callable[[CreateExecutionCheckpoint], CoordinationResult],
    reason: CheckpointReason | None,
    process_exits_before_checkpoint: bool = False,
    process_exit_code: int | None = None,
) -> tuple[SupervisedCommandResult, _Artifacts, Path, str]:
    repository = tmp_path / "worktree"
    repository.mkdir()
    _git_repo(repository)
    lease = _lease(tmp_path)
    assert lease.lease_id is not None
    lease_id = lease.lease_id
    artifacts = _Artifacts()

    def coordinate(command: CoordinationCommand) -> CoordinationResult:
        if isinstance(command, CreateExecutionCheckpoint):
            return checkpoint(command)
        if isinstance(command, AppendExecutionEvent) and command.kind == "process.started":
            ready_deadline = time.monotonic() + 10
            while not (repository / "READY").is_file():
                if time.monotonic() >= ready_deadline:
                    raise AssertionError("supervised evidence process did not become ready")
                time.sleep(0.01)
            if process_exits_before_checkpoint:
                time.sleep(0.05)
                raise RuntimeError("original startup-event persistence failure")
            if reason is CheckpointReason.OPERATOR_CANCEL:
                canceled = request_execution_cancel(lease_id, "explicit fixture cancellation")
                assert canceled["ok"], canceled
        return _coord(command)

    supervisor = StreamingCommandSupervisor(
        coordination_command=coordinate,
        artifact_writer=artifacts,
        heartbeat_seconds=0.02,
        warning_seconds=0.05,
        termination_grace_seconds=0.05,
    )
    result = asyncio.run(
        supervisor.run(
            (
                sys.executable,
                "-u",
                "-c",
                _PROCESS.replace("time.sleep(30)", f"raise SystemExit({process_exit_code})")
                if process_exit_code is not None
                else _PROCESS,
            ),
            repository,
            lease=lease,
            harness="codex",
            timeout_seconds=0.2,
            source_repo_path=repository,
            task_contract="retain checkpoint failure evidence",
        )
    )
    return result, artifacts, repository, lease_id


@pytest.mark.parametrize("reason", [CheckpointReason.DEADLINE, CheckpointReason.OPERATOR_CANCEL])
@pytest.mark.parametrize("failure", [RuntimeError, OSError, TimeoutError])
def test_checkpoint_write_failure_preserves_primary_execution_evidence(
    tmp_path: Path, reason: CheckpointReason, failure: type[Exception]
) -> None:
    def unavailable(command: CreateExecutionCheckpoint) -> CoordinationResult:
        raise failure("checkpoint persistence unavailable")

    result, artifacts, repository, lease_id = _supervise(
        tmp_path, checkpoint=unavailable, reason=reason
    )
    assert "original stdout evidence" in result.capture.stdout
    assert "original stderr evidence" in result.capture.stderr
    assert result.capture.exit_code == (124 if reason is CheckpointReason.DEADLINE else 130)
    assert result.checkpoint_reason is reason
    assert result.checkpoint_id is None
    assert "checkpoint preserved" not in result.capture.stderr
    assert result.persistence_status is PersistenceStatus.FAILED
    assert result.persistence_failure == InfrastructureFailure.CHECKPOINT_WRITE_FAILED.value
    assert result.supervisor_status is SupervisorStatus.COMPLETED
    assert result.supervisor_failure is None
    if reason is CheckpointReason.DEADLINE:
        assert result.agent_failure == InfrastructureFailure.DEADLINE_EXCEEDED.value
    assert result.preserve_worktree
    assert not result.allows_task_completion
    assert (repository / "file.txt").read_text() == "retained execution patch\n"
    assert result.transcript_artifact_id is not None
    assert "original stdout evidence" in artifacts.contents[result.transcript_artifact_id]
    assert "original stderr evidence" in artifacts.contents[result.transcript_artifact_id]
    assert len(result.checkpoint_artifact_ids) == 4
    attached = list_execution_artifacts(lease_id)["execution_artifacts"]
    assert {row["artifact_id"] for row in attached} == set(result.checkpoint_artifact_ids)
    events = list_execution_events(lease_id, limit=1000)["events"]
    kinds = [event["kind"] for event in events]
    assert "checkpoint.created" not in kinds
    failed = next(event for event in events if event["kind"] == "checkpoint.persist.failed")
    assert failed["payload"]["failure"] == InfrastructureFailure.CHECKPOINT_WRITE_FAILED.value
    assert list_execution_checkpoints()["checkpoints"] == []


@pytest.mark.parametrize("exit_code", [0, 71])
def test_exited_process_is_not_relabelled_as_canceled_when_checkpoint_write_fails(
    tmp_path: Path,
    exit_code: int,
) -> None:
    def unavailable(command: CreateExecutionCheckpoint) -> CoordinationResult:
        raise RuntimeError("checkpoint persistence unavailable")

    result, artifacts, _repository, _lease_id = _supervise(
        tmp_path,
        checkpoint=unavailable,
        reason=CheckpointReason.SUPERVISOR_ERROR,
        process_exits_before_checkpoint=True,
        process_exit_code=exit_code,
    )
    assert result.capture.exit_code == exit_code
    assert not result.allows_task_completion
    assert "original stdout evidence" in result.capture.stdout
    assert "original stderr evidence" in result.capture.stderr
    assert "process canceled" not in result.capture.stderr
    assert result.checkpoint_reason is CheckpointReason.SUPERVISOR_ERROR
    assert result.supervisor_status is SupervisorStatus.FAILED
    assert result.supervisor_failure is not None
    assert "original startup-event persistence failure" in result.supervisor_failure
    assert result.persistence_failure == InfrastructureFailure.CHECKPOINT_WRITE_FAILED.value
    assert result.transcript_artifact_id in artifacts.contents
    assert result.checkpoint_id is None


def test_successful_process_and_persistence_allow_task_completion(tmp_path: Path) -> None:
    result, _artifacts, _repository, _lease_id = _supervise(
        tmp_path, checkpoint=_coord, reason=None, process_exit_code=0
    )
    assert result.capture.exit_code == 0
    assert result.checkpoint_reason is None
    assert result.persistence_status is PersistenceStatus.COMPLETED
    assert result.allows_task_completion


@pytest.mark.parametrize("malformed", ["acknowledgement", "missing_identity", "blank_identity"])
def test_malformed_checkpoint_success_remains_a_contract_violation(
    tmp_path: Path, malformed: str
) -> None:
    def malformed_result(command: CreateExecutionCheckpoint) -> CoordinationResult:
        if malformed == "acknowledgement":
            return AcknowledgementResult(command.name, LedgerRecord({"ok": True}))
        return parse_coordination_result(
            command,
            {
                "ok": True,
                "checkpoint": {"checkpoint_id": " "} if malformed == "blank_identity" else {},
            },
        )

    with pytest.raises((TypeError, ValueError), match="checkpoint|Checkpoint"):
        _supervise(tmp_path, checkpoint=malformed_result, reason=CheckpointReason.DEADLINE)
