# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""WorkUnit commands in the coordination command shape.

The coordination surface answers with ``{"ok": True, ...}`` or
``{"ok": False, "error": ...}``, and every operator-facing tool in this repository
speaks that shape. These wrappers translate the typed service layer into it so the
CLI, the MCP registry, and a Pi-dispatched agent all reach the same operations.

There is intentionally no general command that sets a phase, milestone status,
or WorkUnit status. The one recovery verb accepts only an approved exact commit
already integrated into its declared branch and derives the lifecycle facts and
evidence from that immutable dispatch result.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..coordination.resident_loop import (
    ResidentLoop,
    ResidentLoopBusy,
    hold_resident_loop,
)
from ..coordination.store import check_connection_budget, err, ok
from ..harness_availability import check_harness_availability
from ..harness_readiness import plan_tier_staffing, restaffings, staffing_refusals
from ..settings import get_settings
from ..staffing import load_staffing
from . import repository as repo
from . import service
from .dispatch_adoption import (
    DispatchAdoptionRefused,
    adopt_integrated_milestone,
    adopt_recovered_dispatch,
    adopt_settled_dispatch,
)
from .root_workflow import EnqueueDelivery, EnqueueFailed, drain_enqueue_outbox

logger = logging.getLogger(__name__)


def compile_design_doc(
    path_or_revision: str,
    *,
    design_doc_id: str | None = None,
    classify_phases: bool = False,
) -> dict[str, Any]:
    """Compile a DesignDoc file, or an already-stored revision, into a plan.

    ``classify_phases`` asks the local model to propose a lifecycle phase for any
    milestone that declared none, which is what a document writing its milestones
    as prose needs. It is opt-in because a compile without it is deterministic and
    offline, and that is the right default for a document that already says.
    """

    try:
        if path_or_revision.startswith("ddr_"):
            result = service.compile_design_doc_revision(
                path_or_revision,
                classify_phases=classify_phases,
            )
        else:
            path = Path(path_or_revision).expanduser()
            if not path.exists():
                return err("design_doc_not_found", path=str(path))
            revision = service.ingest_design_doc_file(path, design_doc_id=design_doc_id)
            result = service.compile_design_doc_revision(
                revision.design_doc_revision_id,
                classify_phases=classify_phases,
            )
    except repo.WorkUnitError as exc:
        return err("compile_failed", message=str(exc))
    return ok(**result.to_payload())


def _harness_refusal() -> dict[str, Any] | None:
    """The refusal an operator door owes when a staffed CLI says it cannot act.

    Written once and shared by both doors, because this is a single decision
    rather than two that happen to agree: what counts as blocking, what the
    error code is, and what the operator is told to do about it. A second copy
    is a second place for those three to drift apart.

    The probe belongs at this layer and not in the service beneath it. These
    functions are the doors a human opens; the service is called by tests, by
    the crash reconciler, and by other code that has no business spawning a
    subprocess to read this machine. Putting the probe there once made a core
    function environment-dependent and failed 71 tests. It cannot live in
    compilation either: the compiler is offline by design, and one document has
    to compile to one plan hash on every host.

    What counts as blocking is narrower than "a staffed CLI is down". A bench
    that staffs senior to claude and staff to codex has a ready peer for either
    one, and refusing the run because a single provider is logged out turns a
    condition the system recovers from on its own into a stop somebody has to
    clear by hand. Only a tier that nothing on the bench can cover blocks.

    A tier that moved is logged rather than returned. The run proceeds, so it is
    not a refusal, and it is still not allowed to be silent: the bench is a
    decision the operator made and this is the system declining to follow it.

    ``None`` when nothing blocks, so callers read as a guard clause.
    """

    staffing = load_staffing(get_settings().config_dir / "staffing.toml")
    plan = plan_tier_staffing(
        bench=staffing,
        states=check_harness_availability(staffing),
    )
    refusals = staffing_refusals(plan)
    if refusals:
        return err(
            "harness_not_ready",
            message="no harness on this bench can staff: " + "; ".join(refusals),
        )
    for notice in restaffings(plan):
        logger.warning("harness_restaffed", extra={"detail": notice})
    return None


def _budget_refusal() -> dict[str, Any] | None:
    """The refusal a door owes when the ledger's connection budget is already spent.

    Its own function rather than a second clause inside `_harness_refusal`,
    because they answer different questions about different machines: one asks
    whether a CLI on this host can act, the other whether the shared Postgres
    has room for the pools a run will open. Folding them together would give
    them one name and one error code, and an operator reading
    `harness_not_ready` would go re-run a login that was never the problem.

    Checked at a door for the same reason the harness probe is. A pool opens
    lazily inside whatever is already running, so refusing at pool creation
    would take down a resident loop over a condition it did not create, while
    refusing here stops the run that would have created it.

    ``None`` when nothing blocks, so callers read as a guard clause.
    """

    budget = check_connection_budget()
    if budget.sufficient:
        return None
    return err("connection_budget_exceeded", message=budget.describe())


def start_work_unit(
    compiled_plan_revision_id: str,
    title: str | None = None,
    approved_plan_hash: str | None = None,
) -> dict[str, Any]:
    """Start a compiled plan, refusing first if a staffed CLI says it cannot act."""

    refusal = _budget_refusal() or _harness_refusal()
    if refusal is not None:
        return refusal
    try:
        return ok(
            **service.start_work_unit(
                compiled_plan_revision_id,
                title=title,
                approved_plan_hash=approved_plan_hash,
            )
        )
    except repo.WorkUnitError as exc:
        return err("start_rejected", message=str(exc))


def get_work_unit(work_unit_id: str) -> dict[str, Any]:
    try:
        return ok(work_unit=service.get_work_unit(work_unit_id).model_dump(mode="json"))
    except repo.WorkUnitError as exc:
        return err("not_found", message=str(exc))


def list_work_units(status: str | None = None) -> dict[str, Any]:
    try:
        return ok(work_units=list(service.list_work_units(status)))
    except ValueError as exc:
        return err("invalid_status", message=str(exc))


def list_design_docs() -> dict[str, Any]:
    return ok(design_docs=list(service.list_design_docs()))


def list_work_unit_events(
    work_unit_id: str,
    after_sequence: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    try:
        events = service.list_work_unit_events(
            work_unit_id,
            after_sequence=after_sequence,
            limit=limit,
        )
    except repo.WorkUnitError as exc:
        return err("not_found", message=str(exc))
    return ok(work_unit_id=work_unit_id, events=list(events))


def list_work_unit_artifacts(work_unit_id: str) -> dict[str, Any]:
    try:
        artifacts = service.list_work_unit_artifacts(work_unit_id)
    except repo.WorkUnitError as exc:
        return err("not_found", message=str(exc))
    return ok(work_unit_id=work_unit_id, artifacts=list(artifacts))


def _resume_gate() -> str | None:
    """The refusal a decision-triggered resume answers to at this door.

    The same two probes `resume_work_unit` runs, for the same reason: an
    approved override on a BLOCKED WorkUnit now enqueues a resume, and this
    door owes that resume the same refusal it owes a typed one. It is handed to
    the service as a callable rather than run here because most decisions
    (denials, clarifications, approvals of waiting milestones) deliver nothing,
    and a harness probe spawns subprocesses that those submissions should not
    pay for.
    """

    refusal = _budget_refusal() or _harness_refusal()
    if refusal is None:
        return None
    return str(refusal.get("message") or refusal.get("error"))


def submit_work_unit_decision(
    work_unit_id: str,
    request_id: str,
    decision: str,
    idempotency_key: str,
    decided_by: str = "operator",
) -> dict[str, Any]:
    try:
        # A bare CLI or MCP process is also the sender of the durable wake.  The
        # API lifespan already launches DBOS, but this wrapper is the shared edge
        # for the short-lived surfaces and must establish the same capability
        # before recording a decision that a milestone is waiting to receive.
        from ..dbos_app import launch_dbos

        launch_dbos()
        return ok(
            **service.submit_work_unit_decision(
                work_unit_id,
                request_id,
                decision,
                idempotency_key,
                decided_by=decided_by,
                resume_refusal=_resume_gate,
            )
        )
    except repo.DecisionRequestMismatch as exc:
        return err("decision_request_mismatch", message=str(exc))
    except ValueError as exc:
        return err("invalid_decision", message=str(exc))
    except repo.WorkUnitError as exc:
        return err("not_found", message=str(exc))


def cancel_work_unit(work_unit_id: str, reason: str = "cancelled by operator") -> dict[str, Any]:
    try:
        return ok(**service.cancel_work_unit(work_unit_id, reason=reason))
    except repo.WorkUnitError as exc:
        return err("not_found", message=str(exc))


def resume_work_unit(work_unit_id: str, inline: bool = False) -> dict[str, Any]:
    """Re-drive a parked WorkUnit, refusing first on the same grounds as starting.

    This door needs the check at least as much as ``start_work_unit`` does, and
    for a while only that one had it. A WorkUnit that a dead CLI blocked is
    reached by resuming, never by starting, so the guard sat on the path an
    operator takes once and was absent from the path they take every time after
    that. Resuming into a harness that already said it cannot act reproduces the
    identical failure and spends another attempt from the milestone's budget to
    learn nothing.

    The reconciler is deliberately not covered by this. It calls
    ``service.resume_work_unit`` directly and is bounded by
    ``max_automatic_recoveries``; a resident loop must not shell out to probe
    the machine on every pass.

    ``inline`` runs the lifecycle in this process, which is what a single-shot
    operator command wants when no resident DBOS runtime is up. Without it the
    resume is handed to DBOS and this command returns immediately.
    """

    refusal = _budget_refusal() or _harness_refusal()
    if refusal is not None:
        return refusal
    try:
        return ok(
            **service.resume_work_unit(
                work_unit_id,
                delivery=EnqueueDelivery.INLINE if inline else EnqueueDelivery.DURABLE,
            )
        )
    except repo.WorkUnitError as exc:
        return err("resume_rejected", message=str(exc))


def adopt_recovered_work_unit_dispatch(intent_id: str) -> dict[str, Any]:
    """Adopt one approved, integrated parser recovery into its blocked milestone."""

    try:
        adoption = adopt_recovered_dispatch(intent_id)
    except DispatchAdoptionRefused as exc:
        return err(exc.code, message=str(exc), intent_id=intent_id)
    except (KeyError, repo.WorkUnitError) as exc:
        return err("adoption_target_missing", message=str(exc), intent_id=intent_id)
    return ok(
        intent_id=intent_id,
        work_unit_id=adoption.work_unit_id,
        milestone_key=adoption.milestone_key,
        attempt=adoption.attempt,
        approval_id=adoption.approval_id,
        commit_sha=adoption.commit_sha,
        applied=adoption.applied,
        next_step=f"resume WorkUnit {adoption.work_unit_id}",
    )


def adopt_integrated_work_unit_milestone(
    work_unit_id: str,
    milestone_key: str,
    commit_sha: str,
    accepted_by: str,
    acceptance_evidence: str,
) -> dict[str, Any]:
    """Adopt an already-integrated ancestor after a provider-blocked attempt."""

    try:
        adoption = adopt_integrated_milestone(
            work_unit_id,
            milestone_key,
            commit_sha,
            accepted_by=accepted_by,
            acceptance_evidence=acceptance_evidence,
        )
    except DispatchAdoptionRefused as exc:
        return err(exc.code, message=str(exc), work_unit_id=work_unit_id)
    except (KeyError, repo.WorkUnitError) as exc:
        return err("adoption_target_missing", message=str(exc), work_unit_id=work_unit_id)
    return ok(
        work_unit_id=adoption.work_unit_id,
        milestone_key=adoption.milestone_key,
        attempt=adoption.attempt,
        commit_sha=adoption.commit_sha,
        accepted_by=adoption.accepted_by,
        applied=adoption.applied,
        next_step=f"resume WorkUnit {adoption.work_unit_id}",
    )


def adopt_settled_work_unit_dispatch(work_unit_id: str, milestone_key: str) -> dict[str, Any]:
    """Credit a wait-elapsed milestone with its own dispatch once it settled DONE."""

    try:
        adoption = adopt_settled_dispatch(work_unit_id, milestone_key)
    except DispatchAdoptionRefused as exc:
        return err(exc.code, message=str(exc), work_unit_id=work_unit_id)
    except (KeyError, repo.WorkUnitError) as exc:
        return err("adoption_target_missing", message=str(exc), work_unit_id=work_unit_id)
    return ok(
        work_unit_id=adoption.work_unit_id,
        milestone_key=adoption.milestone_key,
        attempt=adoption.attempt,
        intent_id=adoption.intent_id,
        applied=adoption.applied,
        next_step=f"resume WorkUnit {adoption.work_unit_id}",
    )


def drain_work_unit_enqueues(limit: int = 20, inline: bool = False) -> dict[str, Any]:
    """Deliver pending root-workflow enqueues.

    ``inline`` executes them here instead of handing them to DBOS. An undeliverable
    row stays pending and says why.
    """

    outcomes = drain_enqueue_outbox(
        limit,
        EnqueueDelivery.INLINE if inline else EnqueueDelivery.DURABLE,
    )
    payloads = [item.to_payload() for item in outcomes]
    failed = [item for item in outcomes if isinstance(item, EnqueueFailed)]
    if failed:
        # A drain in which every row raised used to answer `{"ok": true,
        # "outcomes": []}` and exit zero, because the failures were caught and
        # dropped before they reached this list. `err` is the command boundary's
        # way of saying no, and it logs the failure dimensions on the way out.
        return err(
            "enqueue_delivery_failed",
            message=(
                f"{len(failed)} of {len(outcomes)} enqueue(s) could not be started; "
                f"first: {failed[0].failure.message}"
            ),
            outcomes=payloads,
            failed=[item.work_unit_id for item in failed],
        )
    return ok(outcomes=payloads)


def run_enqueue_drainer(
    interval_seconds: float = 5.0,
    limit: int = 20,
    max_polls: int | None = None,
    inline: bool = False,
) -> dict[str, Any]:
    """Run the outbox drainer until `max_polls` is reached, or forever.

    The resident half of the transactional outbox. `drain_work_unit_enqueues`
    does one pass, which is right for a script; this is what an operator leaves
    running so a new WorkUnit starts without anyone typing a command.

    One per coordination database. A second one is what starting the runtime in
    a second git worktree used to produce, and while the outbox claim is safe
    under concurrency, the two drainers hand WorkUnits to DBOS from different
    checkouts, so the code that runs a WorkUnit stops being knowable.
    """

    from .enqueue_drainer import EnqueueDrainer

    with hold_resident_loop(ResidentLoop.ENQUEUE_DRAINER) as lease:
        if isinstance(lease, ResidentLoopBusy):
            return err(
                "resident_loop_busy",
                message=lease.describe(),
                loop=lease.loop.value,
                owner=lease.owner.to_payload() if lease.owner else None,
            )

        drainer = EnqueueDrainer(
            limit=limit,
            delivery=EnqueueDelivery.INLINE if inline else EnqueueDelivery.DURABLE,
        )
        delivered = drainer.run(interval_seconds=interval_seconds, max_polls=max_polls)
        return ok(delivered=delivered, polls=max_polls)


def run_crash_reconciler(
    interval_seconds: float = 30.0,
    max_polls: int | None = None,
    max_automatic_recoveries: int = 3,
) -> dict[str, Any]:
    """Recover WorkUnits whose durable execution died, until `max_polls`, or forever.

    The unattended half of `execution_recovery`, which only ever ran when an
    operator resumed. A crash writes no halt, so the ledger keeps asserting that
    work is in flight while no process carries it, and nothing corrected that
    without a person.

    It never infers a death. Candidates are WorkUnits whose *status* claims an
    execution, DBOS is asked about each, and only `ExecutionLiveness.DEAD` is
    recovered. A resume follows only when the recovery proof says something was
    repaired.

    One per coordination database, for the same reason as the other two: a second
    one from a second checkout makes "which code recovered this" a coin flip.

    `max_automatic_recoveries` bounds how often one WorkUnit may be restarted
    without a person, counted from `AUTOMATIC_CRASH_RECOVERY` events rather than
    from `execution_epoch`, which counts every halt however caused.
    """

    from .crash_recovery_loop import CrashReconciler

    with hold_resident_loop(ResidentLoop.CRASH_RECONCILER) as lease:
        if isinstance(lease, ResidentLoopBusy):
            return err(
                "resident_loop_busy",
                message=lease.describe(),
                loop=lease.loop.value,
                owner=lease.owner.to_payload() if lease.owner else None,
            )

        reconciler = CrashReconciler(max_automatic_recoveries=max_automatic_recoveries)
        repaired = reconciler.run(interval_seconds=interval_seconds, max_polls=max_polls)
        return ok(repaired=repaired, polls=max_polls)


__all__ = [
    "adopt_recovered_work_unit_dispatch",
    "adopt_integrated_work_unit_milestone",
    "cancel_work_unit",
    "compile_design_doc",
    "drain_work_unit_enqueues",
    "run_enqueue_drainer",
    "get_work_unit",
    "list_design_docs",
    "list_work_unit_artifacts",
    "list_work_unit_events",
    "list_work_units",
    "resume_work_unit",
    "start_work_unit",
    "submit_work_unit_decision",
]
