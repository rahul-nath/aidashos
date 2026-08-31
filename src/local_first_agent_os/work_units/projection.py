# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The cockpit read model for a WorkUnit.

The operator-facing abstraction is durable lifecycle state: which phase the work is
in, which milestones exist, what is blocking, what decision is pending, and what
evidence has been produced. Model activity and token streams are not the primary
abstraction and do not appear here.

The view is built from the domain tables for speed, and ``rebuild_from_events``
recomputes the same statuses from immutable definitions plus the append-only event
log. Keeping both and testing that they agree is what makes the materialized
summary safe to read: if it ever drifted, the rebuild would disagree with it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..contracts import DispatchIntentStatus
from ..coordination.dispatch import dispatch_intent_statuses
from ..coordination.store import iso
from . import repository as repo
from .cancellation import (
    SCHEMA_VERSION_WORK_UNIT_CANCELLATION,
    StopTargetKind,
    StopVerdict,
)
from .events import DecisionRequestStatus, WorkUnitEventType
from .execution_recovery import (
    SCHEMA_VERSION_WORK_UNIT_EXECUTION_RECOVERY,
    ExecutionLiveness,
)
from .lifecycle import (
    ORDERED_PHASES,
    LifecyclePhase,
    MilestoneExecutionStatus,
    PhaseStatus,
    WorkUnitStatus,
)
from .plan import CompiledWorkPlan

SCHEMA_VERSION_WORK_UNIT_VIEW = "work_unit_view.v1"


class OperatorContract(BaseModel):
    """Base for every model the WorkUnit HTTP surface publishes.

    One config in one place, for a reason that is easy to get wrong per-model: a
    defaulted field is optional to *construct* and always present once
    *serialized*. Publishing it as optional would make every client handle an
    absence the server never produces, which is how a generated TypeScript client
    ends up full of unnecessary null checks.
    """

    model_config = ConfigDict(json_schema_serialization_defaults_required=True)


class MilestoneView(OperatorContract):
    stable_key: str
    title: str
    phase: LifecyclePhase
    ordinal: int
    executor_kind: str
    status: MilestoneExecutionStatus
    attempt: int
    requires_operator_approval: bool
    # The join key to ArtifactView.milestone_execution_id. Diagnostic evidence
    # (dispatch_failure_evidence, runner_crash_traceback) is recorded against the
    # execution, not the stable key, so without this a BLOCKED row's "why" was a
    # database query rather than a click.
    milestone_execution_id: str
    # What the design document asks of whoever runs this milestone. Carried
    # because an operator-review milestone is a task assigned to a person, and
    # the person was the only participant who could not read it: the agent lanes
    # get both in their prompt, while the cockpit showed a title and a status
    # pill. "on-device operator verification" does not say what to verify, and an
    # operator who cannot see the acceptance criteria either guesses or opens the
    # design document by hand.
    description: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    required_artifacts: tuple[str, ...] = ()
    produced_artifacts: tuple[str, ...] = ()
    child_workflow_id: str | None = None
    dispatch_intent_id: str | None = None
    # The intent's live status, read at view-build time. RUNNING with a PENDING
    # intent is a milestone that is parked waiting for a dispatcher, and RUNNING
    # with a CLAIMED one is an agent actually working; an operator watching the
    # pill cannot tell those apart from the milestone status alone, and twice in
    # one morning read "parked" as "stuck". None when no intent exists yet, and
    # on event-log rebuilds, which cannot know live queue state.
    dispatch_status: DispatchIntentStatus | None = None
    failure_code: str | None = None
    failure_summary: str | None = None
    result_summary: str | None = None


class PhaseView(OperatorContract):
    phase: LifecyclePhase
    status: PhaseStatus
    milestone_keys: tuple[str, ...]


class ArtifactView(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_serialization_defaults_required=True,
    )

    artifact_id: str
    artifact_type: str
    uri: str
    content_hash: str
    milestone_execution_id: str | None
    producer_workflow_id: str
    producer_step_name: str | None
    created_at: str


class PendingDecisionView(OperatorContract):
    request_id: str
    request_kind: str
    prompt: str
    milestone_execution_id: str | None
    created_at: str


class EventView(BaseModel):
    """One domain event, in the shape both the view and the events route return.

    ``extra="forbid"`` is the point of this model existing rather than a dict: a
    field added to ``repository.event_to_payload`` and not added here fails
    validation loudly instead of vanishing from the HTTP contract.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_serialization_defaults_required=True,
    )

    event_id: str
    sequence_number: int
    event_type: WorkUnitEventType
    phase: LifecyclePhase | None
    milestone_execution_id: str | None
    root_workflow_id: str
    child_workflow_id: str | None
    occurred_at: str
    payload: dict[str, Any]


class BlockingCondition(OperatorContract):
    """Why the WorkUnit is not moving, in the terms an operator can act on."""

    kind: Literal["NONE", "OPERATOR_DECISION", "BLOCKED_MILESTONE", "FAILED_MILESTONE"]
    detail: str
    milestone_keys: tuple[str, ...] = ()


class WorkUnitView(OperatorContract):
    schema_version: Literal["work_unit_view.v1"] = SCHEMA_VERSION_WORK_UNIT_VIEW
    work_unit_id: str
    title: str
    status: WorkUnitStatus
    current_phase: str
    design_doc_revision_id: str
    compiled_plan_revision_id: str
    compiled_plan_hash: str
    lifecycle_profile: str
    lifecycle_profile_version: int
    root_workflow_id: str
    supersedes_work_unit_id: str | None = None
    legacy_saga_id: str | None = None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    failure_code: str | None = None
    failure_summary: str | None = None
    phases: tuple[PhaseView, ...]
    milestones: tuple[MilestoneView, ...]
    blocking: BlockingCondition
    pending_decisions: tuple[PendingDecisionView, ...]
    artifacts: tuple[ArtifactView, ...]
    recent_events: tuple[EventView, ...]
    dbos_workflow_ids: tuple[str, ...] = Field(default_factory=tuple)


def _blocking_condition(
    milestones: Sequence[MilestoneView],
    pending_decision_executions: frozenset[str],
) -> BlockingCondition:
    """Name the one thing an operator should act on.

    A pending decision outranks a blocked milestone even when the milestone's
    status says BLOCKED, because the decision is the action that unblocks it. The
    order of these checks is the operator's priority order.
    """

    waiting = tuple(
        item.stable_key
        for item in milestones
        if item.status is MilestoneExecutionStatus.WAITING_FOR_OPERATOR
        or item.stable_key in pending_decision_executions
    )
    if waiting:
        return BlockingCondition(
            kind="OPERATOR_DECISION",
            detail="an operator decision is required before this work can continue",
            milestone_keys=waiting,
        )
    blocked = tuple(
        item.stable_key for item in milestones if item.status is MilestoneExecutionStatus.BLOCKED
    )
    if blocked:
        return BlockingCondition(
            kind="BLOCKED_MILESTONE",
            detail="a milestone stopped without finishing and needs recovery",
            milestone_keys=blocked,
        )
    failed = tuple(
        item.stable_key for item in milestones if item.status is MilestoneExecutionStatus.FAILED
    )
    if failed:
        return BlockingCondition(
            kind="FAILED_MILESTONE",
            detail="a milestone failed",
            milestone_keys=failed,
        )
    return BlockingCondition(kind="NONE", detail="nothing is blocking this work")


def build_work_unit_view(work_unit_id: str, *, recent_event_limit: int = 25) -> WorkUnitView:
    unit = repo.get_work_unit(work_unit_id)
    revision = repo.get_compiled_plan_revision(unit.compiled_plan_revision_id)
    plan = revision.plan
    executions = repo.list_milestone_executions(work_unit_id)
    artifacts = repo.list_work_unit_artifacts(work_unit_id)
    events = repo.list_recent_work_unit_events(work_unit_id, limit=recent_event_limit)
    phase_status = repo.phase_statuses(work_unit_id)
    pending = repo.list_decision_requests(work_unit_id, status=DecisionRequestStatus.PENDING)

    artifacts_by_execution: dict[str | None, list[str]] = {}
    for artifact in artifacts:
        artifacts_by_execution.setdefault(artifact.milestone_execution_id, []).append(
            artifact.artifact_type.value
        )

    intent_statuses = dispatch_intent_statuses(
        [item.dispatch_intent_id for item in executions if item.dispatch_intent_id]
    )
    milestone_views: list[MilestoneView] = []
    for execution in executions:
        compiled = plan.milestone(execution.stable_key)
        milestone_views.append(
            MilestoneView(
                stable_key=execution.stable_key,
                title=execution.title,
                milestone_execution_id=execution.milestone_execution_id,
                phase=execution.phase,
                ordinal=execution.ordinal,
                executor_kind=execution.executor_kind,
                status=execution.status,
                attempt=execution.attempt,
                requires_operator_approval=execution.requires_operator_approval,
                description=compiled.description,
                acceptance_criteria=compiled.acceptance_criteria,
                dependencies=compiled.dependencies,
                required_artifacts=compiled.required_artifacts,
                produced_artifacts=tuple(
                    sorted(artifacts_by_execution.get(execution.milestone_execution_id, ()))
                ),
                child_workflow_id=execution.child_workflow_id,
                dispatch_intent_id=execution.dispatch_intent_id,
                dispatch_status=(
                    intent_statuses.get(execution.dispatch_intent_id)
                    if execution.dispatch_intent_id
                    else None
                ),
                failure_code=execution.failure_code,
                failure_summary=execution.failure_summary,
                result_summary=execution.result_summary,
            )
        )

    phase_views = tuple(
        PhaseView(
            phase=phase,
            status=phase_status.get(phase, PhaseStatus.PENDING),
            milestone_keys=tuple(item.stable_key for item in plan.milestones_in_phase(phase)),
        )
        for phase in ORDERED_PHASES
    )

    workflow_ids = [unit.root_workflow_id]
    workflow_ids.extend(
        sorted({item.child_workflow_id for item in executions if item.child_workflow_id})
    )

    return WorkUnitView(
        work_unit_id=unit.work_unit_id,
        title=unit.title,
        status=unit.status,
        current_phase=unit.current_phase,
        design_doc_revision_id=unit.design_doc_revision_id,
        compiled_plan_revision_id=unit.compiled_plan_revision_id,
        compiled_plan_hash=unit.compiled_plan_hash,
        lifecycle_profile=unit.lifecycle_profile,
        lifecycle_profile_version=unit.lifecycle_profile_version,
        root_workflow_id=unit.root_workflow_id,
        supersedes_work_unit_id=unit.supersedes_work_unit_id,
        legacy_saga_id=unit.legacy_saga_id,
        created_at=iso(unit.created_at),
        started_at=iso(unit.started_at) if unit.started_at else None,
        completed_at=iso(unit.completed_at) if unit.completed_at else None,
        failure_code=unit.failure_code,
        failure_summary=unit.failure_summary,
        phases=phase_views,
        milestones=tuple(milestone_views),
        blocking=_blocking_condition(
            milestone_views,
            frozenset(
                execution.stable_key
                for execution in executions
                for item in pending
                if item.milestone_execution_id == execution.milestone_execution_id
            ),
        ),
        pending_decisions=tuple(
            PendingDecisionView(
                request_id=item.request_id,
                request_kind=item.request_kind.value,
                prompt=item.prompt,
                milestone_execution_id=item.milestone_execution_id,
                created_at=iso(item.created_at),
            )
            for item in pending
        ),
        artifacts=tuple(
            ArtifactView(
                artifact_id=item.artifact_id,
                artifact_type=item.artifact_type.value,
                uri=item.uri,
                content_hash=item.content_hash,
                milestone_execution_id=item.milestone_execution_id,
                producer_workflow_id=item.producer_workflow_id,
                producer_step_name=item.producer_step_name,
                created_at=iso(item.created_at),
            )
            for item in artifacts
        ),
        recent_events=tuple(
            EventView(
                event_id=item.event_id,
                sequence_number=item.sequence_number,
                event_type=item.event_type,
                phase=item.phase,
                milestone_execution_id=item.milestone_execution_id,
                root_workflow_id=item.root_workflow_id,
                child_workflow_id=item.child_workflow_id,
                occurred_at=iso(item.occurred_at),
                payload=item.payload,
            )
            for item in events
        ),
        dbos_workflow_ids=tuple(workflow_ids),
    )


# --------------------------------------------------------------------------- #
# HTTP response contracts
#
# These are declared as `response_model` on the routes, which is what makes
# `/openapi.json` describe real shapes instead of bare objects, and therefore
# what lets the TypeScript client be generated rather than transcribed. Each one
# forbids extra fields, so a service payload that grows a key fails validation
# here instead of silently leaving the published contract behind.
# --------------------------------------------------------------------------- #


class WorkUnitSummary(OperatorContract):
    """One row of the WorkUnit list: enough to choose one, not enough to work it."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_serialization_defaults_required=True,
    )

    work_unit_id: str
    title: str
    status: WorkUnitStatus
    current_phase: str
    root_workflow_id: str
    compiled_plan_hash: str
    # Provenance, so a row in this list can be traced back to the document that
    # produced it. Without these the list is a dead end: a reader sees that runs
    # exist and has no way to reach the design doc behind one.
    design_doc_revision_id: str
    compiled_plan_revision_id: str


class WorkUnitIndex(OperatorContract):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_serialization_defaults_required=True,
    )

    work_units: list[WorkUnitSummary]


class WorkUnitEventPage(OperatorContract):
    """A forward-only window into the append-only history.

    The cursor is the caller's own ``after_sequence``; there is no server cursor,
    because WorkUnit sequence numbers are dense and monotonic within one WorkUnit.
    """

    model_config = ConfigDict(extra="forbid")

    work_unit_id: str
    events: list[EventView]


class WorkUnitArtifactList(OperatorContract):
    model_config = ConfigDict(extra="forbid")

    work_unit_id: str
    artifacts: list[ArtifactView]


class ResumeDeliveryView(OperatorContract):
    """What a decision that unblocks a BLOCKED WorkUnit did about resuming it."""

    model_config = ConfigDict(extra="forbid")

    enqueued: bool
    reason: str


class WorkUnitDecisionResult(OperatorContract):
    """What submitting one operator decision did.

    ``applied`` false with a ``reason`` is the idempotent case: the request was
    already resolved, and saying so is not an error.

    ``resume`` is absent for the decisions that unblock nothing; present, it
    says whether a RESUME delivery now awaits the enqueue drainer and why or
    why not.
    """

    model_config = ConfigDict(extra="forbid")

    work_unit_id: str
    request_id: str
    decision: str | None = None
    applied: bool
    milestone_key: str | None = None
    sequence_number: int | None = None
    reason: str | None = None
    resume: ResumeDeliveryView | None = None


class StopAttemptView(OperatorContract):
    """One thing cancellation tried to stop, as the cockpit sees it."""

    model_config = ConfigDict(extra="forbid")

    kind: StopTargetKind
    identifier: str
    verdict: StopVerdict
    detail: str = ""


class WorkUnitCancelResult(OperatorContract):
    model_config = ConfigDict(extra="forbid")

    work_unit_id: str
    status: WorkUnitStatus
    cancelled: bool
    cancelled_milestones: tuple[str, ...] = ()
    reason: str | None = None
    schema_version: str = SCHEMA_VERSION_WORK_UNIT_CANCELLATION
    attempts: tuple[StopAttemptView, ...] = ()
    refused: tuple[StopAttemptView, ...] = ()
    """What nothing here will stop. This is the field an operator must read.

    A cancel that reports `cancelled: true` while this is non-empty means the
    ledger has stopped but something in the world has not, and the operator's
    next move is to go and kill it. Publishing it is the difference between the
    cockpit telling the truth and repeating the old lie one layer up.
    """

    awaiting_stop: tuple[StopAttemptView, ...] = ()
    """Asked to stop cooperatively; a supervisor acts at its next heartbeat."""


class WorkUnitExecutionResult(OperatorContract):
    """The root workflow's own report, returned only when it ran in-process."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str
    work_unit_id: str
    root_workflow_id: str
    compiled_plan_hash: str
    status: WorkUnitStatus
    current_phase: str
    phase_statuses: dict[LifecyclePhase, PhaseStatus]
    milestone_statuses: dict[str, MilestoneExecutionStatus]


class ExecutionRecoveryView(OperatorContract):
    """What a resume had to repair before it could continue anything.

    An operator otherwise cannot tell "this continued a run that parked for me"
    from "this recovered a run that crashed", because both return the same
    accepted resume. ``halted_epoch`` is the epoch whose death had gone
    unrecorded, and ``null`` whenever nothing needed repairing.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION_WORK_UNIT_EXECUTION_RECOVERY
    liveness: ExecutionLiveness
    execution_workflow_id: str
    halted_epoch: int | None = None
    abandoned_milestones: tuple[str, ...] = ()


class ChargedFailureBudgetView(OperatorContract):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["charged_failure_budget"]
    max_charged_failures: int


class OperatorOnlyRetryView(OperatorContract):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["operator_only"]


type RetryPolicyView = ChargedFailureBudgetView | OperatorOnlyRetryView


class ExhaustedMilestoneView(OperatorContract):
    """A milestone the resume refused to try again, and the arithmetic behind it.

    An operator asking "why is this still blocked after I resumed it" needs the
    numbers, not a sentence. ``override_request_id`` names the decision they can
    answer to lift it, so the refusal carries its own remedy.
    """

    model_config = ConfigDict(extra="forbid")

    milestone_key: str
    phase: LifecyclePhase
    execution_ordinal: int
    charged_failures: int
    retry_policy: RetryPolicyView
    override_request_id: str


class WorkUnitResumeResult(OperatorContract):
    """Whether a resume was accepted, and by what.

    ``delivered`` false is a real answer rather than a failure: no durable runtime
    was available to take the continuation, so nothing was started.
    """

    model_config = ConfigDict(extra="forbid")

    work_unit_id: str
    continuation_of: str
    delivered: bool
    durable: bool
    workflow_id: str | None = None
    reason: str | None = None
    resume_enqueued: bool | None = None
    """Whether an undelivered resume left a pending RESUME outbox row.

    ``True`` means the resident enqueue drainer delivers the continuation on
    its next pass, so ``delivered`` false is a promise rather than a stall.
    Absent on a delivered or inline resume, where there is nothing to queue.
    """

    result: WorkUnitExecutionResult | None = None
    recovered: ExecutionRecoveryView | None = None
    """What the resume repaired on its way in, when it repaired anything."""

    exhausted: tuple[ExhaustedMilestoneView, ...] = ()
    """Milestones this resume refused to retry, and why.

    Empty is the ordinary answer. A non-empty list means the resume was accepted
    and did less than it was asked to, which is a distinct outcome from both
    "resumed everything" and "refused the whole resume" - and one that used to be
    unsayable, because resume turned every blocked milestone READY whatever its
    plan permitted.
    """


class RebuiltState(OperatorContract):
    """Lifecycle state recomputed from definitions plus events, nothing else."""

    status: WorkUnitStatus
    current_phase: str
    phase_statuses: dict[LifecyclePhase, PhaseStatus]
    milestone_statuses: dict[str, MilestoneExecutionStatus]
    artifact_types: tuple[str, ...]
    pending_decision_ids: tuple[str, ...]


_PHASE_STATUS_BY_EVENT: dict[WorkUnitEventType, PhaseStatus] = {
    WorkUnitEventType.PHASE_PENDING: PhaseStatus.PENDING,
    WorkUnitEventType.PHASE_STARTED: PhaseStatus.RUNNING,
    WorkUnitEventType.PHASE_COMPLETED: PhaseStatus.SUCCEEDED,
    WorkUnitEventType.PHASE_SKIPPED: PhaseStatus.SKIPPED,
    WorkUnitEventType.PHASE_BLOCKED: PhaseStatus.BLOCKED,
    WorkUnitEventType.PHASE_FAILED: PhaseStatus.FAILED,
    WorkUnitEventType.PHASE_CANCELLED: PhaseStatus.CANCELLED,
}

_MILESTONE_STATUS_BY_EVENT: dict[WorkUnitEventType, MilestoneExecutionStatus] = {
    WorkUnitEventType.MILESTONE_PENDING: MilestoneExecutionStatus.PENDING,
    WorkUnitEventType.MILESTONE_READY: MilestoneExecutionStatus.READY,
    WorkUnitEventType.MILESTONE_STARTED: MilestoneExecutionStatus.RUNNING,
    WorkUnitEventType.MILESTONE_WAITING_FOR_OPERATOR: (
        MilestoneExecutionStatus.WAITING_FOR_OPERATOR
    ),
    WorkUnitEventType.MILESTONE_BLOCKED: MilestoneExecutionStatus.BLOCKED,
    WorkUnitEventType.MILESTONE_SUCCEEDED: MilestoneExecutionStatus.SUCCEEDED,
    WorkUnitEventType.MILESTONE_FAILED: MilestoneExecutionStatus.FAILED,
    WorkUnitEventType.MILESTONE_SKIPPED: MilestoneExecutionStatus.SKIPPED,
    WorkUnitEventType.MILESTONE_CANCELLED: MilestoneExecutionStatus.CANCELLED,
}

_WORK_UNIT_STATUS_BY_EVENT: dict[WorkUnitEventType, WorkUnitStatus] = {
    WorkUnitEventType.WORK_UNIT_CREATED: WorkUnitStatus.QUEUED,
    WorkUnitEventType.WORK_UNIT_COMPILED: WorkUnitStatus.COMPILED,
    WorkUnitEventType.WORK_UNIT_QUEUED: WorkUnitStatus.QUEUED,
    WorkUnitEventType.WORK_UNIT_STARTED: WorkUnitStatus.RUNNING,
    WorkUnitEventType.WORK_UNIT_WAITING_FOR_OPERATOR: WorkUnitStatus.WAITING_FOR_OPERATOR,
    WorkUnitEventType.WORK_UNIT_BLOCKED: WorkUnitStatus.BLOCKED,
    WorkUnitEventType.WORK_UNIT_SUCCEEDED: WorkUnitStatus.SUCCEEDED,
    WorkUnitEventType.WORK_UNIT_FAILED: WorkUnitStatus.FAILED,
    WorkUnitEventType.WORK_UNIT_CANCELLED: WorkUnitStatus.CANCELLED,
    WorkUnitEventType.WORK_UNIT_SUPERSEDED: WorkUnitStatus.SUPERSEDED,
}


def rebuild_from_events(
    plan: CompiledWorkPlan,
    events: Iterable[repo.WorkUnitEventRow],
) -> RebuiltState:
    """Recompute lifecycle state from immutable definitions plus the event log.

    Events are replayed in sequence order, so two sibling milestones completing in
    either order converge on the same result: each event only sets the state of the
    thing it names.
    """

    status = WorkUnitStatus.QUEUED
    current_phase = ORDERED_PHASES[0].value
    phase_statuses: dict[LifecyclePhase, PhaseStatus] = {}
    milestone_statuses: dict[str, MilestoneExecutionStatus] = {
        milestone.stable_key: MilestoneExecutionStatus.PENDING for milestone in plan.milestones
    }
    artifact_types: set[str] = set()
    pending: dict[str, bool] = {}

    for event in sorted(events, key=lambda item: item.sequence_number):
        work_unit_status = _WORK_UNIT_STATUS_BY_EVENT.get(event.event_type)
        if work_unit_status is not None:
            status = work_unit_status
        phase_status = _PHASE_STATUS_BY_EVENT.get(event.event_type)
        if phase_status is not None and event.phase is not None:
            phase_statuses[event.phase] = phase_status
        milestone_status = _MILESTONE_STATUS_BY_EVENT.get(event.event_type)
        if milestone_status is not None:
            key = str(event.payload.get("milestone_key") or "")
            if key:
                milestone_statuses[key] = milestone_status
        if event.event_type is WorkUnitEventType.APPROVAL_REQUESTED:
            pending[str(event.payload["request_id"])] = True
        if event.event_type is WorkUnitEventType.APPROVAL_RECEIVED:
            pending[str(event.payload["request_id"])] = False
        for artifact in event.payload.get("artifacts", ()) or ():
            artifact_types.add(str(artifact["artifact_type"]))
        single = event.payload.get("artifact")
        if isinstance(single, dict):
            artifact_types.add(str(single["artifact_type"]))
        phase_value = event.payload.get("current_phase") or (
            event.phase.value if event.phase is not None else None
        )
        if phase_value:
            current_phase = str(phase_value)

    return RebuiltState(
        status=status,
        current_phase=current_phase,
        phase_statuses=phase_statuses,
        milestone_statuses=milestone_statuses,
        artifact_types=tuple(sorted(artifact_types)),
        pending_decision_ids=tuple(
            sorted(request_id for request_id, open_ in pending.items() if open_)
        ),
    )


__all__ = [
    "SCHEMA_VERSION_WORK_UNIT_VIEW",
    "ArtifactView",
    "BlockingCondition",
    "EventView",
    "MilestoneView",
    "PendingDecisionView",
    "PhaseView",
    "RebuiltState",
    "ResumeDeliveryView",
    "WorkUnitArtifactList",
    "WorkUnitCancelResult",
    "WorkUnitDecisionResult",
    "WorkUnitEventPage",
    "WorkUnitExecutionResult",
    "WorkUnitIndex",
    "WorkUnitResumeResult",
    "WorkUnitSummary",
    "WorkUnitView",
    "build_work_unit_view",
    "rebuild_from_events",
]
