# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path

import pytest
from test_agent_execution_supervisor import _Artifacts, _lease

from local_first_agent_os.coordination.contracts import (
    CoordinationCommand,
    CoordinationCommandName,
    CoordinationResult,
    CreateExecutionCheckpoint,
    EntityResult,
    LedgerRecord,
)
from local_first_agent_os.coordination.outcomes import InfrastructureFailure, PersistenceStatus
from local_first_agent_os.pow_wow import CliPowWowExecutor
from local_first_agent_os.process_containment import ContainedProcess
from local_first_agent_os.spawn_authority import UnattendedImplementation


@pytest.mark.parametrize("checkpoint_available", [False, True])
def test_fallback_persistence_requires_checkpoint_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkpoint_available: bool
) -> None:
    artifacts = _Artifacts()
    lease = _lease(tmp_path / "coordination")

    def coordinate(command: CoordinationCommand) -> CoordinationResult:
        assert isinstance(command, CreateExecutionCheckpoint)
        if not checkpoint_available:
            raise OSError("checkpoint storage unavailable")
        return EntityResult(
            command=CoordinationCommandName.CREATE_EXECUTION_CHECKPOINT,
            field="checkpoint",
            entity=LedgerRecord({"checkpoint_id": "acknowledged-checkpoint"}),
            metadata=LedgerRecord({}),
        )

    def fail_to_prepare(
        *args: object, **kwargs: object
    ) -> AbstractContextManager[ContainedProcess]:
        raise OSError("worker launch unavailable")

    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        coordination_command=coordinate,
        artifact_writer=artifacts,
    )
    monkeypatch.setattr(executor, "_prepared_frontier_process", fail_to_prepare)
    capture, result = executor._run_frontier_command(
        ("unstarted-worker",),
        tmp_path,
        execution_attempt=lease,
        harness="codex",
        env=None,
        source_repo_path=None,
        base_head_sha=None,
        saga_id="fallback-evidence-saga",
        pow_wow_id="fallback-evidence-pow-wow",
        task_contract="preserve launch failure and checkpoint persistence separately",
        posture=UnattendedImplementation(),
    )
    assert result is not None
    assert capture.exit_code != 0
    assert "worker launch unavailable" in capture.stderr
    assert result.preserve_worktree is True
    assert result.transcript_artifact_id is not None
    if checkpoint_available:
        assert result.checkpoint_id == "acknowledged-checkpoint"
        assert result.persistence_status is PersistenceStatus.COMPLETED
        assert result.persistence_failure is None
    else:
        assert result.checkpoint_id is None
        assert result.persistence_status is PersistenceStatus.FAILED
        assert result.persistence_failure == InfrastructureFailure.CHECKPOINT_WRITE_FAILED
        assert "checkpoint storage unavailable" in capture.stderr
