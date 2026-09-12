# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real workflow routing with provider/effect sentinels, not a live DBOS restart."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

import local_first_agent_os.workflow.engine as workflow_engine
from local_first_agent_os.agent_query_retirement import (
    RETIRED_AGENT_QUERY_ALIASES,
    AgentQueryRetirement,
)
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
from local_first_agent_os.directives import TOP_LEVEL_DIRECTIVES, DirectiveParser
from local_first_agent_os.directives_help import explain_failure
from local_first_agent_os.ingress import normalize_scheduled_event
from local_first_agent_os.runtime import AppRuntime
from local_first_agent_os.workflow import WorkflowEngine, parse_workflow_from_payload
from local_first_agent_os.workflow.core import build_event_workflow_id


def _event(directive: str) -> IngressEvent:
    return normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": directive},
    )


@contextmanager
def _forbid_execution_and_writes(
    runtime: AppRuntime, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    attempts: list[str] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        attempts.append("provider invocation or durable mutation")
        raise AssertionError(attempts[-1])

    with monkeypatch.context() as guard:
        guard.setattr(subprocess, "Popen", forbidden)
        guard.setattr(asyncio, "create_subprocess_exec", forbidden)
        guard.setattr(runtime.model_manager, "call_model", forbidden)
        guard.setattr(runtime.repository, "register_ingress_event", forbidden)
        guard.setattr(runtime.repository, "start_workflow_run", forbidden)
        guard.setattr(runtime.repository, "update_workflow", forbidden)
        guard.setattr(runtime.artifact_store, "write_json", forbidden)
        guard.setattr(runtime.artifact_store, "write_text", forbidden)
        yield
    assert attempts == []


def _assert_retired(result: WorkflowResult) -> None:
    retirement = AgentQueryRetirement.RETIRED
    assert result.workflow_type is WorkflowType.AGENT_QUERY
    assert result.status is WorkflowStatus.FAILED_PERMANENT
    assert result.current_stage is Stage.COMPLETED
    assert result.artifacts == []
    assert result.egress_ids == []
    assert result.manual_review_reason == retirement.message
    assert result.help == retirement.help_payload()
    assert result.help is not None
    assert result.help["error_code"] == "AGENT_QUERY_RETIRED"
    assert result.manual_review_reason is not None
    assert "outside AiDashOS" in result.manual_review_reason
    assert "approved WorkUnit" in result.manual_review_reason


@pytest.mark.parametrize("alias", ("/claude", "/cc", "/codex"))
@pytest.mark.parametrize(
    "tail", (" explain this repository", "", " --model never-select-this-model", ' "unterminated')
)
def test_retired_aliases_refuse_through_public_workflow_routing(
    runtime: AppRuntime, monkeypatch: pytest.MonkeyPatch, alias: str, tail: str
) -> None:
    directive = alias + tail
    event = _event(directive)
    engine = WorkflowEngine(runtime)
    monkeypatch.setattr(workflow_engine, "engine", lambda: engine)
    # A retired request must not even need a valid provider-selection config.
    (runtime.settings.config_dir / "staffing.toml").write_text("invalid = [", encoding="utf-8")

    with _forbid_execution_and_writes(runtime, monkeypatch):
        parser = DirectiveParser(runtime.settings)
        spec = parser.parse(directive)
        assert spec.action == "agent_query"
        assert spec.alias == alias
        assert spec.query is None
        assert RETIRED_AGENT_QUERY_ALIASES <= TOP_LEVEL_DIRECTIVES
        assert explain_failure(parser, directive, "obsolete guidance").summary == (
            AgentQueryRetirement.RETIRED.message
        )
        result = parse_workflow_from_payload(
            WorkflowType.MODEL_DIRECTIVE.value, event.model_dump(mode="json")
        )

    _assert_retired(result)
    assert result.workflow_id == build_event_workflow_id(WorkflowType.AGENT_QUERY, event)
    assert not runtime.repository.workflow_run_exists(result.workflow_id)


@pytest.mark.parametrize(
    "previous_status", (WorkflowStatus.COMPLETED, WorkflowStatus.FAILED_PERMANENT)
)
def test_serialized_query_replay_refuses_without_rewriting_historical_data(
    runtime: AppRuntime,
    monkeypatch: pytest.MonkeyPatch,
    previous_status: WorkflowStatus,
) -> None:
    event = _event("/claude historical question")
    workflow_id = build_event_workflow_id(WorkflowType.AGENT_QUERY, event)
    runtime.repository.register_ingress_event(event)
    runtime.repository.start_workflow_run(
        workflow_id=workflow_id,
        workflow_type="agent_query",
        workspace_id=event.workspace_id,
        input_event_id=event.event_id,
    )
    record = {
        "schema_version": "agent_query_record.v2",
        "harness": "claude_code",
        "query": "historical question",
        "succeeded": previous_status is WorkflowStatus.COMPLETED,
        "transcript": {
            "resolution": "exact_session_id",
            "path": "/retained/transcripts/session.jsonl",
            "session_id": "historical-session",
        },
    }
    artifact = runtime.artifact_store.write_json(
        role="agent_query_record",
        payload=record,
        workflow_id=workflow_id,
        schema_version="agent_query_record.v2",
    )
    runtime.repository.update_workflow(workflow_id, status=previous_status, stage=Stage.COMPLETED)
    before = runtime.repository.get_workflow_run_state(workflow_id)
    assert before is not None
    assert before.workflow_type is WorkflowType("agent_query")
    assert ArtifactRole(artifact.role) is ArtifactRole.AGENT_QUERY_RECORD
    engine = WorkflowEngine(runtime)
    monkeypatch.setattr(workflow_engine, "engine", lambda: engine)

    with _forbid_execution_and_writes(runtime, monkeypatch):
        for _ in range(2):
            # This is the serialized dispatcher used by durable entrypoints.
            # Ordinary tests do not claim a running DBOS recovery worker.
            result = parse_workflow_from_payload("agent_query", event.model_dump(mode="json"))
            _assert_retired(result)
            assert result.workflow_id == workflow_id

    assert runtime.repository.get_workflow_run_state(workflow_id) == before
    assert runtime.artifact_store.read_json(artifact.artifact_id) == record
