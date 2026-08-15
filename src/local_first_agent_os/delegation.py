# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import uuid
from typing import Any

from .agent_adapters import (
    AgentAdapterRegistry,
    AgentResult,
    AgentTask,
    LocalLlamaAdapter,
)
from .constants import DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS


def agent_result_payload(result: AgentResult) -> dict[str, Any]:
    return {
        "ok": result.success,
        "task_id": result.task_id,
        "output": result.output,
        "artifacts": result.artifacts,
        "error": result.error,
        "tokens_used": result.tokens_used,
        "metadata": result.metadata,
    }


async def delegate_agent_task(
    runtime: Any,
    *,
    prompt: str,
    tier: str = "weak",
    adapter: str | None = None,
    model_role: str = "general",
    role: str = "delegate",
    pow_wow_id: str | None = None,
    saga_id: str | None = None,
    task_id: str | None = None,
    session_id: str | None = None,
    max_tokens: int = 2048,
    timeout_seconds: int = DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
    model_params: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> AgentResult:
    """Route a small task to an agent runtime.

    This is the package-level seam used by Pi, MCP, and future executor
    integration. Local model offload should go through this service rather
    than calling ModelManager directly from every harness.
    """
    task = AgentTask(
        task_id=task_id or str(uuid.uuid4()),
        pow_wow_id=pow_wow_id or "",
        saga_id=saga_id or "",
        role=role,
        prompt=prompt,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        session_id=session_id,
        metadata={
            **(metadata or {}),
            "model_role": model_role,
            "model_params": model_params or {},
        },
    )

    if adapter == "local_llama":
        return await LocalLlamaAdapter(runtime, model_role=model_role).run(task)

    registry = AgentAdapterRegistry.from_settings(runtime.settings, runtime)
    if adapter:
        selected = registry.get(adapter)
        result = await selected.run(task)
        result.metadata.setdefault("adapter", adapter)
        return result
    return await registry.route(tier, task)
