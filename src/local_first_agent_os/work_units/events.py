# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The facts a WorkUnit's history is made of.

Everything that changes WorkUnit state is expressed as a fact submitted to one
canonical transition operation. A dispatcher, an operator, a phase workflow, and
the failure harness all speak the same vocabulary, so no caller has private
knowledge of how to write a status.

The variance between facts lives in the status enums rather than in a family of
near-identical classes: a milestone fact is "this milestone reached this status,
with this evidence". The event type is derived from the status, which is what
keeps the event log and the summary tables from being able to disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final, Literal, overload

from .lifecycle import (
    FailureClass,
    LifecyclePhase,
    MilestoneExecutionStatus,
    PhaseStatus,
    WorkUnitPhaseMarker,
    WorkUnitStatus,
)


class WorkUnitEventType(StrEnum):
    WORK_UNIT_CREATED = "WORK_UNIT_CREATED"
    PLAN_BOUND = "PLAN_BOUND"
    ROOT_WORKFLOW_ENQUEUED = "ROOT_WORKFLOW_ENQUEUED"
    WORK_UNIT_STARTED = "WORK_UNIT_STARTED"
    WORK_UNIT_WAITING_FOR_OPERATOR = "WORK_UNIT_WAITING_FOR_OPERATOR"
    WORK_UNIT_BLOCKED = "WORK_UNIT_BLOCKED"
    WORK_UNIT_SUCCEEDED = "WORK_UNIT_SUCCEEDED"
    WORK_UNIT_FAILED = "WORK_UNIT_FAILED"
    WORK_UNIT_CANCELLING = "WORK_UNIT_CANCELLING"
    WORK_UNIT_CANCELLED = "WORK_UNIT_CANCELLED"
    WORK_UNIT_SUPERSEDED = "WORK_UNIT_SUPERSEDED"
    WORK_UNIT_COMPILED = "WORK_UNIT_COMPILED"
    WORK_UNIT_QUEUED = "WORK_UNIT_QUEUED"

    PHASE_STARTED = "PHASE_STARTED"
    PHASE_SKIPPED = "PHASE_SKIPPED"
    PHASE_COMPLETED = "PHASE_COMPLETED"
    PHASE_BLOCKED = "PHASE_BLOCKED"
    PHASE_FAILED = "PHASE_FAILED"
    PHASE_CANCELLED = "PHASE_CANCELLED"
    PHASE_PENDING = "PHASE_PENDING"

    MILESTONE_PENDING = "MILESTONE_PENDING"
    MILESTONE_READY = "MILESTONE_READY"
    MILESTONE_STARTED = "MILESTONE_STARTED"
    MILESTONE_WAITING_FOR_OPERATOR = "MILESTONE_WAITING_FOR_OPERATOR"
    MILESTONE_BLOCKED = "MILESTONE_BLOCKED"
    MILESTONE_SUCCEEDED = "MILESTONE_SUCCEEDED"
    MILESTONE_FAILED = "MILESTONE_FAILED"
    MILESTONE_SKIPPED = "MILESTONE_SKIPPED"
    MILESTONE_CANCELLED = "MILESTONE_CANCELLED"

    ARTIFACT_RECORDED = "ARTIFACT_RECORDED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_RECEIVED = "APPROVAL_RECEIVED"
    DISPATCH_INTENT_CREATED = "DISPATCH_INTENT_CREATED"
    AUTOMATIC_CRASH_RECOVERY = "AUTOMATIC_CRASH_RECOVERY"


_WORK_UNIT_EVENT_BY_STATUS: Final[dict[WorkUnitStatus, WorkUnitEventType]] = {
    WorkUnitStatus.DRAFT: WorkUnitEventType.WORK_UNIT_CREATED,
    WorkUnitStatus.COMPILED: WorkUnitEventType.WORK_UNIT_COMPILED,
    WorkUnitStatus.QUEUED: WorkUnitEventType.WORK_UNIT_QUEUED,
    WorkUnitStatus.RUNNING: WorkUnitEventType.WORK_UNIT_STARTED,
    WorkUnitStatus.WAITING_FOR_OPERATOR: WorkUnitEventType.WORK_UNIT_WAITING_FOR_OPERATOR,
    WorkUnitStatus.BLOCKED: WorkUnitEventType.WORK_UNIT_BLOCKED,
    WorkUnitStatus.SUCCEEDED: WorkUnitEventType.WORK_UNIT_SUCCEEDED,
    WorkUnitStatus.FAILED: WorkUnitEventType.WORK_UNIT_FAILED,
    WorkUnitStatus.CANCELLING: WorkUnitEventType.WORK_UNIT_CANCELLING,
    WorkUnitStatus.CANCELLED: WorkUnitEventType.WORK_UNIT_CANCELLED,
    WorkUnitStatus.SUPERSEDED: WorkUnitEventType.WORK_UNIT_SUPERSEDED,
}

_PHASE_EVENT_BY_STATUS: Final[dict[PhaseStatus, WorkUnitEventType]] = {
    PhaseStatus.PENDING: WorkUnitEventType.PHASE_PENDING,
    PhaseStatus.RUNNING: WorkUnitEventType.PHASE_STARTED,
    PhaseStatus.SUCCEEDED: WorkUnitEventType.PHASE_COMPLETED,
    PhaseStatus.SKIPPED: WorkUnitEventType.PHASE_SKIPPED,
    PhaseStatus.BLOCKED: WorkUnitEventType.PHASE_BLOCKED,
    PhaseStatus.FAILED: WorkUnitEventType.PHASE_FAILED,
    PhaseStatus.CANCELLED: WorkUnitEventType.PHASE_CANCELLED,
}

_MILESTONE_EVENT_BY_STATUS: Final[dict[MilestoneExecutionStatus, WorkUnitEventType]] = {
    MilestoneExecutionStatus.PENDING: WorkUnitEventType.MILESTONE_PENDING,
    MilestoneExecutionStatus.READY: WorkUnitEventType.MILESTONE_READY,
    MilestoneExecutionStatus.RUNNING: WorkUnitEventType.MILESTONE_STARTED,
    MilestoneExecutionStatus.WAITING_FOR_OPERATOR: (
        WorkUnitEventType.MILESTONE_WAITING_FOR_OPERATOR
    ),
    MilestoneExecutionStatus.BLOCKED: WorkUnitEventType.MILESTONE_BLOCKED,
    MilestoneExecutionStatus.SUCCEEDED: WorkUnitEventType.MILESTONE_SUCCEEDED,
    MilestoneExecutionStatus.FAILED: WorkUnitEventType.MILESTONE_FAILED,
    MilestoneExecutionStatus.SKIPPED: WorkUnitEventType.MILESTONE_SKIPPED,
    MilestoneExecutionStatus.CANCELLED: WorkUnitEventType.MILESTONE_CANCELLED,
}


class DecisionRequestKind(StrEnum):
    APPROVAL = "APPROVAL"
    CLARIFICATION = "CLARIFICATION"
    RETRY_BUDGET_OVERRIDE = "RETRY_BUDGET_OVERRIDE"
    """A person deciding that an exhausted attempt budget should not stop a retry.

    A third kind rather than a flag on resume, because the machinery an override
    needs already exists here and nowhere else: a durable request row, an
    idempotency key, a status machine that refuses a second answer, and a
    published pending-decision surface an operator can see. A boolean parameter
    on the resume route would have none of those, and "who decided this and when"
    is exactly what an override has to be able to answer.
    """


class DecisionRequestStatus(StrEnum):
    PENDING = "PENDING"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"


class OperatorDecision(StrEnum):
    """The wire spelling of an operator's answer.

    Kept because it is what HTTP carries and what the decision row stores. It is
    deliberately *not* what the lifecycle reasons about: on its own it cannot say
    which kind of request it belongs to, so an `ANSWERED` and an `APPROVED` are
    interchangeable to any code holding one. `decision_outcome` turns this into
    the typed pairing below at the one boundary where the two are both known.
    """

    APPROVED = "APPROVED"
    DENIED = "DENIED"
    ANSWERED = "ANSWERED"


class DecisionKindMismatch(ValueError):
    """An operator decision that does not belong to the request kind it names.

    A programmer or operator error rather than a runtime failure, so it raises.
    The alternative this replaces was silent: an `ANSWERED` reached an approval
    gate that tested only for `DENIED` and was read as consent.
    """


@dataclass(frozen=True)
class Approved:
    """An APPROVAL request answered yes."""

    decided_by: str = "operator"


@dataclass(frozen=True)
class Denied:
    """An APPROVAL request answered no."""

    decided_by: str = "operator"


@dataclass(frozen=True)
class RetryOverridden:
    """A RETRY_BUDGET_OVERRIDE request answered yes.

    Distinct from `Approved` because it authorises something different: an
    approval says the work may proceed, an override says the plan's own bound may
    be exceeded. Sharing one type would let a milestone approval be read as
    permission to retry past the budget.
    """

    decided_by: str = "operator"


@dataclass(frozen=True)
class RetryRefusalUpheld:
    """A RETRY_BUDGET_OVERRIDE request answered no; the budget stands."""

    decided_by: str = "operator"


@dataclass(frozen=True)
class Answered:
    """A CLARIFICATION request answered, carrying the answer.

    The payload is the reason this is a distinct type rather than a third member
    of an enum: an answer has content and an approval does not, so the two do not
    have the same shape and should not share one.
    """

    payload: dict[str, Any] = field(default_factory=dict)
    decided_by: str = "operator"


ApprovalOutcome = Approved | Denied
"""Everything an APPROVAL request may be resolved with, and nothing else.

The gate that consumes this takes `ApprovalOutcome`, so handing it a
clarification answer is a type error rather than a runtime check somebody has to
remember to write.
"""

ClarificationOutcome = Answered
"""Everything a CLARIFICATION request may be resolved with."""

RetryOverrideOutcome = RetryOverridden | RetryRefusalUpheld
"""Everything a RETRY_BUDGET_OVERRIDE request may be resolved with."""

OperatorDecisionOutcome = ApprovalOutcome | ClarificationOutcome | RetryOverrideOutcome


@overload
def decision_outcome(
    kind: Literal[DecisionRequestKind.APPROVAL],
    decision: OperatorDecision,
    payload: dict[str, Any] | None = ...,
    decided_by: str = ...,
) -> ApprovalOutcome: ...


@overload
def decision_outcome(
    kind: Literal[DecisionRequestKind.CLARIFICATION],
    decision: OperatorDecision,
    payload: dict[str, Any] | None = ...,
    decided_by: str = ...,
) -> ClarificationOutcome: ...


@overload
def decision_outcome(
    kind: Literal[DecisionRequestKind.RETRY_BUDGET_OVERRIDE],
    decision: OperatorDecision,
    payload: dict[str, Any] | None = ...,
    decided_by: str = ...,
) -> RetryOverrideOutcome: ...


@overload
def decision_outcome(
    kind: DecisionRequestKind,
    decision: OperatorDecision,
    payload: dict[str, Any] | None = ...,
    decided_by: str = ...,
) -> OperatorDecisionOutcome: ...


def decision_outcome(
    kind: DecisionRequestKind,
    decision: OperatorDecision,
    payload: dict[str, Any] | None = None,
    decided_by: str = "operator",
) -> OperatorDecisionOutcome:
    """Pair a request kind with an answer, or refuse the pairing.

    The single place an untyped `(kind, decision)` pair becomes a value the
    lifecycle can act on. Everything downstream holds the result, so no other
    code has to know which decisions belong to which requests.

    The overloads are what make this more than a runtime check: a caller passing
    `DecisionRequestKind.APPROVAL` as a literal gets `ApprovalOutcome` back, so a
    branch handling `Answered` there is unreachable code the type checker
    reports, and a gate declared to take `ApprovalOutcome` cannot be handed one.
    """

    match kind, decision:
        case DecisionRequestKind.APPROVAL, OperatorDecision.APPROVED:
            return Approved(decided_by=decided_by)
        case DecisionRequestKind.APPROVAL, OperatorDecision.DENIED:
            return Denied(decided_by=decided_by)
        case DecisionRequestKind.CLARIFICATION, OperatorDecision.ANSWERED:
            return Answered(payload=dict(payload or {}), decided_by=decided_by)
        case DecisionRequestKind.RETRY_BUDGET_OVERRIDE, OperatorDecision.APPROVED:
            return RetryOverridden(decided_by=decided_by)
        case DecisionRequestKind.RETRY_BUDGET_OVERRIDE, OperatorDecision.DENIED:
            return RetryRefusalUpheld(decided_by=decided_by)
    permitted = (
        f"{OperatorDecision.APPROVED.value} or {OperatorDecision.DENIED.value}"
        if kind in {DecisionRequestKind.APPROVAL, DecisionRequestKind.RETRY_BUDGET_OVERRIDE}
        else OperatorDecision.ANSWERED.value
    )
    raise DecisionKindMismatch(
        f"a {kind.value} request cannot be resolved by {decision.value}; it takes {permitted}"
    )


class ArtifactKind(StrEnum):
    """The closed set of evidence a milestone may be required to produce.

    Closed on purpose. A required artifact type is a promise that something on
    the execution path can produce it, and a free-form string cannot make that
    promise: a document asking for ``design_review_notes`` compiles happily and
    then requires evidence no executor emits, which fails an hour into a run
    instead of at compile time.

    Membership here is the compiler's satisfiability check. Adding a member
    means committing a producer for it in the same change, because a kind
    nothing emits is exactly the unsatisfiable requirement this type exists to
    make unrepresentable.

    ``StrEnum`` so a member serializes as its own name. The compiled plan hashes
    the artifact types it requires, and a representation change would rewrite
    every plan hash ever computed.
    """

    CLARIFICATION_RECORD = "clarification_record"
    ENVIRONMENT_REPORT = "environment_report"
    IMPLEMENTATION_PLAN = "implementation_plan"
    SOURCE_PATCH = "source_patch"
    TEST_RESULT = "test_result"
    ACCEPTANCE_REPORT = "acceptance_report"
    REVIEW_DECISION = "review_decision"
    OPERATOR_APPROVAL = "operator_approval"
    DELIVERY_RECORD = "delivery_record"
    DEPLOYMENT_RECORD = "deployment_record"


class DiagnosticArtifactKind(StrEnum):
    """Evidence a milestone may *produce* but may never be *required* to.

    The sibling of ``ArtifactKind``, and separate from it for one reason: these
    exist only when something went wrong. Membership in ``ArtifactKind`` is the
    compiler's satisfiability check, so a kind that no successful run can emit
    would be a requirement no document could honestly state, and adding one
    there would rewrite every plan hash ever computed besides.

    Closed sets rather than one open string, joined by ``ArtifactType`` below,
    which is what makes "may a document require this?" a question the type
    answers instead of one the reader has to know the convention for.
    """

    DISPATCH_FAILURE_EVIDENCE = "dispatch_failure_evidence"
    RUNNER_CRASH_TRACEBACK = "runner_crash_traceback"


class TraceArtifactKind(StrEnum):
    """How a run proceeded, which no document may require.

    The third population, and it exists because the first two do not cover a
    successful run's own record of itself. ``ArtifactKind`` is what a milestone
    promises to produce, so membership there is a satisfiability claim.
    ``DiagnosticArtifactKind`` is failure evidence, and a trace from a run that
    succeeded is not that. Filing traces under either would make one of those
    two docstrings false.

    Never requirable, for the same reason a diagnostic is not: a document that
    could demand a trace would be demanding a particular execution shape rather
    than a result, and a run that reached the right answer in one turn would
    fail for having nothing to show.
    """

    TOOL_CALL_TRANSCRIPT = "tool_call_transcript"


@dataclass(frozen=True)
class RequirableArtifact:
    """Evidence a compiled plan may name, and an executor promises to produce."""

    kind: ArtifactKind

    @property
    def value(self) -> str:
        return self.kind.value


@dataclass(frozen=True)
class DiagnosticArtifact:
    """Evidence only a failure produces, which no document may require."""

    kind: DiagnosticArtifactKind

    @property
    def value(self) -> str:
        return self.kind.value


@dataclass(frozen=True)
class TraceArtifact:
    """A record of how a run proceeded, which no document may require."""

    kind: TraceArtifactKind

    @property
    def value(self) -> str:
        return self.kind.value


@dataclass(frozen=True)
class UnrecognizedArtifact:
    """An ``artifact_type`` read back from storage that this build does not know.

    Not a defect and not a programmer error. The ledger outlives any one build:
    rows survive a kind being renamed or retired, and a reader that crashed on
    them would make an old row a poison pill for the whole WorkUnit. Keeping the
    raw string in a case of its own is what lets the read path stay total while
    still refusing to let an unknown type masquerade as requirable evidence.
    """

    raw: str

    @property
    def value(self) -> str:
        return self.raw


ArtifactType = RequirableArtifact | DiagnosticArtifact | TraceArtifact | UnrecognizedArtifact
"""What an artifact claims to be, as four populations rather than one string.

The distinction that matters is the first case against all the others. Milestone
completion is decided by whether required evidence arrived, and only a
``RequirableArtifact`` can ever satisfy a requirement: a diagnostic exists
precisely because a run failed, so counting one as evidence would let a failed
run report itself complete, and a trace describes how a run went rather than
what it produced. That rule used to live in whoever remembered which strings
were special. Now it is `isinstance`.

Adding a population is the intended way to grow this. The alternative each time
is a new member of ``ArtifactKind``, which is the one set where membership is a
promise that an executor emits it, and where growth is therefore expensive.

Serialization is deliberately unchanged: every case renders as the same string
the column has always held, so the ledger schema, the published API type, and
every plan hash ever computed stay exactly as they are. A plan hash covers the
artifact types a document *requires*, and no case but the first can appear
there, so a new non-requirable population cannot move one.
"""


def parse_artifact_type(raw: str) -> ArtifactType:
    """Classify a stored ``artifact_type``, total over every possible string."""

    try:
        return RequirableArtifact(ArtifactKind(raw))
    except ValueError:
        pass
    try:
        return DiagnosticArtifact(DiagnosticArtifactKind(raw))
    except ValueError:
        pass
    try:
        return TraceArtifact(TraceArtifactKind(raw))
    except ValueError:
        return UnrecognizedArtifact(raw)


@dataclass(frozen=True)
class ArtifactRecord:
    """Durable evidence that a milestone did what it claimed.

    ``content_hash`` is required, not optional: an artifact whose content cannot
    be identified is not evidence, and letting it in would make "milestone
    complete" mean "an agent said so".

    ``artifact_type`` is an ``ArtifactType`` rather than a ``str`` so that a
    record carries which population it belongs to. Constructing one from a
    stored or agent-supplied string goes through ``parse_artifact_type``, which
    is the single place an unknown name is allowed to become an artifact at all.
    """

    artifact_type: ArtifactType
    uri: str
    content_hash: str
    media_type: str | None = None
    size_bytes: int | None = None
    producer_step_name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def satisfies_requirement(self) -> bool:
        """Whether this artifact can discharge a milestone's required evidence."""

        return isinstance(self.artifact_type, RequirableArtifact)

    def to_payload(self) -> dict[str, Any]:
        return {
            "artifact_type": self.artifact_type.value,
            "uri": self.uri,
            "content_hash": self.content_hash,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "producer_step_name": self.producer_step_name,
            "metadata": dict(sorted(self.metadata.items())),
        }


@dataclass(frozen=True)
class WorkUnitTransition:
    """The WorkUnit summary reached a new status."""

    status: WorkUnitStatus
    current_phase: LifecyclePhase | WorkUnitPhaseMarker | None = None
    failure_code: str | None = None
    failure_summary: str | None = None
    reason: str | None = None
    epoch: int = 0
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def event_type(self) -> WorkUnitEventType:
        return _WORK_UNIT_EVENT_BY_STATUS[self.status]

    @property
    def transition_name(self) -> str:
        return self.event_type.value

    @property
    def phase(self) -> LifecyclePhase | None:
        return self.current_phase if isinstance(self.current_phase, LifecyclePhase) else None

    @property
    def milestone_key(self) -> str | None:
        return None

    @property
    def attempt(self) -> int:
        """The execution epoch, which is what makes a resumed run distinguishable.

        Without it, the second execution's ``WORK_UNIT_STARTED`` would compute the
        same idempotency key as the first and be absorbed as a duplicate, leaving a
        resumed WorkUnit reporting the status it had before the resume.
        """

        return self.epoch

    def event_payload(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "current_phase": (self.current_phase.value if self.current_phase is not None else None),
            "failure_code": self.failure_code,
            "failure_summary": self.failure_summary,
            "reason": self.reason,
            **self.payload,
        }


@dataclass(frozen=True)
class PhaseTransition:
    """One lifecycle phase reached a new status.

    A phase has no row of its own: it is the aggregate of its milestones plus
    these events. Recording the transition durably is what makes ``SKIPPED``
    provable rather than inferred from an absence of milestones.
    """

    phase: LifecyclePhase
    status: PhaseStatus
    reason: str | None = None
    epoch: int = 0
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def event_type(self) -> WorkUnitEventType:
        return _PHASE_EVENT_BY_STATUS[self.status]

    @property
    def transition_name(self) -> str:
        return self.event_type.value

    @property
    def milestone_key(self) -> str | None:
        return None

    @property
    def attempt(self) -> int:
        """The execution epoch, so a phase retried after a resume is its own fact."""

        return self.epoch

    def event_payload(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "status": self.status.value,
            "reason": self.reason,
            "epoch": self.epoch,
            **self.payload,
        }


@dataclass(frozen=True)
class MilestoneTransition:
    """One milestone execution reached a new status, with its evidence."""

    phase: LifecyclePhase
    milestone_key: str
    status: MilestoneExecutionStatus
    attempt: int = 1
    child_workflow_id: str | None = None
    dispatch_intent_id: str | None = None
    result_summary: str | None = None
    failure_code: str | None = None
    failure_summary: str | None = None
    failure_class: FailureClass | None = None
    """How the failure must be handled, alongside the code that names it.

    The code is free text and is written in four places; the class is the closed
    vocabulary the scheduler already branched on. Only the class answers "did
    this BLOCKED milestone spend an attempt?", and it used to be discarded at the
    write, so a retry budget had no honest way to ask.
    """

    artifacts: tuple[ArtifactRecord, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def event_type(self) -> WorkUnitEventType:
        return _MILESTONE_EVENT_BY_STATUS[self.status]

    @property
    def transition_name(self) -> str:
        return self.event_type.value

    def event_payload(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "milestone_key": self.milestone_key,
            "status": self.status.value,
            "attempt": self.attempt,
            "child_workflow_id": self.child_workflow_id,
            "dispatch_intent_id": self.dispatch_intent_id,
            "result_summary": self.result_summary,
            "failure_code": self.failure_code,
            "failure_summary": self.failure_summary,
            "failure_class": self.failure_class.value if self.failure_class else None,
            "artifacts": [artifact.to_payload() for artifact in self.artifacts],
            **self.payload,
        }


@dataclass(frozen=True)
class ApprovalRequested:
    """An operator decision was persisted and the lifecycle is now waiting on it."""

    phase: LifecyclePhase
    milestone_key: str
    attempt: int
    request_id: str
    prompt: str
    kind: DecisionRequestKind = DecisionRequestKind.APPROVAL

    @property
    def event_type(self) -> WorkUnitEventType:
        return WorkUnitEventType.APPROVAL_REQUESTED

    @property
    def transition_name(self) -> str:
        return f"{self.event_type.value}:{self.request_id}"

    def event_payload(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "milestone_key": self.milestone_key,
            "attempt": self.attempt,
            "request_id": self.request_id,
            "prompt": self.prompt,
            "kind": self.kind.value,
        }


@dataclass(frozen=True)
class ApprovalReceived:
    """A named request was resolved by an operator."""

    phase: LifecyclePhase
    milestone_key: str
    attempt: int
    request_id: str
    decision: OperatorDecision
    decided_by: str
    response_idempotency_key: str
    decision_payload: dict[str, Any] = field(default_factory=dict)

    @property
    def event_type(self) -> WorkUnitEventType:
        return WorkUnitEventType.APPROVAL_RECEIVED

    @property
    def transition_name(self) -> str:
        return f"{self.event_type.value}:{self.request_id}:{self.response_idempotency_key}"

    def event_payload(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "milestone_key": self.milestone_key,
            "attempt": self.attempt,
            "request_id": self.request_id,
            "decision": self.decision.value,
            "decided_by": self.decided_by,
            "decision_payload": dict(sorted(self.decision_payload.items())),
        }


@dataclass(frozen=True)
class ArtifactRecorded:
    """Evidence attached to a milestone outside its status transition."""

    phase: LifecyclePhase
    milestone_key: str
    attempt: int
    artifact: ArtifactRecord

    @property
    def event_type(self) -> WorkUnitEventType:
        return WorkUnitEventType.ARTIFACT_RECORDED

    @property
    def transition_name(self) -> str:
        return (
            f"{self.event_type.value}:{self.artifact.artifact_type.value}"
            f":{self.artifact.content_hash}"
        )

    def event_payload(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "milestone_key": self.milestone_key,
            "attempt": self.attempt,
            "artifact": self.artifact.to_payload(),
        }


@dataclass(frozen=True)
class DispatchIntentCreated:
    """A milestone transition produced agent work in the dispatch ledger.

    A DispatchIntent is never a WorkUnit of its own. It exists because a legal
    milestone transition asked for agent work, and this fact is the durable link
    between the two.
    """

    phase: LifecyclePhase
    milestone_key: str
    attempt: int
    dispatch_intent_id: str
    tier: str
    kind: str
    # The commit this intent's worktree branches from, resolved from the
    # milestone's dependency's settled result. None means HEAD, which is both
    # the no-dependency case and every event recorded before the field existed.
    # Recorded so the operator at the CODE_MERGE gate can read the whole
    # lineage from the ledger instead of reconstructing it from git.
    base_commit_sha: str | None = None

    @property
    def event_type(self) -> WorkUnitEventType:
        return WorkUnitEventType.DISPATCH_INTENT_CREATED

    @property
    def transition_name(self) -> str:
        return f"{self.event_type.value}:{self.dispatch_intent_id}"

    def event_payload(self) -> dict[str, Any]:
        return {
            "phase": self.phase.value,
            "milestone_key": self.milestone_key,
            "attempt": self.attempt,
            "dispatch_intent_id": self.dispatch_intent_id,
            "tier": self.tier,
            "kind": self.kind,
            "base_commit_sha": self.base_commit_sha,
        }


@dataclass(frozen=True)
class AutomaticCrashRecovery:
    """An unattended reconciler repaired a dead execution and resumed it.

    Its own event type, and the whole reason for one: the automatic-recovery
    budget has to be countable, and there was no way to count it. `execution_epoch`
    counts `WORK_UNIT_BLOCKED` and `WORK_UNIT_WAITING_FOR_OPERATOR`, which is
    every halt however caused - a milestone that finished a phase and parked, an
    approval gate waiting on a person, an operator resume. Reading that as a
    crash-retry counter would let an ordinary approval wait consume a WorkUnit's
    budget for surviving crashes, and would let a WorkUnit that crashes between
    approvals never consume it at all.

    So the budget counts these, and nothing else writes one.
    """

    execution_workflow_id: str
    halted_epoch: int
    abandoned_milestones: tuple[str, ...] = ()
    reconciler: str = "crash-reconciler"

    @property
    def event_type(self) -> WorkUnitEventType:
        return WorkUnitEventType.AUTOMATIC_CRASH_RECOVERY

    @property
    def transition_name(self) -> str:
        # The epoch, so a second recovery of the same dead execution is absorbed
        # as a duplicate while a recovery of a later one is a new fact. A
        # reconciler running twice - two processes, or one restarted mid-pass -
        # must not spend two budget entries on one crash.
        return f"{self.event_type.value}:{self.halted_epoch}"

    def event_payload(self) -> dict[str, Any]:
        return {
            "execution_workflow_id": self.execution_workflow_id,
            "halted_epoch": self.halted_epoch,
            "abandoned_milestones": list(self.abandoned_milestones),
            "reconciler": self.reconciler,
        }


LifecycleFact = (
    WorkUnitTransition
    | PhaseTransition
    | MilestoneTransition
    | ApprovalRequested
    | ApprovalReceived
    | ArtifactRecorded
    | DispatchIntentCreated
    | AutomaticCrashRecovery
)


def idempotency_key(root_workflow_id: str, fact: LifecycleFact) -> str:
    """The deterministic identity of one transition.

    A replayed step, a re-delivered dispatch outcome, and a duplicated operator
    submission all compute the same key, so the unique index absorbs them instead
    of the history growing a second copy of the same fact.
    """

    phase = getattr(fact, "phase", None)
    phase_part = phase.value if phase is not None else "-"
    milestone_part = getattr(fact, "milestone_key", None) or "-"
    attempt_part = getattr(fact, "attempt", 0)
    return f"{root_workflow_id}:{phase_part}:{milestone_part}:{attempt_part}:{fact.transition_name}"


__all__ = [
    "ApprovalReceived",
    "ApprovalRequested",
    "ArtifactKind",
    "ArtifactRecord",
    "ArtifactRecorded",
    "ArtifactType",
    "AutomaticCrashRecovery",
    "Answered",
    "Approved",
    "ApprovalOutcome",
    "ClarificationOutcome",
    "DecisionKindMismatch",
    "Denied",
    "DiagnosticArtifact",
    "DiagnosticArtifactKind",
    "OperatorDecisionOutcome",
    "decision_outcome",
    "DecisionRequestKind",
    "DecisionRequestStatus",
    "DispatchIntentCreated",
    "LifecycleFact",
    "MilestoneTransition",
    "OperatorDecision",
    "PhaseTransition",
    "RequirableArtifact",
    "TraceArtifact",
    "TraceArtifactKind",
    "UnrecognizedArtifact",
    "WorkUnitEventType",
    "WorkUnitTransition",
    "idempotency_key",
    "parse_artifact_type",
]
