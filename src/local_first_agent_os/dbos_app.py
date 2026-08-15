# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import contextlib
import os
import sys
import threading
from typing import Any

from . import (
    live_integration_checks,  # noqa: F401  -- registers its DBOS workflow
    workflowy_refresh,  # noqa: F401  -- registers its DBOS workflow
)
from ._dbos_runtime import DBOS, SetWorkflowID, dbos_workflow
from .contracts import IngressEvent, WorkflowStatus, WorkflowType
from .coordination import durable as coordination_durable  # noqa: F401
from .settings import get_settings

# Registration happens when the decorator runs, so a module whose workflows DBOS
# must know about has to be imported here. Without this the WorkUnit workflows
# existed only in a process that happened to import api.py, and a worker that
# launched DBOS for recovery came up not knowing they existed.
from .work_units import root_workflow as work_unit_root_workflow  # noqa: F401
from .workflow import run_workflow

_dbos_launched = False
settings = get_settings()


@dbos_workflow()
def durable_workflow_entrypoint(
    workflow_type: str,
    event_payload: dict[str, Any],
) -> dict[str, Any]:
    """DBOS workflow entrypoint.

    The body runs inside the workflow context, so any `@dbos_step`-decorated
    leaf method called transitively (ingress register, model invocation, pi
    decision, artifact write, egress create/update) is checkpointed
    independently. The workflow ID is supplied by `SetWorkflowID` on the
    caller side and is content-addressed, so re-entry across restarts hits
    the same workflow record.
    """

    event = IngressEvent.model_validate(event_payload)
    result = run_workflow(WorkflowType(workflow_type), event)
    return result.model_dump(mode="json")


def start_durable_workflow(workflow_type: WorkflowType, event: IngressEvent) -> str:
    from .ids import build_workflow_id, sha256_text

    workflow_id = build_workflow_id(
        workflow_type,
        event.workspace_id,
        event.source_type,
        event.content_sha256 or sha256_text(event.model_dump_json()),
    )
    if not settings.use_dbos or DBOS is None or SetWorkflowID is None:
        run_workflow(workflow_type, event)
        return workflow_id
    with SetWorkflowID(workflow_id):
        DBOS.start_workflow(
            durable_workflow_entrypoint,
            workflow_type.value,
            event.model_dump(mode="json"),
        )
    return workflow_id


def launch_dbos() -> None:
    global _dbos_launched
    if _dbos_launched:
        return
    if settings.use_dbos and DBOS is not None:
        try:
            DBOS.launch()
        except Exception:
            # If launch fails (already launched, missing system DB, etc.), fall
            # back to the direct path so Pi keeps working.
            return
        _dbos_launched = True


def shutdown_dbos() -> None:
    """Stop the runtime this process launched, so the interpreter can exit.

    DBOS's worker threads are non-daemon, and a launched runtime nobody
    destroys leaves `Py_Finalize` joining them forever. Observed live on
    2026-08-10: a one-poll enqueue drainer finished its work, printed its
    result, and sat in `wait_for_thread_shutdown` for minutes - main thread
    joining a DBOS heartbeat, the notification listener still in `poll()` -
    until it was sampled and killed. The 26-minute figure in
    docs/verification_gate_environment_design.md is this shape seen from the
    outside.

    Only a process boundary calls this. The command functions must not: they
    also serve the resident in-process transport, where the runtime outlives
    any one call on purpose.
    """

    global _dbos_launched
    if not _dbos_launched or DBOS is None:
        return
    # A runtime that cannot be destroyed cleanly must not turn a finished
    # command into a failed one on its way out.
    with contextlib.suppress(Exception):
        DBOS.destroy()
    _dbos_launched = False


def exit_code_after_runtime_shutdown(code: int) -> int:
    """The process boundary: stop the runtime, then refuse to hang on survivors.

    Both hangs this guards were observed live on 2026-08-10. A one-poll drainer
    that launched DBOS finished its work and sat in `Py_Finalize` joining the
    runtime's non-daemon threads, which `shutdown_dbos` above fixes. A one-poll
    crash reconciler whose launch recovered three parked WorkUnit workflows
    then hung the same way *after* `DBOS.destroy`, because destroy does not
    stop a thread hosting a recovered, parked workflow. Every fact such a
    thread protects is already durable in Postgres - that is DBOS's whole
    contract - so once output is flushed, a lingering non-daemon thread costs a
    hard `os._exit` with the same code, and the next launch recovers exactly
    what the durable record says.

    Callers are process boundaries only: the ledger CLI's `main`, and
    `local-agent serve` after `uvicorn.run` returns. The resident daemons never
    call this; their runtime outlives any one command on purpose.
    """

    shutdown_dbos()
    lingering = [
        thread
        for thread in threading.enumerate()
        if thread is not threading.main_thread() and not thread.daemon
    ]
    if not lingering:
        return code
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def is_dbos_active() -> bool:
    return bool(
        _dbos_launched and settings.use_dbos and DBOS is not None and SetWorkflowID is not None
    )


def is_conductor_configured() -> bool:
    conductor_key = (settings.dbos_conductor_key or "").strip()
    return bool(settings.use_dbos and conductor_key)


def should_resume_pending_workflows_locally() -> bool:
    return not (is_dbos_active() and is_conductor_configured())


def run_workflow_durably(workflow_type: WorkflowType, event: IngressEvent) -> dict[str, Any]:
    """Run a workflow under DBOS when configured, else fall through to direct.

    Returns the WorkflowResult.model_dump(mode='json') dict so callers see the
    same shape whether they took the durable path or not.
    """

    if settings.use_dbos and DBOS is not None and not _dbos_launched:
        launch_dbos()
    if not is_dbos_active():
        return run_workflow(workflow_type, event).model_dump(mode="json")
    from .ids import build_workflow_id, sha256_text

    workflow_id = build_workflow_id(
        workflow_type,
        event.workspace_id,
        event.source_type,
        event.content_sha256 or sha256_text(event.model_dump_json()),
    )
    try:
        with SetWorkflowID(workflow_id):  # type: ignore[misc]
            return durable_workflow_entrypoint(
                workflow_type.value,
                event.model_dump(mode="json"),
            )
    except Exception:
        # If DBOS faults mid-call (system DB not reachable, etc.), fall through
        # to the direct path so Pi remains usable.
        return run_workflow(workflow_type, event).model_dump(mode="json")


def resume_pending_workflows() -> list[str]:
    """Explicitly recover every legacy PROCESSING/CREATED application run.

    DBOS owns recovery of DBOS executions. This fallback exists for application
    workflow rows created outside an active DBOS runtime and is called only by
    the operator command. Every selected row leaves its stale live state:
    replay results are copied back to that row, missing input fails permanently,
    and replay exceptions fail retryably.
    """

    from .runtime import get_runtime

    runtime = get_runtime()
    pending = runtime.repository.list_pending_workflow_runs()
    resumed: list[str] = []
    for workflow_id, workflow_type, event_id in pending:
        if event_id is None:
            runtime.repository.update_workflow(
                workflow_id,
                status=WorkflowStatus.FAILED_PERMANENT,
                error="cannot recover workflow without an input event id",
            )
            continue
        event = runtime.repository.get_ingress_event(event_id)
        if event is None:
            runtime.repository.update_workflow(
                workflow_id,
                status=WorkflowStatus.FAILED_PERMANENT,
                error=f"cannot recover workflow because ingress event {event_id!r} is missing",
            )
            continue
        try:
            result = run_workflow(WorkflowType(workflow_type), event)
            if result.status in {WorkflowStatus.CREATED, WorkflowStatus.PROCESSING}:
                raise RuntimeError(
                    f"recovery returned non-terminal workflow status {result.status.value}"
                )
            runtime.repository.update_workflow(
                workflow_id,
                status=result.status,
                stage=result.current_stage,
                clear_error=True,
            )
            resumed.append(workflow_id)
        except Exception as exc:
            runtime.repository.update_workflow(
                workflow_id,
                status=WorkflowStatus.FAILED_RETRYABLE,
                error=f"explicit recovery failed: {exc}",
                retry_increment=True,
            )
            continue
    return resumed
