# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The public operations on DesignDoc-governed work.

These are the only entry points a CLI command, an HTTP route, or an MCP tool
should use. Deliberately absent: anything that sets a phase or a milestone status
directly. An operator can approve, deny, cancel, or resume, and each of those is a
fact the transition engine validates.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..coordination.store import iso
from ..ids import sha256_text
from . import repository as repo
from .cancellation import run_cancellation_cascade
from .compiler import (
    CompilationRejected,
    CompiledPlanOutcome,
    ValidationStatus,
    compile_design_doc,
)
from .design_doc import (
    SCHEMA_VERSION_PARSED_DESIGN_DOC,
    Diagnostic,
    PhaseInference,
    apply_phase_inference,
    parse_design_doc,
)
from .events import (
    ApprovalReceived,
    ApprovalRequested,
    DecisionKindMismatch,
    DecisionRequestKind,
    DecisionRequestStatus,
    MilestoneTransition,
    OperatorDecision,
    RetryOverridden,
    decision_outcome,
)
from .execution_recovery import ExecutionLiveness, recover_dead_execution
from .lifecycle import (
    TERMINAL_WORK_UNIT_STATUSES,
    LifecyclePhase,
    MilestoneExecutionStatus,
    WorkUnitStatus,
)
from .phase_classifier import PhaseClassifier, classify_missing_phases
from .projection import WorkUnitView, build_work_unit_view
from .retry import (
    RetryPermitted,
    RetryRefused,
    decide_retry,
)
from .root_workflow import (
    EnqueueDelivery,
    EnqueueFailed,
    drain_enqueue_outbox,
    notify_operator_decision,
    resume_root_workflow,
)


@dataclass(frozen=True)
class CompileResult:
    """What compiling produced, including the reasons it may not run yet."""

    design_doc_revision_id: str
    compiled_plan_revision_id: str | None
    plan_hash: str | None
    validation_status: ValidationStatus
    diagnostics: tuple[Diagnostic, ...]
    execution_blockers: tuple[str, ...]

    @property
    def runnable(self) -> bool:
        return (
            self.validation_status is ValidationStatus.VALID
            and self.compiled_plan_revision_id is not None
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "design_doc_revision_id": self.design_doc_revision_id,
            "compiled_plan_revision_id": self.compiled_plan_revision_id,
            "plan_hash": self.plan_hash,
            "validation_status": self.validation_status.value,
            "diagnostics": [item.to_payload() for item in self.diagnostics],
            "execution_blockers": list(self.execution_blockers),
            "runnable": self.runnable,
        }


def ingest_design_doc(
    raw_content: str,
    *,
    design_doc_id: str,
    source_path: str | None = None,
    created_by: str = "operator",
) -> repo.DesignDocRevisionRow:
    """Persist one immutable DesignDoc revision, parsed but not yet compiled."""

    parsed = parse_design_doc(raw_content, design_doc_id=design_doc_id, source_path=source_path)
    return repo.insert_design_doc_revision(
        design_doc_id=design_doc_id,
        raw_content=raw_content,
        schema_version=SCHEMA_VERSION_PARSED_DESIGN_DOC,
        structured_content=parsed.to_payload(),
        source_path=source_path,
        created_by=created_by,
    )


def ingest_design_doc_file(
    path: str | Path,
    *,
    design_doc_id: str | None = None,
    created_by: str = "operator",
) -> repo.DesignDocRevisionRow:
    resolved = Path(path).expanduser().resolve()
    return ingest_design_doc(
        resolved.read_text(encoding="utf-8"),
        design_doc_id=design_doc_id or resolved.stem,
        source_path=str(resolved),
        created_by=created_by,
    )


def compile_design_doc_revision(
    design_doc_revision_id: str,
    *,
    phase_inferences: tuple[PhaseInference, ...] = (),
    classify_phases: bool = False,
    classifier: PhaseClassifier | None = None,
) -> CompileResult:
    """Compile a stored revision into an immutable plan, or explain the refusal.

    Inferred phases are accepted only as proposals: they arrive with confidence and
    reasoning, and a low-confidence inference becomes an execution blocker rather
    than a silent decision.

    ``phase_inferences`` supplies proposals directly, which is what legacy saga
    adoption does because it classifies from ledger rows rather than from prose.
    ``classify_phases`` asks a model for them instead, for a document whose
    milestones are written as prose and therefore declare no phase. Supplying both
    is not an error; explicit inferences win, because a caller that computed them
    knows something the classifier does not.
    """

    revision = repo.get_design_doc_revision(design_doc_revision_id)
    parsed = parse_design_doc(
        revision.raw_content,
        design_doc_id=revision.design_doc_id,
        source_path=revision.source_path,
    )
    if not phase_inferences and classify_phases:
        # Only for milestones that declared no phase, and only as proposals: a
        # document spelling `Phase: IMPLEMENT` is never second-guessed, and an
        # inference below the confirmation threshold becomes an execution blocker
        # rather than a phase. Off by default so a compile stays deterministic
        # and offline unless the caller asks for judgment.
        phase_inferences = classify_missing_phases(parsed, classifier)
    if phase_inferences:
        parsed = apply_phase_inference(parsed, phase_inferences)
    outcome = compile_design_doc(parsed, design_doc_revision_id=design_doc_revision_id)
    if isinstance(outcome, CompilationRejected):
        return CompileResult(
            design_doc_revision_id=design_doc_revision_id,
            compiled_plan_revision_id=None,
            plan_hash=None,
            validation_status=outcome.validation_status,
            diagnostics=outcome.diagnostics,
            execution_blockers=(),
        )
    persisted = repo.insert_compiled_plan_revision(
        outcome,
        design_doc_revision_id=design_doc_revision_id,
    )
    return CompileResult(
        design_doc_revision_id=design_doc_revision_id,
        compiled_plan_revision_id=persisted.compiled_plan_revision_id,
        plan_hash=persisted.plan_hash,
        validation_status=persisted.validation_status,
        diagnostics=outcome.diagnostics,
        execution_blockers=persisted.execution_blockers,
    )


def compile_design_doc_text(
    raw_content: str,
    *,
    design_doc_id: str,
    source_path: str | None = None,
    created_by: str = "operator",
) -> CompileResult:
    revision = ingest_design_doc(
        raw_content,
        design_doc_id=design_doc_id,
        source_path=source_path,
        created_by=created_by,
    )
    return compile_design_doc_revision(revision.design_doc_revision_id)


def start_work_unit(
    compiled_plan_revision_id: str,
    *,
    title: str | None = None,
    approved_plan_hash: str | None = None,
    supersedes_work_unit_id: str | None = None,
    delivery: EnqueueDelivery | None = EnqueueDelivery.DURABLE,
) -> dict[str, Any]:
    """Create the WorkUnit and hand its root execution to a runtime.

    Creation and the intent to run commit together; delivery happens after. A
    repeated call returns the existing WorkUnit and does not enqueue a second root
    execution.

    ``delivery=None`` creates the WorkUnit and attempts no delivery at all, which
    is what a caller that only wants the row wants. ``DURABLE`` leaves the outbox
    row pending when no DBOS runtime is active; ``INLINE`` drives the lifecycle
    here.
    """

    result = repo.start_work_unit(
        compiled_plan_revision_id,
        title=title,
        approved_plan_hash=approved_plan_hash,
        supersedes_work_unit_id=supersedes_work_unit_id,
    )
    payload: dict[str, Any] = {
        "work_unit_id": result.work_unit.work_unit_id,
        "root_workflow_id": result.root_workflow_id,
        "status": result.work_unit.status.value,
        "created": result.created,
    }
    if delivery is not None and result.created:
        outcomes = drain_enqueue_outbox(delivery=delivery)
        payload["dispatch"] = [item.to_payload() for item in outcomes]
        failed = [item for item in outcomes if isinstance(item, EnqueueFailed)]
        if failed:
            # A start whose delivery raised used to return a success payload with
            # an empty `dispatch`, because the drain caught the exception and
            # appended nothing. The caller who asked for the run got no error and
            # the only trace was a WorkUnit left RUNNING.
            payload["dispatch_failed"] = [
                {
                    "work_unit_id": item.work_unit_id,
                    "attempts": item.attempts,
                    "failure": item.failure.to_dict(),
                }
                for item in failed
            ]
    return payload


def resume_work_unit(
    work_unit_id: str,
    *,
    delivery: EnqueueDelivery = EnqueueDelivery.DURABLE,
) -> dict[str, Any]:
    """Re-drive a blocked or waiting WorkUnit.

    Blocked milestones return to ``READY`` so the scheduler can pick them up again;
    completed phases, completed siblings, artifacts, decisions, and history are
    untouched. Re-entry is what makes this safe: the root workflow skips whatever
    already terminated.

    A crashed execution is recovered first. A run that dies never records how it
    ended, so the epoch still records it and the continuation would be derived
    from a workflow ID DBOS refuses to re-run. Recovery writes that missing halt,
    which is what makes the continuation below land on a fresh identity rather
    than replaying the dead run's recorded error.

    ``BLOCKED -> READY`` is now a decision rather than a foregone conclusion. The
    compiled plan carries a per-milestone attempt budget, and the only place that
    consulted it ran while the WorkUnit was scheduling and only for milestones
    whose status was ``FAILED`` - which is the one status a failed executor run
    never occupies. So a blocked attempt 3 became attempt 4 and N resumes bought
    N attempts. The decision lives here rather than in the scheduler because
    resume is where the retry policy is actually chosen, and because widening the
    scheduler's predicate to ``BLOCKED`` would fail milestones blocked for
    no-fault reasons.
    """

    unit = repo.get_work_unit(work_unit_id)
    if unit.status in TERMINAL_WORK_UNIT_STATUSES:
        raise repo.WorkUnitError(
            f"work unit {work_unit_id!r} is {unit.status.value} and cannot be resumed"
        )
    recovered = recover_dead_execution(work_unit_id)
    if recovered.liveness is ExecutionLiveness.INDETERMINATE:
        # Refused before anything is written. Proceeding here would mint a
        # continuation on an epoch that was never repaired, which is the failure
        # this whole path exists to prevent - and it would report `delivered:
        # true` while doing it, so the operator would be told the resume worked.
        #
        # Declining is safe in a way proceeding is not: a resume can be repeated
        # once the runtime answers, and nothing has been changed in the meantime.
        return {
            "work_unit_id": work_unit_id,
            "continuation_of": unit.root_workflow_id,
            "delivered": False,
            "durable": False,
            "reason": (
                "could not determine whether execution "
                f"{recovered.execution_workflow_id} has ended, so resuming would "
                "risk re-entering it; retry once the DBOS system database answers"
            ),
            "recovered": recovered.to_payload(),
        }
    plan = repo.get_compiled_plan_revision(unit.compiled_plan_revision_id).plan
    exhausted: list[dict[str, Any]] = []
    for execution in repo.list_milestone_executions(work_unit_id):
        if execution.status is not MilestoneExecutionStatus.BLOCKED:
            continue
        request_id = retry_override_request_id(work_unit_id, execution.stable_key)
        decision = decide_retry(
            milestone_key=execution.stable_key,
            phase=execution.phase,
            status=execution.status,
            attempt=execution.attempt,
            failure_class=execution.failure_class,
            max_attempts=plan.milestone(execution.stable_key).failure_policy.max_attempts,
            operator_override=_retry_override_granted(request_id),
        )
        match decision:
            case RetryPermitted():
                repo.record_fact(
                    work_unit_id,
                    MilestoneTransition(
                        phase=decision.phase,
                        milestone_key=decision.milestone_key,
                        status=MilestoneExecutionStatus.READY,
                        attempt=decision.next_attempt,
                    ),
                )
            case RetryRefused():
                _refuse_retry(work_unit_id, decision, request_id=request_id)
                exhausted.append(
                    {
                        "milestone_key": decision.milestone_key,
                        "phase": decision.phase.value,
                        "attempt": decision.attempt,
                        "permitted": decision.permitted,
                        "override_request_id": request_id,
                    }
                )
    return {
        **resume_root_workflow(work_unit_id, delivery),
        "recovered": recovered.to_payload(),
        "exhausted": tuple(exhausted),
    }


def retry_override_request_id(work_unit_id: str, milestone_key: str) -> str:
    """The decision an operator answers to let one milestone exceed its budget.

    Derived from the milestone and the kind, never from the attempt. Two reasons,
    and they pull the same way: an override is a judgement about this milestone
    rather than about one try at it, and the approval request for the same
    milestone is derived from work-unit-plus-key alone, so anything that did not
    also name the kind would collide with it.
    """

    digest = sha256_text(
        f"{work_unit_id}:{milestone_key}:{DecisionRequestKind.RETRY_BUDGET_OVERRIDE.value}"
    )
    return f"wud_{digest[:24]}"


def _retry_override_granted(request_id: str) -> bool:
    """Whether an operator has already answered this override request yes.

    A resolved-and-denied request is not an absence: it is a person saying the
    budget stands, and reading it as "no override" would be the same answer by
    accident. It is spelled out here so a later reader can see that both
    resolutions were considered.
    """

    request = repo.get_decision_request(request_id)
    if request is None or request.status is not DecisionRequestStatus.RESOLVED:
        return False
    outcome = decision_outcome(
        DecisionRequestKind.RETRY_BUDGET_OVERRIDE,
        OperatorDecision(str(request.decision)),
        decided_by=request.decided_by or "operator",
    )
    return isinstance(outcome, RetryOverridden)


def _refuse_retry(work_unit_id: str, decision: RetryRefused, *, request_id: str) -> None:
    """Leave the milestone blocked, and open the decision that could unblock it.

    The milestone is deliberately **not** moved to ``FAILED``. ``FAILED`` is
    terminal for a milestone - its edge set is empty - so failing it here would
    make the operator override this same function opens unusable, and "a new plan
    revision or an explicit operator override" would collapse to the first alone.
    ``BLOCKED`` already means "stopped, and can be picked up again", which is
    exactly the state a milestone awaiting a person's decision is in.

    So the refusal is expressed by declining to write ``READY``, not by writing
    anything. What is durable is the decision request: it names the milestone,
    the arithmetic, and the two answers, and it appears in the WorkUnit's pending
    decisions where an operator will see it. A refusal with no named way out is a
    dead end somebody has to read the codebase to escape.

    The scheduler's own budget check does write ``FAILED``, and that is not an
    inconsistency: it refuses a milestone that has *already* failed, where
    ``FAILED -> FAILED`` restates a conclusion. This one refuses a milestone that
    has stopped, where the conclusion is still an operator's to draw.
    """

    if repo.get_decision_request(request_id) is not None:
        return
    repo.record_fact(
        work_unit_id,
        ApprovalRequested(
            phase=decision.phase,
            milestone_key=decision.milestone_key,
            attempt=decision.attempt,
            request_id=request_id,
            prompt=(
                f"{decision.describe()}. Approve to permit one more attempt, or deny "
                "to leave the budget standing."
            ),
            kind=DecisionRequestKind.RETRY_BUDGET_OVERRIDE,
        ),
    )


def cancel_work_unit(work_unit_id: str, *, reason: str = "cancelled by operator") -> dict[str, Any]:
    """Ask for cancellation and run the cascade that actually stops things.

    Terminal milestones keep their outcomes. Cancelling does not erase what already
    happened; it stops what has not.

    The work is in `cancellation.run_cancellation_cascade`, which moves the
    WorkUnit to `CANCELLING` before it stops anything and only writes `CANCELLED`
    once every stoppable thing has been told to stop. The returned payload names
    what could not be stopped, because a claimed dispatch intent means an agent
    may still be running and that is the operator's next decision, not ours.
    """

    return run_cancellation_cascade(work_unit_id, reason=reason).to_payload()


def submit_work_unit_decision(
    work_unit_id: str,
    request_id: str,
    decision: str,
    idempotency_key: str,
    *,
    decided_by: str = "operator",
    payload: dict[str, Any] | None = None,
    resume_refusal: Callable[[], str | None] | None = None,
) -> dict[str, Any]:
    """Resolve one named operator decision, and let it move the work it unblocks.

    The request ID is required and validated. A decision that names another
    request, another WorkUnit, or an already-resolved request is rejected, so a
    message that merely sounds like approval cannot unblock a milestone.

    A decision that unblocks a ``BLOCKED`` WorkUnit does not stop at being
    recorded. The durable wake below only reaches a milestone that is still
    parked inside a live root execution; a blocked unit's epoch has ended and
    nothing is listening, so the answer used to land and the unit stayed parked
    until an operator also typed ``resume_work_unit``. Now the resolution
    leaves a pending ``RESUME`` row in the enqueue outbox, and the resident
    drainer delivers it through the same path that command takes. The payload's
    ``resume`` field reports what happened; absent means the decision unblocks
    nothing.

    ``resume_refusal`` is the door's refusal gate, consulted lazily and only
    when a resume would actually be enqueued. It lives at the door and arrives
    injected for the same reason `_harness_refusal` documents: this function is
    called by tests and the API and must not spawn probe subprocesses. A gate
    that answers with a reason still resolves the decision - an operator's
    answer must never be lost - but the resume is reported rather than
    enqueued.
    """

    request = repo.get_decision_request(request_id)
    if request is None:
        raise repo.DecisionRequestMismatch(f"unknown decision request {request_id!r}")
    if request.work_unit_id != work_unit_id:
        raise repo.DecisionRequestMismatch(
            f"decision request {request_id!r} does not belong to work unit {work_unit_id!r}"
        )
    if request.status is not DecisionRequestStatus.PENDING:
        return {
            "work_unit_id": work_unit_id,
            "request_id": request_id,
            "decision": request.decision.value if request.decision is not None else None,
            "applied": False,
            "reason": f"request is already {request.status.value}",
            # Replayed on purpose: the enqueue runs in its own transaction
            # after the fact commits, so a crash between the two loses only the
            # delivery. Re-submitting the decision is the documented repair,
            # and it heals here because this helper re-derives everything from
            # the durable request row rather than from this call's arguments.
            "resume": _resume_delivery_after_decision(
                work_unit_id, request_id, resume_refusal=resume_refusal
            ),
        }
    executions = {
        execution.milestone_execution_id: execution
        for execution in repo.list_milestone_executions(work_unit_id)
    }
    execution = executions.get(request.milestone_execution_id or "")
    if execution is None:
        raise repo.DecisionRequestMismatch(
            f"decision request {request_id!r} names no milestone of this work unit"
        )
    # The pairing is checked here because here is the only place both halves are
    # known: the request row says which kind it is, and the submission says which
    # answer it carries. Refusing an impossible pair at the write means no reader
    # downstream has to re-derive the rule, and the approval gate can take a type
    # that a clarification answer cannot inhabit.
    # Only the mismatch is translated. A decision string that names no member at
    # all is a different error with a different audience, and it keeps raising the
    # ValueError that `OperatorDecision` produces.
    try:
        decision_outcome(request.request_kind, OperatorDecision(decision), payload or {})
    except DecisionKindMismatch as exc:
        raise repo.DecisionRequestMismatch(str(exc)) from exc

    outcome = repo.record_fact(
        work_unit_id,
        ApprovalReceived(
            phase=execution.phase,
            milestone_key=execution.stable_key,
            attempt=execution.attempt,
            request_id=request_id,
            decision=OperatorDecision(decision),
            decided_by=decided_by,
            response_idempotency_key=idempotency_key,
            decision_payload=payload or {},
        ),
    )
    # After the fact is durable, never before. A milestone woken by this
    # notification immediately re-reads the request row, so a send from inside the
    # write would race the waiter against the decision it is being told about.
    if outcome.applied:
        notify_operator_decision(work_unit_id, execution.stable_key, execution.attempt, request_id)

    return {
        "work_unit_id": work_unit_id,
        "request_id": request_id,
        "decision": decision,
        "applied": outcome.applied,
        "milestone_key": execution.stable_key,
        "sequence_number": outcome.event.sequence_number,
        "resume": _resume_delivery_after_decision(
            work_unit_id, request_id, resume_refusal=resume_refusal
        ),
    }


def _resume_delivery_after_decision(
    work_unit_id: str,
    request_id: str,
    *,
    resume_refusal: Callable[[], str | None] | None,
) -> dict[str, Any] | None:
    """Enqueue the resume this resolved decision earns, or say why not.

    ``None`` means the decision unblocks nothing: it is unresolved, its outcome
    is not one that lifts a block, or the WorkUnit is not ``BLOCKED``. Only
    `RetryOverridden` qualifies for now; a denial upholds the budget and a
    clarification answers a question, and neither is permission to run. An
    ``APPROVAL`` on a WorkUnit that halted ``WAITING_FOR_OPERATOR`` has the
    same delivery gap but different unblocking semantics, so extending this
    predicate is a decision for its own change, not a case quietly added here.

    Everything is re-read from the durable request row rather than taken from
    the caller, so the fresh resolution and the already-resolved replay are the
    same operation, and `repo.enqueue_resume` coalesces, so calling it twice
    ensures one delivery rather than two.

    The WorkUnit-status gate is why a ``WAITING_FOR_OPERATOR`` unit keeps its
    existing durable wake untouched: its milestone is parked inside a live root
    execution, and starting a rival continuation under it is exactly what a
    resume of a running unit must not do.
    """

    request = repo.get_decision_request(request_id)
    if (
        request is None
        or request.status is not DecisionRequestStatus.RESOLVED
        or request.decision is None
    ):
        return None
    outcome = decision_outcome(
        request.request_kind,
        request.decision,
        request.decision_payload,
        decided_by=request.decided_by or "operator",
    )
    if not isinstance(outcome, RetryOverridden):
        return None
    unit = repo.get_work_unit(work_unit_id)
    if unit.status is not WorkUnitStatus.BLOCKED:
        return None
    refusal = resume_refusal() if resume_refusal is not None else None
    if refusal is not None:
        return {
            "enqueued": False,
            "reason": f"{refusal}; the decision is recorded, resume manually once cleared",
        }
    if not repo.enqueue_resume(work_unit_id):
        return {
            "enqueued": False,
            "reason": (
                "a pending START delivery already exists for this work unit and "
                "will run the same root workflow"
            ),
        }
    return {
        "enqueued": True,
        "reason": (
            "the approved override unblocks this BLOCKED work unit; a pending "
            "RESUME delivery awaits the enqueue drainer"
        ),
    }


def get_work_unit(work_unit_id: str) -> WorkUnitView:
    return build_work_unit_view(work_unit_id)


def list_work_unit_events(
    work_unit_id: str,
    *,
    after_sequence: int = 0,
    limit: int = 200,
) -> tuple[dict[str, Any], ...]:
    return tuple(
        repo.event_to_payload(event)
        for event in repo.list_work_unit_events(
            work_unit_id,
            after_sequence=after_sequence,
            limit=limit,
        )
    )


def list_work_unit_artifacts(work_unit_id: str) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "artifact_id": item.artifact_id,
            # `.value`, because `ArtifactType` is a sum of dataclasses, not a
            # string. Without it this dict reaches `json.dumps` carrying a
            # `RequirableArtifact` and the CLI dies with "Object of type
            # RequirableArtifact is not JSON serializable" - on exactly the
            # WorkUnits that have artifacts, which is to say on every one worth
            # asking about. The cockpit never saw it because `projection.py`
            # builds its own view; this is the CLI and MCP path's only reader.
            "artifact_type": item.artifact_type.value,
            "uri": item.uri,
            "content_hash": item.content_hash,
            "milestone_execution_id": item.milestone_execution_id,
            "producer_workflow_id": item.producer_workflow_id,
            "producer_step_name": item.producer_step_name,
            "created_at": iso(item.created_at),
        }
        for item in repo.list_work_unit_artifacts(work_unit_id)
    )


def list_work_units(status: str | None = None) -> tuple[dict[str, Any], ...]:
    rows = repo.list_work_units(WorkUnitStatus(status) if status else None)
    return tuple(
        {
            "work_unit_id": item.work_unit_id,
            "title": item.title,
            "status": item.status.value,
            "current_phase": item.current_phase,
            "root_workflow_id": item.root_workflow_id,
            "compiled_plan_hash": item.compiled_plan_hash,
            # Provenance. Without it this list is a dead end: a reader can see
            # that six work units exist and cannot get back to the documents
            # that produced them, which is why design-doc state was tracked by
            # hand in a README instead of read from here.
            "design_doc_revision_id": item.design_doc_revision_id,
            "compiled_plan_revision_id": item.compiled_plan_revision_id,
        }
        for item in rows
    )


def list_design_docs() -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "design_doc_id": item.design_doc_id,
            "source_path": item.source_path,
            "revision_count": item.revision_count,
            "latest_revision_number": item.latest_revision_number,
            "latest_design_doc_revision_id": item.latest_design_doc_revision_id,
            "latest_plan_hash": item.latest_plan_hash,
            "latest_validation_status": item.latest_validation_status,
            "execution_blocker_count": item.execution_blocker_count,
            "work_unit_count": item.work_unit_count,
            "latest_work_unit_id": item.latest_work_unit_id,
            "latest_work_unit_status": item.latest_work_unit_status,
            "latest_work_unit_phase": item.latest_work_unit_phase,
        }
        for item in repo.list_design_docs()
    )


def pending_operator_decisions(work_unit_id: str) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "request_id": item.request_id,
            "prompt": item.prompt,
            "request_kind": item.request_kind.value,
            "milestone_execution_id": item.milestone_execution_id,
        }
        for item in repo.list_decision_requests(
            work_unit_id,
            status=DecisionRequestStatus.PENDING,
        )
    )


def compiled_plan_payload(compiled_plan_revision_id: str) -> dict[str, Any]:
    revision = repo.get_compiled_plan_revision(compiled_plan_revision_id)
    return {
        "compiled_plan_revision_id": revision.compiled_plan_revision_id,
        "design_doc_revision_id": revision.design_doc_revision_id,
        "plan_hash": revision.plan_hash,
        "validation_status": revision.validation_status.value,
        "execution_blockers": list(revision.execution_blockers),
        "plan": revision.plan.to_payload(),
    }


__all__ = [
    "CompileResult",
    "EnqueueDelivery",
    "CompiledPlanOutcome",
    "LifecyclePhase",
    "cancel_work_unit",
    "compile_design_doc_revision",
    "compile_design_doc_text",
    "compiled_plan_payload",
    "get_work_unit",
    "ingest_design_doc",
    "ingest_design_doc_file",
    "list_work_unit_artifacts",
    "list_work_unit_events",
    "list_work_units",
    "pending_operator_decisions",
    "resume_work_unit",
    "start_work_unit",
    "submit_work_unit_decision",
]
