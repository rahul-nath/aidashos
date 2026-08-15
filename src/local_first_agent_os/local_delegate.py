# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The local model, as the one callback an executor holds to reach it.

The junior tier does not run a CLI. It answers from the served local model
through ``delegate_agent_task``, and the executor reaches that through a single
sync callback so "which model answers" stays behind an injected adapter instead
of inside the scheduler.

This used to exist only as a ``WorkflowEngine`` method, which meant the only way
to get a working local junior was to be inside a Pi directive. The resident
dispatcher is not, so it built its runner with no delegate at all; every junior
task it claimed fell past the delegate branch into the frontier CLI path and was
launched as ``claude --model gemma4``. Extracting the callback is what lets the
resident path have a real one.

The two builders differ in exactly one decision: who owns the ``workflow_runs``
row a model call is recorded against. A directive already has one and passes it
down. The resident dispatcher has none, so it opens one. That row is not
bookkeeping - ``model_invocations.workflow_id`` is ``NOT NULL REFERENCES
workflow_runs``, so a delegate with no registered workflow cannot record that it
called a model at all, and the adapter refuses an unregistered id rather than
writing a dangling one.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from .contracts import ModelRole, WorkflowType, WorkspaceId
from .pow_wow.types import DelegateFn
from .workflow.saga_support import run_coroutine_blocking

if TYPE_CHECKING:
    from .runtime import AppRuntime

# One workflow row per pow-wow, not per task. The pow-wow is the unit of work a
# reader traces, several delegated calls belong to it, and a row per call would
# grow `workflow_runs` at the rate of model invocations for no added provenance.
_RESIDENT_WORKFLOW_ID_PREFIX = "resident-local-delegate"

# Delegate calls that carry no pow-wow (the progress assessor and the review
# convergence classifier are asked outside any one task) still need a row the FK
# accepts. They share this one, and the name says so rather than implying a
# pow-wow that does not exist.
_UNSCOPED_POW_WOW = "unscoped"


# `(pow_wow_id, task_name) -> a workflow_runs id that exists by the time it
# returns`. The callers differ only here, so this is the whole seam.
type WorkflowIdFor = Callable[[str, str], str]


def resident_delegate_workflow_id(pow_wow_id: str) -> str:
    """The workflow id a resident delegate records one pow-wow's calls against."""

    return f"{_RESIDENT_WORKFLOW_ID_PREFIX}:{pow_wow_id or _UNSCOPED_POW_WOW}"


def build_directive_local_delegate(runtime: AppRuntime, *, workflow_id: str) -> DelegateFn:
    """The delegate a Pi directive uses.

    The directive already registered a workflow for itself, so every model call
    the delegate makes is recorded against it and the artifacts land inside the
    directive's own provenance.
    """

    return _build_local_delegate(runtime, workflow_id_for=lambda _pow_wow, _task: workflow_id)


def build_resident_local_delegate(runtime: AppRuntime) -> DelegateFn:
    """The delegate the resident dispatcher uses.

    Nothing upstream of a claimed dispatch intent registered a workflow run, so
    this opens one per pow-wow on first use.

    The lock and the memo are load-bearing rather than an optimisation. Junior
    tasks run on a thread pool sized by the bench slot's capacity, four by
    default, so several threads reach the same unregistered pow-wow at once and
    ``start_workflow_run`` is read-then-insert, not an upsert. Serialising the
    first registration is what keeps that from being a duplicate-key race the
    adapter would report as a failed junior task.
    """

    registered: set[str] = set()
    lock = threading.Lock()

    def workflow_id_for(pow_wow_id: str, _task_name: str) -> str:
        workflow_id = resident_delegate_workflow_id(pow_wow_id)
        if workflow_id in registered:
            return workflow_id
        with lock:
            if workflow_id not in registered:
                runtime.repository.start_workflow_run(
                    workflow_id=workflow_id,
                    workflow_type=WorkflowType.RESIDENT_LOCAL_DELEGATE.value,
                    workspace_id=WorkspaceId.GENERAL.value,
                    input_event_id=None,
                )
                registered.add(workflow_id)
        return workflow_id

    return _build_local_delegate(runtime, workflow_id_for=workflow_id_for)


def _build_local_delegate(runtime: AppRuntime, *, workflow_id_for: WorkflowIdFor) -> DelegateFn:
    """The shared body: resolve the served model to a role, then run the task."""

    from .delegation import agent_result_payload, delegate_agent_task

    def delegate(
        *,
        prompt: str,
        task_name: str = "",
        role: str = "delegate",
        tier: str = "junior",
        model: str | None = None,
        model_params: Mapping[str, Any] | None = None,
        timeout_seconds: int | float | None = None,
        pow_wow_id: str = "",
        **_: Any,
    ) -> Mapping[str, Any]:
        # The bench slot names a served model (e.g. 'gemma4'); resolve it to the
        # model role the ModelManager routes by. Falls back to GENERAL.
        model_role = ModelRole.GENERAL
        if model:
            resolved = runtime.model_registry.role_for_server_name(model)
            if resolved is not None:
                model_role = resolved
        workflow_id = workflow_id_for(pow_wow_id, task_name)

        async def _execute_delegated_agent_task() -> Mapping[str, Any]:
            result = await delegate_agent_task(
                runtime,
                prompt=prompt,
                tier="weak",
                adapter="local_llama",
                model_role=model_role.value,
                role=role,
                model_params=dict(model_params or {}),
                timeout_seconds=int(timeout_seconds or runtime.settings.saga_task_timeout_seconds),
                metadata={
                    "workflow_id": workflow_id,
                    "tier": tier,
                    "task_name": task_name,
                    "requested_model": model,
                    "resolved_model_role": model_role.value,
                },
            )
            return agent_result_payload(result)

        return run_coroutine_blocking(_execute_delegated_agent_task())

    return delegate


__all__ = [
    "WorkflowIdFor",
    "build_directive_local_delegate",
    "build_resident_local_delegate",
    "resident_delegate_workflow_id",
]
