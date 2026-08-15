# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pure scheduling decisions: readiness, phase-local blocking, and phase exit.

Nothing here touches the database, the clock, or a model. Given a compiled plan
and a snapshot of milestone statuses it says what may run next and what a phase's
outcome is. That makes every scheduling rule testable without a workflow runtime,
and it is why the root workflow body can stay deterministic.

The rules deliberately do not know why a milestone is in the state it is in. A
dependency is satisfied only by ``SUCCEEDED``, because that is the only status
that comes with the evidence the plan required.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from .lifecycle import (
    TERMINAL_MILESTONE_STATUSES,
    LifecyclePhase,
    MilestoneExecutionStatus,
    PhaseStatus,
)
from .plan import DELIVERY_PACE_WIDTH_REQUEST, CompiledWorkPlan

MilestoneStatuses = Mapping[str, MilestoneExecutionStatus]


@dataclass(frozen=True)
class PhaseWorkSet:
    """What the scheduler decided about one phase at one moment.

    A sum of four lists rather than a single "next" value: a phase can
    simultaneously have runnable work, work waiting on dependencies, and work that
    can never run because a dependency failed. Collapsing those into one answer is
    how a scheduler ends up silently dropping the third category.
    """

    ready: tuple[str, ...]
    waiting: tuple[str, ...]
    unreachable: tuple[str, ...]
    terminal: tuple[str, ...]

    @property
    def has_pending_work(self) -> bool:
        return bool(self.ready or self.waiting)


def _dependency_satisfied(status: MilestoneExecutionStatus) -> bool:
    return status is MilestoneExecutionStatus.SUCCEEDED


def _dependency_unreachable(status: MilestoneExecutionStatus) -> bool:
    return status in {
        MilestoneExecutionStatus.FAILED,
        MilestoneExecutionStatus.SKIPPED,
        MilestoneExecutionStatus.CANCELLED,
    }


def compute_phase_work_set(
    plan: CompiledWorkPlan,
    phase: LifecyclePhase,
    statuses: MilestoneStatuses,
) -> PhaseWorkSet:
    """Classify every milestone in one phase.

    Only milestones assigned to ``phase`` are considered, so a later-phase
    milestone whose dependencies happen to be satisfied still cannot be started
    early. The fixed lifecycle, not the dependency graph, decides when a phase's
    work becomes eligible.
    """

    ready: list[str] = []
    waiting: list[str] = []
    unreachable: list[str] = []
    terminal: list[str] = []

    for milestone in plan.milestones_in_phase(phase):
        status = statuses.get(milestone.stable_key, MilestoneExecutionStatus.PENDING)
        if status in TERMINAL_MILESTONE_STATUSES:
            terminal.append(milestone.stable_key)
            continue
        if status in {
            MilestoneExecutionStatus.RUNNING,
            MilestoneExecutionStatus.WAITING_FOR_OPERATOR,
            MilestoneExecutionStatus.BLOCKED,
        }:
            # A blocked milestone is not schedulable. It stopped without finishing
            # and only an explicit recovery returns it to READY; treating it as
            # ready again would spin the scheduler on work that cannot progress.
            waiting.append(milestone.stable_key)
            continue
        dependency_statuses = [
            statuses.get(dependency, MilestoneExecutionStatus.PENDING)
            for dependency in milestone.dependencies
        ]
        if any(_dependency_unreachable(item) for item in dependency_statuses):
            unreachable.append(milestone.stable_key)
            continue
        if all(_dependency_satisfied(item) for item in dependency_statuses):
            ready.append(milestone.stable_key)
        else:
            waiting.append(milestone.stable_key)

    return PhaseWorkSet(
        ready=tuple(ready),
        waiting=tuple(waiting),
        unreachable=tuple(unreachable),
        terminal=tuple(terminal),
    )


def evaluate_phase_exit(
    plan: CompiledWorkPlan,
    phase: LifecyclePhase,
    statuses: MilestoneStatuses,
) -> PhaseStatus:
    """The status a phase has now, given its milestones.

    Called after the phase has stopped making progress. The order of the checks is
    the policy: a failure that blocks its phase beats a block, a block beats a
    cancellation, and only a phase whose every milestone succeeded may complete.
    """

    milestones = plan.milestones_in_phase(phase)
    if not milestones:
        return PhaseStatus.SKIPPED

    resolved = {
        milestone.stable_key: statuses.get(milestone.stable_key, MilestoneExecutionStatus.PENDING)
        for milestone in milestones
    }

    for milestone in milestones:
        status = resolved[milestone.stable_key]
        if status is MilestoneExecutionStatus.FAILED and milestone.failure_policy.blocks_phase:
            return PhaseStatus.FAILED

    if any(
        status
        in {
            MilestoneExecutionStatus.BLOCKED,
            MilestoneExecutionStatus.WAITING_FOR_OPERATOR,
        }
        for status in resolved.values()
    ):
        return PhaseStatus.BLOCKED

    if any(status is MilestoneExecutionStatus.CANCELLED for status in resolved.values()):
        return PhaseStatus.CANCELLED

    if all(status is MilestoneExecutionStatus.SUCCEEDED for status in resolved.values()):
        return PhaseStatus.SUCCEEDED

    if all(
        status in {MilestoneExecutionStatus.SUCCEEDED, MilestoneExecutionStatus.SKIPPED}
        for status in resolved.values()
    ):
        return PhaseStatus.SUCCEEDED

    # Something in the phase never reached a terminal state and nothing above
    # explains why. Blocking is the honest answer: the phase is not finished and
    # cannot proceed on its own.
    return PhaseStatus.BLOCKED


def bounded_batch(ready: tuple[str, ...], limit: int) -> tuple[str, ...]:
    """The slice of ready milestones a phase may start concurrently.

    Parallelism changes when work happens, never what it means, so the batch is
    just a prefix of the deterministic ready order.
    """

    if limit < 1:
        raise ValueError(f"parallelism limit must be at least 1, got {limit}")
    return ready[:limit]


class WidthConstraint(StrEnum):
    """Which of the three bounds actually decided the width.

    Reported rather than recomputed, because the three are indistinguishable
    from the number alone: a width of 2 could be a graph with two ready leaves,
    a document asking to go steadily, or a ceiling clipping a request for eight.
    An operator asking why a compressed plan is not running wide needs the
    difference, and so does anyone auditing whether the ceiling ever bound a
    document that asked for more.
    """

    READY_SET = "ready_set"
    DECLARED_PACE = "declared_pace"
    AUTHORITY_CEILING = "authority_ceiling"


@dataclass(frozen=True)
class ScheduleWidth:
    """How wide one phase may go right now, and what held it there."""

    effective: int
    ready_count: int
    ceiling: int
    requested: int | None

    @property
    def binding_constraint(self) -> WidthConstraint:
        """Derived, not stored, so it cannot disagree with the number beside it.

        Checked narrowest-first. The graph wins ties: when only two milestones
        are ready, a ceiling of two did not bind anything, and reporting that it
        did would send someone to raise a limit that was never the reason.
        """

        if self.ready_count <= self.effective:
            return WidthConstraint.READY_SET
        if self.requested is not None and self.requested <= self.ceiling:
            return WidthConstraint.DECLARED_PACE
        return WidthConstraint.AUTHORITY_CEILING

    def to_payload(self) -> dict[str, object]:
        return {
            "effective": self.effective,
            "ready_count": self.ready_count,
            "ceiling": self.ceiling,
            "requested": self.requested,
            "binding_constraint": self.binding_constraint.value,
        }


def resolve_schedule_width(plan: CompiledWorkPlan, ready: tuple[str, ...]) -> ScheduleWidth:
    """Reconcile what the document asked for against what it is allowed.

    Three bounds, and the smallest wins: the dependency graph cannot use more
    width than it has ready leaves, the document's pace states what it wants,
    and ``AuthorityPolicy.max_parallel_milestones`` is the bound the document
    could not widen even by asking. The ceiling is applied to the request rather
    than to the result so that a document asking for more than it may have is
    clipped, never honoured.

    ``effective`` is zero only when nothing is ready, which is the caller's
    signal to stop rather than an argument for ``bounded_batch``.
    """

    ceiling = plan.authority_policy.max_parallel_milestones
    if ceiling < 1:
        raise ValueError(f"authority ceiling must be at least 1, got {ceiling}")
    requested = DELIVERY_PACE_WIDTH_REQUEST[plan.declared_delivery_pace]
    allowed = ceiling if requested is None else min(requested, ceiling)
    return ScheduleWidth(
        effective=min(len(ready), allowed),
        ready_count=len(ready),
        ceiling=ceiling,
        requested=requested,
    )


__all__ = [
    "MilestoneStatuses",
    "PhaseWorkSet",
    "ScheduleWidth",
    "WidthConstraint",
    "bounded_batch",
    "compute_phase_work_set",
    "evaluate_phase_exit",
    "resolve_schedule_width",
]
