# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The fixed engineering lifecycle and its state machines.

Every DesignDoc executes the same ordered phases. A document cannot add, remove,
or reorder a phase; it can only decide which phases hold milestones. A phase with
no milestones still occupies the topology and transitions through ``SKIPPED``, so
the shape of an execution is a property of this module rather than of any
document.

The transition tables here are the single source of truth for legal state change.
Nothing else in the package may write a status without going through
``assert_work_unit_transition``, ``assert_phase_transition``, or
``assert_milestone_transition``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

LIFECYCLE_PROFILE: Final = "engineering.v1"
LIFECYCLE_PROFILE_VERSION: Final = 1


class LifecyclePhase(StrEnum):
    """The fixed, ordered lifecycle phases.

    Declaration order is the execution order. ``ORDERED_PHASES`` derives from it
    so a reordered member cannot leave a second list saying otherwise.
    """

    CLARIFY = "CLARIFY"
    VALIDATE = "VALIDATE"
    PLAN = "PLAN"
    IMPLEMENT = "IMPLEMENT"
    VERIFY = "VERIFY"
    REVIEW = "REVIEW"
    DELIVER = "DELIVER"


ORDERED_PHASES: Final[tuple[LifecyclePhase, ...]] = tuple(LifecyclePhase)

_PHASE_ORDINALS: Final[dict[LifecyclePhase, int]] = {
    phase: index for index, phase in enumerate(ORDERED_PHASES)
}


def phase_ordinal(phase: LifecyclePhase) -> int:
    """Position of a phase in the fixed order, for dependency direction checks."""

    return _PHASE_ORDINALS[phase]


class WorkUnitStatus(StrEnum):
    DRAFT = "DRAFT"
    COMPILED = "COMPILED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_FOR_OPERATOR = "WAITING_FOR_OPERATOR"
    BLOCKED = "BLOCKED"
    CANCELLING = "CANCELLING"
    """Cancellation was asked for and the cascade has not finished stopping things.

    Non-terminal on purpose. `CANCELLED` used to be written the instant an
    operator asked, which made "we have stopped" indistinguishable from "we have
    been asked to stop" while dispatch intents and workflows were still live.
    """

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


class WorkUnitPhaseMarker(StrEnum):
    """``work_units.current_phase`` values that are not lifecycle phases."""

    COMPLETE = "COMPLETE"


class PhaseStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class MilestoneExecutionStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_FOR_OPERATOR = "WAITING_FOR_OPERATOR"
    BLOCKED = "BLOCKED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class FailureClass(StrEnum):
    """How a failure must be handled, not what raised it."""

    TRANSIENT = "TRANSIENT"
    CORRECTABLE = "CORRECTABLE"
    REQUIRES_REPLAN = "REQUIRES_REPLAN"
    REQUIRES_OPERATOR = "REQUIRES_OPERATOR"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    NONRECOVERABLE = "NONRECOVERABLE"


TERMINAL_WORK_UNIT_STATUSES: Final[frozenset[WorkUnitStatus]] = frozenset(
    {
        WorkUnitStatus.SUCCEEDED,
        WorkUnitStatus.FAILED,
        WorkUnitStatus.CANCELLED,
        WorkUnitStatus.SUPERSEDED,
    }
)

TERMINAL_PHASE_STATUSES: Final[frozenset[PhaseStatus]] = frozenset(
    {
        PhaseStatus.SUCCEEDED,
        PhaseStatus.SKIPPED,
        PhaseStatus.FAILED,
        PhaseStatus.CANCELLED,
    }
)

TERMINAL_MILESTONE_STATUSES: Final[frozenset[MilestoneExecutionStatus]] = frozenset(
    {
        MilestoneExecutionStatus.SUCCEEDED,
        MilestoneExecutionStatus.FAILED,
        MilestoneExecutionStatus.SKIPPED,
        MilestoneExecutionStatus.CANCELLED,
    }
)


_WORK_UNIT_TRANSITIONS: Final[dict[WorkUnitStatus, frozenset[WorkUnitStatus]]] = {
    WorkUnitStatus.DRAFT: frozenset(
        {WorkUnitStatus.CANCELLING, WorkUnitStatus.COMPILED, WorkUnitStatus.CANCELLED}
    ),
    WorkUnitStatus.COMPILED: frozenset(
        {
            WorkUnitStatus.CANCELLING,
            WorkUnitStatus.QUEUED,
            WorkUnitStatus.CANCELLED,
            WorkUnitStatus.SUPERSEDED,
        }
    ),
    WorkUnitStatus.QUEUED: frozenset(
        {
            WorkUnitStatus.CANCELLING,
            WorkUnitStatus.RUNNING,
            WorkUnitStatus.CANCELLED,
            WorkUnitStatus.SUPERSEDED,
        }
    ),
    WorkUnitStatus.RUNNING: frozenset(
        {
            WorkUnitStatus.CANCELLING,
            WorkUnitStatus.WAITING_FOR_OPERATOR,
            WorkUnitStatus.BLOCKED,
            WorkUnitStatus.SUCCEEDED,
            WorkUnitStatus.FAILED,
            WorkUnitStatus.CANCELLED,
        }
    ),
    WorkUnitStatus.WAITING_FOR_OPERATOR: frozenset(
        {
            WorkUnitStatus.CANCELLING,
            WorkUnitStatus.RUNNING,
            WorkUnitStatus.BLOCKED,
            WorkUnitStatus.FAILED,
            WorkUnitStatus.CANCELLED,
        }
    ),
    WorkUnitStatus.BLOCKED: frozenset(
        {
            WorkUnitStatus.CANCELLING,
            WorkUnitStatus.RUNNING,
            WorkUnitStatus.FAILED,
            WorkUnitStatus.CANCELLED,
            WorkUnitStatus.SUPERSEDED,
        }
    ),
    WorkUnitStatus.CANCELLING: frozenset({WorkUnitStatus.CANCELLED}),
    WorkUnitStatus.SUCCEEDED: frozenset(),
    WorkUnitStatus.FAILED: frozenset({WorkUnitStatus.SUPERSEDED}),
    WorkUnitStatus.CANCELLED: frozenset({WorkUnitStatus.SUPERSEDED}),
    WorkUnitStatus.SUPERSEDED: frozenset(),
}


_PHASE_TRANSITIONS: Final[dict[PhaseStatus, frozenset[PhaseStatus]]] = {
    PhaseStatus.PENDING: frozenset(
        {
            PhaseStatus.RUNNING,
            PhaseStatus.SKIPPED,
            PhaseStatus.CANCELLED,
        }
    ),
    PhaseStatus.RUNNING: frozenset(
        {
            PhaseStatus.SUCCEEDED,
            PhaseStatus.SKIPPED,
            PhaseStatus.BLOCKED,
            PhaseStatus.FAILED,
            PhaseStatus.CANCELLED,
        }
    ),
    PhaseStatus.BLOCKED: frozenset(
        {
            PhaseStatus.RUNNING,
            PhaseStatus.FAILED,
            PhaseStatus.CANCELLED,
        }
    ),
    PhaseStatus.SUCCEEDED: frozenset(),
    PhaseStatus.SKIPPED: frozenset(),
    PhaseStatus.FAILED: frozenset(),
    PhaseStatus.CANCELLED: frozenset(),
}


_MILESTONE_TRANSITIONS: Final[
    dict[MilestoneExecutionStatus, frozenset[MilestoneExecutionStatus]]
] = {
    MilestoneExecutionStatus.PENDING: frozenset(
        {
            MilestoneExecutionStatus.READY,
            MilestoneExecutionStatus.SKIPPED,
            MilestoneExecutionStatus.CANCELLED,
        }
    ),
    MilestoneExecutionStatus.READY: frozenset(
        {
            MilestoneExecutionStatus.RUNNING,
            MilestoneExecutionStatus.WAITING_FOR_OPERATOR,
            MilestoneExecutionStatus.BLOCKED,
            # A ready milestone can fail at its gate without ever running: an
            # operator denial is a failure of the milestone, and pretending it ran
            # first would put a RUNNING event in the history for work that never
            # started.
            MilestoneExecutionStatus.FAILED,
            MilestoneExecutionStatus.CANCELLED,
        }
    ),
    MilestoneExecutionStatus.RUNNING: frozenset(
        {
            MilestoneExecutionStatus.WAITING_FOR_OPERATOR,
            MilestoneExecutionStatus.BLOCKED,
            MilestoneExecutionStatus.SUCCEEDED,
            MilestoneExecutionStatus.FAILED,
            MilestoneExecutionStatus.CANCELLED,
        }
    ),
    MilestoneExecutionStatus.WAITING_FOR_OPERATOR: frozenset(
        {
            MilestoneExecutionStatus.RUNNING,
            MilestoneExecutionStatus.BLOCKED,
            MilestoneExecutionStatus.FAILED,
            MilestoneExecutionStatus.CANCELLED,
        }
    ),
    MilestoneExecutionStatus.BLOCKED: frozenset(
        {
            MilestoneExecutionStatus.READY,
            MilestoneExecutionStatus.FAILED,
            MilestoneExecutionStatus.CANCELLED,
        }
    ),
    MilestoneExecutionStatus.SUCCEEDED: frozenset(),
    MilestoneExecutionStatus.FAILED: frozenset(),
    MilestoneExecutionStatus.SKIPPED: frozenset(),
    MilestoneExecutionStatus.CANCELLED: frozenset(),
}


class IllegalTransition(RuntimeError):
    """A state change the lifecycle does not permit.

    This is a programmer error, never a runtime condition: the caller asked the
    domain to enter a state the state machine has no edge to. It crashes rather
    than logging, because a silently absorbed illegal transition is exactly how
    two lifecycle models came to disagree in the saga implementation.
    """

    def __init__(self, aggregate: str, current: str, requested: str) -> None:
        super().__init__(f"illegal {aggregate} transition: {current} -> {requested}")
        self.aggregate = aggregate
        self.current = current
        self.requested = requested


def assert_work_unit_transition(current: WorkUnitStatus, requested: WorkUnitStatus) -> None:
    if requested is current:
        return
    if requested not in _WORK_UNIT_TRANSITIONS[current]:
        raise IllegalTransition("work_unit", current.value, requested.value)


def assert_phase_transition(current: PhaseStatus, requested: PhaseStatus) -> None:
    if requested is current:
        return
    if requested not in _PHASE_TRANSITIONS[current]:
        raise IllegalTransition("phase", current.value, requested.value)


def assert_milestone_transition(
    current: MilestoneExecutionStatus,
    requested: MilestoneExecutionStatus,
) -> None:
    if requested is current:
        return
    if requested not in _MILESTONE_TRANSITIONS[current]:
        raise IllegalTransition("milestone_execution", current.value, requested.value)


def work_unit_transition_allowed(current: WorkUnitStatus, requested: WorkUnitStatus) -> bool:
    return requested is current or requested in _WORK_UNIT_TRANSITIONS[current]


def milestone_transition_allowed(
    current: MilestoneExecutionStatus,
    requested: MilestoneExecutionStatus,
) -> bool:
    return requested is current or requested in _MILESTONE_TRANSITIONS[current]


__all__ = [
    "FailureClass",
    "IllegalTransition",
    "LIFECYCLE_PROFILE",
    "LIFECYCLE_PROFILE_VERSION",
    "LifecyclePhase",
    "MilestoneExecutionStatus",
    "ORDERED_PHASES",
    "PhaseStatus",
    "TERMINAL_MILESTONE_STATUSES",
    "TERMINAL_PHASE_STATUSES",
    "TERMINAL_WORK_UNIT_STATUSES",
    "WorkUnitPhaseMarker",
    "WorkUnitStatus",
    "assert_milestone_transition",
    "assert_phase_transition",
    "assert_work_unit_transition",
    "milestone_transition_allowed",
    "phase_ordinal",
    "work_unit_transition_allowed",
]
