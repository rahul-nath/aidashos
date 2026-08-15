# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import pytest

from local_first_agent_os.contracts import (
    ModelRole,
    SourceType,
    Stage,
    WorkflowResult,
    WorkflowStatus,
    WorkflowType,
    WorkspaceId,
)
from local_first_agent_os.dbos_app import resume_pending_workflows
from local_first_agent_os.ingress import normalize_scheduled_event
from local_first_agent_os.runtime import build_runtime
from local_first_agent_os.settings import Settings
from local_first_agent_os.workflow import WorkflowEngine


def _directive_event(directive: str):
    return normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": directive},
    )


def test_fallback_hard_fails_when_no_embeddings_present(runtime, monkeypatch) -> None:
    """When the default model fails to load and there is nothing in the
    embedding store to fall back to, /start surfaces a hard failure."""

    def explode(role):
        if role == ModelRole.GENERAL:
            raise RuntimeError("simulated default-model load failure")

    monkeypatch.setattr(runtime.model_manager, "ensure_loaded", explode)
    result = WorkflowEngine(runtime).model_directive(_directive_event("/start /qwen"))
    assert result.status == WorkflowStatus.FAILED_PERMANENT
    assert result.help is not None
    assert runtime.model_manager.is_default_fallback_active() is False


def test_fallback_persists_across_runtime_rebuild(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'fallback.sqlite3'}",
        artifact_root=tmp_path / "artifacts",
        spool_dir=tmp_path / "spool",
        config_dir=tmp_path / "configs",
        mock_models=True,
        use_dbos=False,
    )
    first = build_runtime(settings)
    first.model_manager.activate_default_fallback(
        ModelRole.GENERAL_FALLBACK, "first-process: default offline"
    )
    assert first.model_manager.is_default_fallback_active() is True

    second = build_runtime(settings)
    assert second.model_manager.is_default_fallback_active() is True
    assert second.model_manager.default_role_fallback == ModelRole.GENERAL_FALLBACK
    assert second.model_manager.default_fallback_reason == "first-process: default offline"

    second.model_manager.clear_default_fallback()
    third = build_runtime(settings)
    assert third.model_manager.is_default_fallback_active() is False


def test_active_general_role_persists_across_runtime_rebuild(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'active-general.sqlite3'}",
        artifact_root=tmp_path / "artifacts",
        spool_dir=tmp_path / "spool",
        config_dir=tmp_path / "configs",
        mock_models=True,
        use_dbos=False,
    )
    first = build_runtime(settings)
    result = WorkflowEngine(first).model_directive(_directive_event("/start /qwen"))
    assert result.status == WorkflowStatus.COMPLETED
    assert first.model_manager.effective_general_role() == ModelRole.GENERAL_FALLBACK

    second = build_runtime(settings)
    assert second.model_manager.effective_general_role() == ModelRole.GENERAL_FALLBACK


def test_done_directive_records_fallback_when_default_unavailable(runtime, tmp_path: Path) -> None:
    note_dir = tmp_path / "notes"
    note_dir.mkdir()
    (note_dir / "a.md").write_text("Pi keeps DBOS-aware boundaries.", encoding="utf-8")
    WorkflowEngine(runtime).model_directive(_directive_event(f"/store {note_dir}"))
    runtime.model_manager.activate_default_fallback(ModelRole.GENERAL_FALLBACK, "default offline")
    result = WorkflowEngine(runtime).model_directive(_directive_event("/done dbos"))
    assert result.workflow_type == WorkflowType.DONE_RECALL
    assert result.status == WorkflowStatus.COMPLETED
    payloads = [a for a in result.artifacts if str(a.role) == "done_recall_result"]
    assert payloads
    payload = runtime.artifact_store.read_json(payloads[0].artifact_id)
    assert payload["fallback_active"] is True
    assert "Default model unavailable" in payload["aggregated_answer"]


# ---------------------------------------------------------------------------
# Explicit recovery of legacy application workflow runs
# ---------------------------------------------------------------------------
#
# `resume_pending_workflows` selects rows that are still CREATED or PROCESSING.
# Its contract is that no row it selects stays in that live state: recovery is
# an operator action whose whole point is that the ledger stops lying about
# what is running. These tests drive the entry point itself, because the old
# ones replayed a workflow by hand and so could not observe that settlement.


@pytest.fixture()
def recovering_runtime(runtime, monkeypatch: pytest.MonkeyPatch):
    """Point every process-wide runtime lookup at the disposable test runtime."""

    monkeypatch.setattr("local_first_agent_os.runtime.get_runtime", lambda: runtime)
    monkeypatch.setattr("local_first_agent_os.workflow.engine.get_runtime", lambda: runtime)
    return runtime


def _stall_workflow_run(
    runtime,
    workflow_id: str,
    *,
    workflow_type: WorkflowType,
    input_event_id: str | None,
) -> None:
    """Leave a workflow row in the live state a crashed process would leave."""

    runtime.repository.start_workflow_run(
        workflow_id=workflow_id,
        workflow_type=workflow_type.value,
        workspace_id=WorkspaceId.GENERAL.value,
        input_event_id=input_event_id,
    )
    runtime.repository.update_workflow(
        workflow_id, status=WorkflowStatus.PROCESSING, stage=Stage.PROCESSING
    )


def test_recovery_settles_the_stale_row_it_replayed(recovering_runtime, tmp_path: Path) -> None:
    """A crashed `/store` directive: the row is PROCESSING, the event survived.

    Recovery replays it and copies the terminal result back onto the original
    row, so the run that was reported as in-flight is now reported as done.
    """

    note = tmp_path / "n.md"
    note.write_text("Pi remembers durable workflows.", encoding="utf-8")
    event = _directive_event(f"/store {note}")
    recovering_runtime.repository.register_ingress_event(event)
    workflow_id = "directory_embedding:general:manual:simulated:v1"
    _stall_workflow_run(
        recovering_runtime,
        workflow_id,
        workflow_type=WorkflowType.DIRECTORY_EMBEDDING,
        input_event_id=event.event_id,
    )

    assert resume_pending_workflows() == [workflow_id]

    state = recovering_runtime.repository.get_workflow_run_state(workflow_id)
    assert state is not None
    assert state.status == WorkflowStatus.COMPLETED
    assert state.last_error is None
    assert not recovering_runtime.repository.list_pending_workflow_runs()


def test_recovery_fails_permanently_when_the_input_event_is_gone(recovering_runtime) -> None:
    """Nothing can replay a workflow whose input no longer exists.

    Leaving it PROCESSING would keep an unrecoverable run in the pending set
    forever, so it is a permanent failure with the missing id named.
    """

    _stall_workflow_run(
        recovering_runtime,
        "orphan-1",
        workflow_type=WorkflowType.GENERAL_QUESTIONS,
        input_event_id="missing-event-id",
    )

    assert resume_pending_workflows() == []

    state = recovering_runtime.repository.get_workflow_run_state("orphan-1")
    assert state is not None
    assert state.status == WorkflowStatus.FAILED_PERMANENT
    assert state.last_error is not None
    assert "missing-event-id" in state.last_error
    assert not recovering_runtime.repository.list_pending_workflow_runs()


def test_recovery_fails_retryably_when_the_replay_raises(
    recovering_runtime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A replay that blows up is a retryable failure with a visible cause."""

    event = _directive_event(f"/store {tmp_path}")
    recovering_runtime.repository.register_ingress_event(event)
    _stall_workflow_run(
        recovering_runtime,
        "exploding-1",
        workflow_type=WorkflowType.DIRECTORY_EMBEDDING,
        input_event_id=event.event_id,
    )

    def explode(workflow_type, replayed_event):
        raise RuntimeError("simulated replay failure")

    monkeypatch.setattr("local_first_agent_os.dbos_app.run_workflow", explode)

    assert resume_pending_workflows() == []

    state = recovering_runtime.repository.get_workflow_run_state("exploding-1")
    assert state is not None
    assert state.status == WorkflowStatus.FAILED_RETRYABLE
    assert state.retry_count == 1
    assert state.last_error is not None
    assert "simulated replay failure" in state.last_error
    assert not recovering_runtime.repository.list_pending_workflow_runs()


def test_recovery_refuses_a_replay_that_stays_non_terminal(
    recovering_runtime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A replay that returns PROCESSING has not recovered anything.

    Copying that status back would re-select the same row on the next pass, so
    the recovery pass treats it as a failed replay instead.
    """

    event = _directive_event(f"/store {tmp_path}")
    recovering_runtime.repository.register_ingress_event(event)
    _stall_workflow_run(
        recovering_runtime,
        "still-processing-1",
        workflow_type=WorkflowType.DIRECTORY_EMBEDDING,
        input_event_id=event.event_id,
    )

    def never_settles(workflow_type, replayed_event):
        return WorkflowResult(
            workflow_id="still-processing-1",
            workflow_type=workflow_type,
            status=WorkflowStatus.PROCESSING,
            current_stage=Stage.PROCESSING,
        )

    monkeypatch.setattr("local_first_agent_os.dbos_app.run_workflow", never_settles)

    assert resume_pending_workflows() == []

    state = recovering_runtime.repository.get_workflow_run_state("still-processing-1")
    assert state is not None
    assert state.status == WorkflowStatus.FAILED_RETRYABLE
    assert state.last_error is not None
    assert "non-terminal" in state.last_error


def test_recovery_leaves_no_run_in_a_live_state(
    recovering_runtime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole-pass invariant, stated once: recovery empties the pending set.

    A single pass over one row of every kind must leave nothing behind for the
    next pass to re-select.
    """

    event = _directive_event(f"/store {tmp_path}")
    recovering_runtime.repository.register_ingress_event(event)
    _stall_workflow_run(
        recovering_runtime,
        "mixed-replayable",
        workflow_type=WorkflowType.DIRECTORY_EMBEDDING,
        input_event_id=event.event_id,
    )
    _stall_workflow_run(
        recovering_runtime,
        "mixed-orphaned",
        workflow_type=WorkflowType.GENERAL_QUESTIONS,
        input_event_id="missing-event-id",
    )
    _stall_workflow_run(
        recovering_runtime,
        "mixed-exploding",
        workflow_type=WorkflowType.CONTEXT_COMPACTION,
        input_event_id=event.event_id,
    )

    def settle_or_explode(workflow_type, replayed_event):
        if workflow_type == WorkflowType.CONTEXT_COMPACTION:
            raise RuntimeError("simulated replay failure")
        return WorkflowResult(
            workflow_id="mixed-replayable",
            workflow_type=workflow_type,
            status=WorkflowStatus.COMPLETED,
            current_stage=Stage.ARTIFACT_PERSISTED,
        )

    monkeypatch.setattr("local_first_agent_os.dbos_app.run_workflow", settle_or_explode)

    resume_pending_workflows()

    assert not recovering_runtime.repository.list_pending_workflow_runs()
