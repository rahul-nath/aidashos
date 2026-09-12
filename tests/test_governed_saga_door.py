# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Retired routes refuse without changing governed work or retained workflow history."""

from __future__ import annotations

from typing import NoReturn

import pytest

import local_first_agent_os.workflow.engine as workflow_engine
from local_first_agent_os.contracts import (
    ArtifactRole,
    IngressEvent,
    SourceType,
    Stage,
    WorkflowResult,
    WorkflowStatus,
    WorkflowType,
    WorkspaceId,
)
from local_first_agent_os.coordination.store import tx
from local_first_agent_os.ingress import normalize_scheduled_event
from local_first_agent_os.runtime import AppRuntime
from local_first_agent_os.settings import Settings
from local_first_agent_os.workflow import WorkflowEngine, parse_workflow_from_payload
from local_first_agent_os.workflow.core import build_event_workflow_id
from local_first_agent_os.workflow.governed_door import RETIREMENT_DOC

RETIRED_DIRECTIVES = (
    "/start /approved-gawd historical-doc --target-project target",
    "/approve-most-recent",
)


def _event(directive: str) -> IngressEvent:
    return normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": directive},
    )


def _coordination_counts() -> dict[str, int]:
    tables = ("sagas", "gawd_docs", "saga_milestones", "approval_requests", "dispatch_intents")
    with tx() as connection:
        return {
            table: connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
            for table in tables
        }


def _forbid_retired_effects(runtime: AppRuntime, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    attempts: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        attempts.append("retired route attempted execution or durable mutation")
        raise AssertionError(attempts[-1])

    monkeypatch.setattr(workflow_engine, "run_coordination_command", forbidden)
    monkeypatch.setattr(runtime.repository, "register_ingress_event", forbidden)
    monkeypatch.setattr(runtime.repository, "start_workflow_run", forbidden)
    monkeypatch.setattr(runtime.repository, "update_workflow", forbidden)
    monkeypatch.setattr(runtime.artifact_store, "write_json", forbidden)
    monkeypatch.setattr(runtime.artifact_store, "write_text", forbidden)
    monkeypatch.setattr(runtime.model_manager, "call_model", forbidden)
    return attempts


def _assert_retired(result: WorkflowResult, event: IngressEvent) -> None:
    assert result.workflow_id == build_event_workflow_id(WorkflowType.MODEL_DIRECTIVE, event)
    assert result.workflow_type is WorkflowType.MODEL_DIRECTIVE
    assert result.status is WorkflowStatus.FAILED_PERMANENT
    assert result.current_stage is Stage.COMPLETED
    assert result.artifacts == []
    assert result.egress_ids == []
    assert result.manual_review_reason is not None
    assert "standalone saga door for governed work is retired" in result.manual_review_reason
    assert "agent-ledger compile_design_doc" in result.manual_review_reason
    assert "start_work_unit" in result.manual_review_reason
    assert RETIREMENT_DOC in result.manual_review_reason
    assert result.help is not None


@pytest.mark.parametrize("directive", RETIRED_DIRECTIVES)
@pytest.mark.parametrize("legacy_posture", [None, "open", "deprecated", "retired"])
def test_retired_routes_refuse_even_with_historical_settings(
    runtime: AppRuntime,
    monkeypatch: pytest.MonkeyPatch,
    directive: str,
    legacy_posture: str | None,
) -> None:
    if legacy_posture is None:
        monkeypatch.delenv("LOCAL_AGENT_GOVERNED_SAGA_DOOR", raising=False)
    else:
        monkeypatch.setenv("LOCAL_AGENT_GOVERNED_SAGA_DOOR", legacy_posture)
    # Reload through the actual settings boundary so old environment values
    # cannot revive authority in a fresh resident process.
    runtime.settings = Settings(**runtime.settings.model_dump())
    before = _coordination_counts()
    attempts = _forbid_retired_effects(runtime, monkeypatch)
    event = _event(directive)

    result = WorkflowEngine(runtime).model_directive(event)

    _assert_retired(result, event)
    assert attempts == []
    assert not runtime.repository.workflow_run_exists(result.workflow_id)
    assert _coordination_counts() == before


@pytest.mark.parametrize("directive", RETIRED_DIRECTIVES)
@pytest.mark.parametrize(
    "previous_status", (WorkflowStatus.COMPLETED, WorkflowStatus.FAILED_PERMANENT)
)
def test_replayed_retired_directive_preserves_historical_workflow_and_artifact(
    runtime: AppRuntime,
    monkeypatch: pytest.MonkeyPatch,
    directive: str,
    previous_status: WorkflowStatus,
) -> None:
    event = _event(directive)
    workflow_id = build_event_workflow_id(WorkflowType.MODEL_DIRECTIVE, event)
    runtime.repository.register_ingress_event(event)
    runtime.repository.start_workflow_run(
        workflow_id=workflow_id,
        workflow_type=WorkflowType.MODEL_DIRECTIVE.value,
        workspace_id=event.workspace_id,
        input_event_id=event.event_id,
    )
    retained_result = {
        "schema_version": "directive_result.v1",
        "directive": directive,
        "action": "approved_gawd",
        "status": (
            "approved_and_enqueued" if previous_status is WorkflowStatus.COMPLETED else "failed"
        ),
        "gawd_doc_id": "historical-doc",
        "dispatch_intent_id": "retained-intent",
    }
    artifact = runtime.artifact_store.write_json(
        role=ArtifactRole.DIRECTIVE_RESULT.value,
        payload=retained_result,
        workflow_id=workflow_id,
        schema_version="directive_result.v1",
    )
    runtime.repository.update_workflow(workflow_id, status=previous_status, stage=Stage.COMPLETED)
    before_state = runtime.repository.get_workflow_run_state(workflow_id)
    before_artifacts = runtime.repository.list_workflow_artifacts(workflow_id)
    before_coordination = _coordination_counts()
    assert before_state is not None
    assert before_state.status is previous_status
    assert before_artifacts == [artifact]
    monkeypatch.setattr(workflow_engine, "engine", lambda: WorkflowEngine(runtime))
    attempts = _forbid_retired_effects(runtime, monkeypatch)

    for _ in range(2):
        result = parse_workflow_from_payload(
            WorkflowType.MODEL_DIRECTIVE.value, event.model_dump(mode="json")
        )
        _assert_retired(result, event)

    assert attempts == []
    assert runtime.repository.get_workflow_run_state(workflow_id) == before_state
    assert runtime.repository.list_workflow_artifacts(workflow_id) == before_artifacts
    assert runtime.artifact_store.read_json(artifact.artifact_id) == retained_result
    assert _coordination_counts() == before_coordination
