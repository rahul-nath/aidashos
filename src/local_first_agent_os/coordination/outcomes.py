# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Structured terminal outcomes kept separate from lifecycle status."""

from __future__ import annotations

from enum import StrEnum

from ..contracts import ApprovalStatus


class FailureCategory(StrEnum):
    BUSINESS = "BUSINESS"
    INFRASTRUCTURE = "INFRASTRUCTURE"


class BusinessFailure(StrEnum):
    """The requested work ran, but its domain contract was not satisfied."""

    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    DEPENDENCY_FAILED = "DEPENDENCY_FAILED"
    DELEGATE_REQUEST_REJECTED = "DELEGATE_REQUEST_REJECTED"


class InfrastructureFailure(StrEnum):
    """The execution machinery or one of its providers failed."""

    USAGE_LIMIT = "USAGE_LIMIT"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    TRANSPORT_INTERRUPTED = "TRANSPORT_INTERRUPTED"
    PROVIDER_OVERLOADED = "PROVIDER_OVERLOADED"
    ARGUMENT_LIST_TOO_LONG = "ARGUMENT_LIST_TOO_LONG"
    INTERNAL_ASSERTION = "INTERNAL_ASSERTION"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    ORPHANED_LEASE_EXPIRED = "ORPHANED_LEASE_EXPIRED"
    SUPERVISOR_FAILED = "SUPERVISOR_FAILED"
    PROCESS_FAILED = "PROCESS_FAILED"
    ARTIFACT_WRITE_FAILED = "ARTIFACT_WRITE_FAILED"
    EVENT_WRITE_FAILED = "EVENT_WRITE_FAILED"
    CHECKPOINT_WRITE_FAILED = "CHECKPOINT_WRITE_FAILED"
    DATA_INTEGRITY_VIOLATION = "DATA_INTEGRITY_VIOLATION"
    UNKNOWN_FAILURE = "UNKNOWN_FAILURE"


class AgentStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    UNKNOWN = "UNKNOWN"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


class SupervisorStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class PersistenceStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class ExecutionActivityStatus(StrEnum):
    """Progress health, independent from lease and dispatch ownership."""

    STARTING = "STARTING"
    PROGRESSING = "PROGRESSING"
    QUIET = "QUIET"
    STALLED_SUSPECTED = "STALLED_SUSPECTED"
    TERMINAL = "TERMINAL"


class ProgressRecommendation(StrEnum):
    CONTINUE = "CONTINUE"
    CHECKPOINT = "CHECKPOINT"
    SPLIT = "SPLIT"
    PAUSE_OPERATOR = "PAUSE_OPERATOR"


class TerminalOutcome(StrEnum):
    AUTOMATED_COMPLETION = "AUTOMATED_COMPLETION"
    MANUAL_RECOVERY_COMPLETION = "MANUAL_RECOVERY_COMPLETION"
    OPERATOR_CANCELED = "OPERATOR_CANCELED"
    USAGE_LIMIT = "USAGE_LIMIT"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    TRANSPORT_INTERRUPTED = "TRANSPORT_INTERRUPTED"
    PROVIDER_OVERLOADED = "PROVIDER_OVERLOADED"
    DELEGATE_REQUEST_REJECTED = "DELEGATE_REQUEST_REJECTED"
    ARGUMENT_LIST_TOO_LONG = "ARGUMENT_LIST_TOO_LONG"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    INTERNAL_ASSERTION = "INTERNAL_ASSERTION"
    DEPENDENCY_FAILED = "DEPENDENCY_FAILED"
    DEADLINE_EXCEEDED = "DEADLINE_EXCEEDED"
    ORPHANED_LEASE_EXPIRED = "ORPHANED_LEASE_EXPIRED"
    # A claim whose holder never came back. The intent settles as CANCELED so
    # quorum and retention keep their existing terminal statuses; the outcome
    # is what says the cancellation was abandonment rather than an operator.
    ORPHANED_CLAIM_EXPIRED = "ORPHANED_CLAIM_EXPIRED"
    COMPENSATED = "COMPENSATED"
    SUPERVISOR_FAILED = "SUPERVISOR_FAILED"
    DUPLICATE_SUPPRESSED = "DUPLICATE_SUPPRESSED"
    PROCESS_FAILED = "PROCESS_FAILED"
    UNKNOWN_FAILURE = "UNKNOWN_FAILURE"


class ExecutionTransition(StrEnum):
    """Non-terminal orchestration actions derived from a terminal attempt."""

    SWITCH_TO_FALLBACK = "SWITCH_TO_FALLBACK"


class DispatchResultOrigin(StrEnum):
    """How durable dispatch evidence was produced."""

    AUTOMATED = "AUTOMATED"
    AUTOMATED_RECOVERY = "AUTOMATED_RECOVERY"
    MANUAL_RECOVERY = "MANUAL_RECOVERY"
    UNKNOWN = "UNKNOWN"


class DispatchResultState(StrEnum):
    """Finite states accepted by a dispatch_runner_result.v1 envelope."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"
    REVIEWED = "REVIEWED"
    UNAVAILABLE = "UNAVAILABLE"


class DispatchPromotionState(StrEnum):
    """Promotion states after result evidence exists.

    Approval and merge are deliberately separate transitions.  An approved
    CODE_MERGE request is not evidence that the branch was integrated.
    """

    RESULT_RECORDED = "RESULT_RECORDED"
    REVIEWED = "REVIEWED"
    MERGE_PENDING = "MERGE_PENDING"
    MERGE_APPROVED = "MERGE_APPROVED"
    MERGED = "MERGED"
    MILESTONE_COMPLETED = "MILESTONE_COMPLETED"


_APPROVAL_STATUS_TRANSITIONS: dict[ApprovalStatus, frozenset[ApprovalStatus]] = {
    ApprovalStatus.PENDING: frozenset(
        {
            ApprovalStatus.APPROVED,
            ApprovalStatus.DENIED,
        }
    ),
    ApprovalStatus.APPROVED: frozenset({ApprovalStatus.REVOKED}),
    ApprovalStatus.DENIED: frozenset(),
    ApprovalStatus.REVOKED: frozenset(),
}


def require_approval_status_transition(
    current: ApprovalStatus,
    target: ApprovalStatus,
) -> None:
    """Fail closed when an approval tries to skip or reverse its lifecycle."""

    if target not in _APPROVAL_STATUS_TRANSITIONS[current]:
        raise ValueError(f"invalid approval status transition: {current} -> {target}")


def next_approval_statuses(current: ApprovalStatus) -> frozenset[ApprovalStatus]:
    return _APPROVAL_STATUS_TRANSITIONS[current]


_DISPATCH_PROMOTION_TRANSITIONS: dict[DispatchPromotionState, frozenset[DispatchPromotionState]] = {
    DispatchPromotionState.RESULT_RECORDED: frozenset({DispatchPromotionState.REVIEWED}),
    DispatchPromotionState.REVIEWED: frozenset({DispatchPromotionState.MERGE_PENDING}),
    DispatchPromotionState.MERGE_PENDING: frozenset({DispatchPromotionState.MERGE_APPROVED}),
    DispatchPromotionState.MERGE_APPROVED: frozenset({DispatchPromotionState.MERGED}),
    DispatchPromotionState.MERGED: frozenset({DispatchPromotionState.MILESTONE_COMPLETED}),
    DispatchPromotionState.MILESTONE_COMPLETED: frozenset(),
}


def require_dispatch_promotion_transition(
    current: DispatchPromotionState,
    target: DispatchPromotionState,
) -> None:
    """Fail closed when code attempts to skip a promotion boundary."""

    if target not in _DISPATCH_PROMOTION_TRANSITIONS[current]:
        raise ValueError(f"invalid dispatch promotion transition: {current} -> {target}")


def next_dispatch_promotion_states(
    current: DispatchPromotionState,
) -> frozenset[DispatchPromotionState]:
    return _DISPATCH_PROMOTION_TRANSITIONS[current]


def classify_failure(text: str | None) -> TerminalOutcome:
    normalized = (text or "").casefold()
    if "argument list too long" in normalized:
        return TerminalOutcome.ARGUMENT_LIST_TOO_LONG
    if any(
        marker in normalized
        for marker in (
            "usage_limit",
            "usage limit",
            "rate_limit",
            "rate limit",
            "session limit",
            "quota exceeded",
            "too many requests",
            "http 429",
            '"api_error_status":429',
        )
    ):
        return TerminalOutcome.USAGE_LIMIT
    if any(
        marker in normalized
        for marker in (
            "authentication invalid",
            "authentication expired",
            "not authenticated",
            "unauthorized",
            "http 401",
        )
    ):
        return TerminalOutcome.AUTHENTICATION_FAILED
    if "400 bad request" in normalized and "delegate" in normalized:
        return TerminalOutcome.DELEGATE_REQUEST_REJECTED
    if "verification failed" in normalized or "verification_failed" in normalized:
        return TerminalOutcome.VERIFICATION_FAILED
    if "assertionerror" in normalized or "assertion error" in normalized:
        return TerminalOutcome.INTERNAL_ASSERTION
    # Below the branches that name a judgment and above the one that names a
    # consequence. A run that verified and failed, or tripped an assertion, said
    # something about the work, and a dropped connection in the same log does not
    # take that back. `dependencies did not complete` says the opposite: the
    # upstream turn died, which is what a dropped stream *causes*. The observed
    # failure carried both sentences, the consequence matched first, and an
    # infrastructure event was recorded as a `BusinessFailure` - spending one of
    # the milestone's three attempts on work it never got to do.
    # A provider that was too busy to answer said nothing about the work, and it
    # sits here for the same reason the transport branch does: the run it killed
    # leaves `dependencies did not complete` behind it, and that consequence used
    # to match first. On 2026-08-12 an `API Error: 529 Overloaded` was recorded as
    # `DEPENDENCY_FAILED` - a `BusinessFailure`, meaning the work ran and failed
    # its contract - and charged milestone 1 of the worktree-loss WorkUnit one of
    # its three attempts for a server-side overload it had no part in.
    #
    # Distinct from `USAGE_LIMIT`, which is also the provider refusing. A quota is
    # spent for hours and a retry meets the same wall; an overload clears in
    # moments, so this is the rare provider failure where trying again is not just
    # blameless but likely to work.
    if any(
        marker in normalized
        for marker in (
            "529 overloaded",
            "http 529",
            "api error: 529",
            "overloaded_error",
            '"type":"overloaded_error"',
        )
    ):
        return TerminalOutcome.PROVIDER_OVERLOADED
    if any(
        marker in normalized
        for marker in (
            "connection closed mid-response",
            "connection closed midresponse",
            "connection reset",
            "connection aborted",
            "incomplete chunked read",
            "peer closed connection",
            "server disconnected",
            "remote end closed connection",
        )
    ):
        return TerminalOutcome.TRANSPORT_INTERRUPTED
    if "dependencies did not complete" in normalized or "dependency" in normalized:
        return TerminalOutcome.DEPENDENCY_FAILED
    if "supervisor" in normalized:
        return TerminalOutcome.SUPERVISOR_FAILED
    if "timed out" in normalized or "deadline" in normalized:
        return TerminalOutcome.DEADLINE_EXCEEDED
    if normalized:
        return TerminalOutcome.PROCESS_FAILED
    return TerminalOutcome.UNKNOWN_FAILURE


def failure_category(outcome: TerminalOutcome | str | None) -> FailureCategory | None:
    if outcome is None:
        return None
    value = outcome.value if isinstance(outcome, TerminalOutcome) else str(outcome)
    if value in BusinessFailure._value2member_map_:
        return FailureCategory.BUSINESS
    if value in InfrastructureFailure._value2member_map_:
        return FailureCategory.INFRASTRUCTURE
    return None


def classify_persistence_failure(error: BaseException | str) -> InfrastructureFailure:
    text = str(error).casefold()
    if any(
        marker in text
        for marker in (
            "foreign key",
            "foreignkeyviolation",
            "unique constraint",
            "not null constraint",
            "check constraint",
            "integrityerror",
        )
    ):
        return InfrastructureFailure.DATA_INTEGRITY_VIOLATION
    return InfrastructureFailure.ARTIFACT_WRITE_FAILED


__all__ = [
    "AgentStatus",
    "BusinessFailure",
    "DispatchPromotionState",
    "DispatchResultOrigin",
    "DispatchResultState",
    "ExecutionTransition",
    "ExecutionActivityStatus",
    "FailureCategory",
    "InfrastructureFailure",
    "PersistenceStatus",
    "ProgressRecommendation",
    "SupervisorStatus",
    "TerminalOutcome",
    "classify_failure",
    "classify_persistence_failure",
    "failure_category",
    "next_approval_statuses",
    "next_dispatch_promotion_states",
    "require_approval_status_transition",
    "require_dispatch_promotion_transition",
]
