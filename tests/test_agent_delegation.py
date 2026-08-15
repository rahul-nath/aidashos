# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from local_first_agent_os.agent_adapters import AgentTask, LocalLlamaAdapter
from local_first_agent_os.constants import DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS
from local_first_agent_os.contracts import ModelCallRequest, ModelRole, SourceType, WorkspaceId
from local_first_agent_os.delegation import agent_result_payload, delegate_agent_task
from local_first_agent_os.ingress import normalize_scheduled_event
from local_first_agent_os.runtime import AppRuntime


def test_agent_and_model_request_defaults_allow_one_hour() -> None:
    task = AgentTask(
        task_id="default-timeout",
        pow_wow_id="pow-default-timeout",
        saga_id="saga-default-timeout",
        role="delegate",
        prompt="Confirm the default timeout.",
    )
    request = ModelCallRequest(
        workflow_id="workflow-default-timeout",
        model_role=ModelRole.GENERAL,
        input_artifact_id="artifact-default-timeout",
        payload={"prompt": "Confirm the default timeout."},
    )

    assert task.timeout_seconds == DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS == 3600
    assert request.timeout_seconds == DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS


def test_local_llama_adapter_uses_model_manager(runtime: AppRuntime) -> None:
    adapter = LocalLlamaAdapter(runtime, model_role="general")
    result = asyncio.run(
        adapter.run(
            AgentTask(
                task_id="task-1",
                pow_wow_id="pow-1",
                saga_id="saga-1",
                role="summarizer",
                prompt="Summarize this local model delegation seam.",
                max_tokens=64,
            )
        )
    )

    assert result.success is True
    assert "Mock local answer" in result.output
    assert result.metadata["adapter"] == "local_llama"
    assert result.metadata["model_role"] == "general"
    assert result.metadata["output_artifact_id"]


def test_delegate_agent_task_defaults_to_weak_local_route(runtime: AppRuntime) -> None:
    result = asyncio.run(
        delegate_agent_task(
            runtime,
            prompt="Classify this note.",
            tier="weak",
            model_role="general",
            max_tokens=32,
        )
    )
    payload = agent_result_payload(result)

    assert payload["ok"] is True
    assert payload["metadata"]["adapter"] == "local_llama"
    assert "Mock local answer" in payload["output"]


def test_delegate_agent_task_rejects_unregistered_explicit_workflow_id(
    runtime: AppRuntime,
) -> None:
    result = asyncio.run(
        delegate_agent_task(
            runtime,
            prompt="Draft a generated workflow.",
            adapter="local_llama",
            model_role="general",
            metadata={"workflow_id": "missing-workflow-run"},
            max_tokens=32,
        )
    )
    payload = agent_result_payload(result)

    assert payload["ok"] is False
    assert "not registered" in str(payload["error"])
    assert payload["metadata"] == {}


def test_delegate_agent_task_records_artifacts_for_registered_workflow(
    runtime: AppRuntime,
) -> None:
    workflow_id = "junior-delegate-e2e"
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="test.junior_delegate",
        payload={"prompt": "Draft a generated workflow."},
    )
    runtime.repository.register_ingress_event(event)
    runtime.repository.start_workflow_run(
        workflow_id=workflow_id,
        workflow_type="junior_delegate_e2e",
        workspace_id=WorkspaceId.GENERAL.value,
        input_event_id=event.event_id,
    )

    result = asyncio.run(
        delegate_agent_task(
            runtime,
            prompt="Draft a generated workflow.",
            adapter="local_llama",
            model_role="general",
            metadata={"workflow_id": workflow_id},
            max_tokens=32,
        )
    )
    payload = agent_result_payload(result)

    assert payload["ok"] is True
    output_artifact_id = payload["metadata"]["output_artifact_id"]
    output_ref = runtime.repository.get_artifact(output_artifact_id)
    assert output_ref is not None
    output = runtime.artifact_store.read_json(output_artifact_id)
    assert output["schema_version"] == "model_output.v1"
    assert output["model_role"] == "general"
    assert "Mock local answer" in output["output"]["text"]


def _load_coordination_module() -> Any:
    from local_first_agent_os.coordination import pow_wows

    return pow_wows


def test_mcp_delegate_task_emits_result_without_pow_wow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from local_first_agent_os.coordination.store import set_root

    mcp = _load_coordination_module()
    set_root(str(tmp_path))
    monkeypatch.setattr(
        mcp,
        "_run_delegate_task",
        lambda **_: {
            "ok": True,
            "task_id": "task-1",
            "output": "local result",
            "artifacts": [],
            "error": None,
            "tokens_used": 0,
            "metadata": {"adapter": "local_llama"},
        },
    )

    result = mcp.delegate_task("Summarize locally.", submit_result=False)

    assert result["ok"] is True
    assert result["output"] == "local result"
    assert result["submitted_artifact"] is None


def test_standalone_coordination_script_uses_project_settings_when_env_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from local_first_agent_os import settings as settings_module
    from local_first_agent_os.coordination import store as mcp

    monkeypatch.delenv("AGENT_COORDINATION_BACKEND", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_COORDINATION_BACKEND", raising=False)
    monkeypatch.delenv("AGENT_COORDINATION_DATABASE_URL", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_COORDINATION_DATABASE_URL", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        settings_module,
        "get_settings",
        lambda: SimpleNamespace(
            coordination_backend="postgres",
            coordination_database_url="postgresql+psycopg://example/coordination",
            database_url="postgresql+psycopg://example/app",
        ),
    )

    assert mcp.coordination_backend() == "postgres"
    # The coordination script uses psycopg directly, so it normalizes the
    # project's SQLAlchemy-style URL to psycopg's native PostgreSQL form.
    assert mcp.postgres_database_url() == "postgresql://example/coordination"
