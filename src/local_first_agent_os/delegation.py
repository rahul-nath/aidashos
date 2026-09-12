# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from .constants import (
    DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
    DEFAULT_DELEGATED_TASK_MAX_TOKENS,
)
from .contracts import ModelRole
from .coordination.failures import FailureV1
from .local_model_delegation import LocalModelAdapter, LocalModelResult, LocalModelTask

if TYPE_CHECKING:
    from .runtime import AppRuntime


def agent_result_payload(result: LocalModelResult) -> dict[str, Any]:
    artifact_ids = [
        artifact.artifact_id
        for artifact in (
            result.provenance.prompt_artifact,
            result.provenance.output_artifact,
        )
        if artifact is not None
    ]
    return {
        "ok": result.success,
        "task_id": result.task_id,
        "output": result.output,
        "artifact_ids": artifact_ids,
        "error": result.error_text,
        "error_code": result.error.error_code if isinstance(result.error, FailureV1) else None,
        "tokens_used": result.tokens_used,
        "provenance": result.provenance.to_payload(),
    }


async def delegate_local_model_task(
    runtime: AppRuntime,
    *,
    prompt: str,
    model_role: ModelRole = ModelRole.GENERAL,
    task_id: str | None = None,
    task_max_tokens: int = DEFAULT_DELEGATED_TASK_MAX_TOKENS,
    timeout_seconds: int = DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
    model_params: Mapping[str, object] | None = None,
    workflow_id: str | None = None,
) -> LocalModelResult:
    """Run a bounded task on the selected local-model role.

    Runtime-backend selection belongs to ModelManager and the installed model
    configuration. This boundary cannot silently replace local work with a
    frontier request.
    """
    task = LocalModelTask(
        task_id=task_id or str(uuid.uuid4()),
        prompt=prompt,
        model_role=model_role,
        task_max_tokens=task_max_tokens,
        timeout_seconds=timeout_seconds,
        workflow_id=workflow_id,
        model_params=model_params or {},
    )
    return await LocalModelAdapter(runtime).run(task)
