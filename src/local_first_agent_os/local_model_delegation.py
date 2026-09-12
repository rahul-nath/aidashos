# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded local-model delegation through the existing ModelManager owner."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import TYPE_CHECKING

from .constants import DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS
from .contracts import ArtifactRef, ArtifactRole, ModelCallRequest, ModelRole
from .coordination.failures import DurableFailureError, FailureV1

if TYPE_CHECKING:
    from .runtime import AppRuntime


@dataclass(frozen=True)
class LocalModelTask:
    """A local-model task with no CLI-only or unconsumed policy fields."""

    task_id: str
    prompt: str
    model_role: ModelRole
    task_max_tokens: int
    timeout_seconds: int = DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS
    workflow_id: str | None = None
    model_params: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("local-model task_id is required")
        if not self.prompt.strip():
            raise ValueError("local-model prompt is required")
        if self.task_max_tokens <= 0:
            raise ValueError("local-model task_max_tokens must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("local-model timeout_seconds must be positive")
        if "max_tokens" in self.model_params:
            raise ValueError("use task_max_tokens, not model_params['max_tokens']")
        object.__setattr__(self, "model_params", MappingProxyType(dict(self.model_params)))


@dataclass(frozen=True)
class LocalModelRunProvenance:
    model_role: ModelRole
    workflow_id: str
    model_id: str | None = None
    invocation_id: str | None = None
    prompt_artifact: ArtifactRef | None = None
    output_artifact: ArtifactRef | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "runtime": "local_model",
            "model_role": self.model_role.value,
            "workflow_id": self.workflow_id,
            "model_id": self.model_id,
            "invocation_id": self.invocation_id,
            "prompt_artifact_id": (
                self.prompt_artifact.artifact_id if self.prompt_artifact is not None else None
            ),
            "output_artifact_id": (
                self.output_artifact.artifact_id if self.output_artifact is not None else None
            ),
        }


@dataclass(frozen=True)
class LocalModelResult:
    """Local-model output with typed invocation provenance and retained failures."""

    task_id: str
    success: bool
    output: str
    provenance: LocalModelRunProvenance
    error: str | FailureV1 | None = None
    tokens_used: int = 0

    def __post_init__(self) -> None:
        if self.success == (self.error is not None):
            raise ValueError("a successful agent result has no error; a failure must name one")
        if self.tokens_used < 0:
            raise ValueError("agent tokens_used cannot be negative")

    @property
    def error_text(self) -> str | None:
        """Project human guidance without discarding an owned failure's typed cause."""

        if isinstance(self.error, FailureV1):
            return self.error.message or self.error.error_code
        return self.error


def _model_output_text(payload: object) -> str:
    """Render the documented model-output JSON shapes and reject arbitrary objects."""

    if isinstance(payload, str):
        return payload
    if isinstance(payload, Mapping):
        output = payload.get("output", payload)
        if isinstance(output, str):
            return output
        if isinstance(output, Mapping):
            text = output.get("text")
            if isinstance(text, str):
                return text
            return json.dumps(dict(output), ensure_ascii=False, sort_keys=True)
        if output is None or isinstance(output, (bool, int, float, list)):
            return json.dumps(output, ensure_ascii=False, sort_keys=True)
    raise TypeError(f"model output must be JSON data, got {type(payload).__name__}")


class LocalModelAdapter:
    """Run a task through ModelManager's selected local-inference backend."""

    def __init__(self, runtime: AppRuntime) -> None:
        self.runtime = runtime

    async def run(self, task: LocalModelTask) -> LocalModelResult:
        workflow_id = task.workflow_id or f"agent-task-{task.task_id}"
        requested = LocalModelRunProvenance(task.model_role, workflow_id)
        try:
            if task.workflow_id and not self.runtime.repository.workflow_run_exists(workflow_id):
                raise ValueError(f"Local delegate workflow_id {workflow_id!r} is not registered")
            prompt_artifact = self.runtime.artifact_store.write_text(
                role=ArtifactRole.PROMPT.value,
                text=task.prompt,
                workflow_id=workflow_id,
                schema_version="agent_task_prompt.v1",
            )
            requested = replace(requested, prompt_artifact=prompt_artifact)
            model_params = dict(task.model_params)
            model_params["max_tokens"] = task.task_max_tokens
            request = ModelCallRequest(
                workflow_id=workflow_id,
                model_role=task.model_role,
                input_artifact_id=prompt_artifact.artifact_id,
                payload={"prompt": task.prompt},
                params=model_params,
                timeout_seconds=task.timeout_seconds,
            )
            model_result = await asyncio.to_thread(self.runtime.model_manager.call_model, request)
            output_payload = self.runtime.artifact_store.read_json(
                model_result.output_artifact.artifact_id
            )
            provenance = LocalModelRunProvenance(
                model_role=model_result.model_role,
                workflow_id=workflow_id,
                model_id=model_result.model_id,
                invocation_id=model_result.invocation_id,
                prompt_artifact=prompt_artifact,
                output_artifact=model_result.output_artifact,
            )
            return LocalModelResult(
                task_id=task.task_id,
                success=True,
                output=_model_output_text(output_payload),
                provenance=provenance,
            )
        except Exception as exc:
            return LocalModelResult(
                task_id=task.task_id,
                success=False,
                output="",
                error=exc.failure if isinstance(exc, DurableFailureError) else str(exc),
                provenance=requested,
            )


__all__ = ["LocalModelAdapter", "LocalModelResult", "LocalModelRunProvenance", "LocalModelTask"]
