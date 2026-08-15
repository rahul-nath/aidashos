# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The COMPACTOR model, as the one callback that shortens a dependency block.

An executor assembling a spawned agent's prompt reaches the compactor through a
single sync callback, so "which model summarises" stays behind an injected
adapter rather than inside prompt construction. ``pow_wow/prompts.py`` is
imported by tests with no model, no database, and no runtime; it must not learn
about any of them to render a block.

The failure contract runs the other way from most adapters here. This one may
fail, and its caller is required to survive that: ``build_bounded_view_block``
falls back to truncation on any exception. So this module is free to raise
plainly - unregistered prompt, unreachable server, refused model, empty answer -
instead of encoding degraded outcomes it cannot decide the response to.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from .contracts import ModelRole, WorkflowStatus, WorkflowType, WorkspaceId
from .delegation import agent_result_payload, delegate_agent_task
from .pow_wow.views import ViewCompactionRequest, ViewCompactor
from .workflow.saga_support import run_coroutine_blocking

if TYPE_CHECKING:
    from .runtime import AppRuntime

DEPENDENCY_CONTEXT_COMPACTION_PROMPT = "dependency_context_compaction"

# One workflow row for every dependency-context compaction this machine ever
# runs, not one per pow-wow. `model_invocations.workflow_id` is NOT NULL
# REFERENCES workflow_runs, so the calls need a parent row to be recorded at
# all - but compaction is a detail of how one prompt was assembled, not a unit
# of work an operator traces, and the per-call provenance an operator would want
# already lands on the `model_invocations` and artifact rows underneath. A
# constant id keeps that parent findable instead of scattering it across
# generated ones.
COMPACTION_WORKFLOW_ID = "dependency-context-compaction"

# A summary is a fraction of an over-budget block, and llama.cpp will happily
# keep generating past the point of usefulness. The character budget is enforced
# by the caller regardless; this only stops a runaway generation from spending
# minutes producing text that will be discarded for exceeding it.
_COMPACTION_MAX_TOKENS = 4096

# Roughly four characters per token, the ratio `utils.estimate_tokens` rounds
# with, floored well under the compactor's 65k context window.
_CHARS_PER_TOKEN = 4

# The whole call is bounded to an advisory budget rather than inheriting
# DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS. The repo already made this decision for
# the same shape of call: constants.py says a progress assessment "is advisory
# and gets a model-sized budget of its own; it must not inherit the full
# frontier-agent hour", and a compaction is exactly that - an inline local-model
# call on the way to spawning an agent. The stall this bounds sits in
# build_agent_task_prompt, before the execution-attempt lease opens, so a
# wedged llama server would otherwise hold the tier slot invisibly for an hour
# with nothing supervising it. The caller's fallback makes a timeout cheap:
# truncation, the answer this codebase shipped before compaction existed.
DEPENDENCY_COMPACTION_TIMEOUT_SECONDS = 300


def build_dependency_context_compactor(runtime: AppRuntime) -> ViewCompactor:
    """A compactor backed by the resident COMPACTOR model.

    Registration of the parent workflow row is lazy and memoised because an
    executor is built at daemon start and most runs never overflow the
    dependency block: opening a workflow run for a compaction that never
    happens would put a row in the ledger describing nothing. The lock is
    load-bearing rather than an optimisation - tasks run on a thread pool, so
    several threads can reach an unregistered id at once, and
    ``start_workflow_run`` is read-then-insert rather than an upsert.

    The compactor is deliberately not wrapped in ``model_manager.loaded_session``.
    That context manager unloads a zero-warm-ttl role on exit, and the compactor
    is zero-ttl, so two concurrent compactions would have the first to finish
    unload the model out from under the second. ``call_model`` calls
    ``ensure_loaded`` itself and ``COMPACTOR`` is in ``ALWAYS_AUTOLOAD_ROLES``,
    which is what makes the model resident here without that hazard.
    """

    registered = False
    lock = threading.Lock()

    def ensure_workflow_registered() -> str:
        nonlocal registered
        if registered:
            return COMPACTION_WORKFLOW_ID
        with lock:
            if not registered:
                runtime.repository.start_workflow_run(
                    workflow_id=COMPACTION_WORKFLOW_ID,
                    workflow_type=WorkflowType.CONTEXT_COMPACTION.value,
                    workspace_id=WorkspaceId.GENERAL.value,
                    input_event_id=None,
                )
                # Terminal immediately, and unconditionally rather than only
                # when the insert happened. A CREATED row with no input event
                # is exactly what operator recovery sweeps into
                # FAILED_PERMANENT ("cannot recover workflow without an input
                # event id"), which would leave the ledger asserting a
                # permanent failure over a row that keeps accruing successful
                # model_invocations. This row is bookkeeping for a foreign
                # key, not a unit of work recovery could resume, so COMPLETED
                # is its honest resting state - and re-stamping it also heals
                # a ledger where an earlier sweep already got to it.
                runtime.repository.update_workflow(
                    COMPACTION_WORKFLOW_ID,
                    status=WorkflowStatus.COMPLETED,
                    clear_error=True,
                )
                registered = True
        return COMPACTION_WORKFLOW_ID

    def compact(request: ViewCompactionRequest) -> str:
        # Declining beats pretending under mock models. The mock returns the Pi
        # context-compaction schema for ModelRole.COMPACTOR - well-formed,
        # non-empty, under budget, and containing none of the dependency
        # content - so it sails through every fallback gate while destroying
        # the block it claims to summarise. mock_models ships true in
        # docker-compose.yml and k8s/kind/app.yaml, not only in tests. Raising
        # here lands in the caller's ordinary fallback: truncation, which
        # preserves the prefix of the real content.
        if runtime.settings.mock_models:
            raise RuntimeError(
                "dependency-context compaction declines under mock_models: the mock "
                "compactor output preserves none of the dependency content, and the "
                "truncation fallback does"
            )
        prompt_spec = runtime.pi_prompts.get(DEPENDENCY_CONTEXT_COMPACTION_PROMPT)
        prompt = prompt_spec.render(
            char_budget=str(request.char_limit),
            dependency_context=request.content,
        )
        workflow_id = ensure_workflow_registered()

        async def run_compaction() -> str:
            result = await delegate_agent_task(
                runtime,
                prompt=prompt,
                tier="weak",
                adapter="local_llama",
                model_role=ModelRole.COMPACTOR.value,
                role="dependency_context_compactor",
                max_tokens=min(
                    _COMPACTION_MAX_TOKENS,
                    max(1, request.char_limit // _CHARS_PER_TOKEN),
                ),
                timeout_seconds=DEPENDENCY_COMPACTION_TIMEOUT_SECONDS,
                model_params={"temperature": 0},
                metadata={
                    "workflow_id": workflow_id,
                    "view_source": request.source,
                    "char_limit": request.char_limit,
                    "prompt_schema_version": prompt_spec.version,
                },
            )
            payload = agent_result_payload(result)
            # The local adapter reports a failed model call as a payload rather
            # than an exception, so the one path that must not silently return
            # an empty summary is turned back into a raise here. An empty string
            # would read to the caller as a compactor that succeeded at saying
            # nothing.
            if not payload.get("ok"):
                raise RuntimeError(
                    f"Dependency-context compaction failed: {payload.get('error') or 'no output'}"
                )
            return str(payload.get("output") or "")

        return run_coroutine_blocking(run_compaction())

    return compact


__all__ = [
    "COMPACTION_WORKFLOW_ID",
    "DEPENDENCY_COMPACTION_TIMEOUT_SECONDS",
    "DEPENDENCY_CONTEXT_COMPACTION_PROMPT",
    "build_dependency_context_compactor",
]
