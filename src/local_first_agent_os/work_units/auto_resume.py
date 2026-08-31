# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded automatic re-drive of transient-blocked WorkUnits.

A TRANSIENT failure means the request died in flight and the work was never
judged: `attempt_charge` leaves it uncharged, and `decide_retry` grants such a
milestone a fresh execution ordinal without consuming its budget. The
boundedness of that grant used to rest on inaction - nothing re-drove a BLOCKED
milestone on its own - which also meant a dropped provider stream parked the
whole WorkUnit in front of an operator.

This module is the actor that re-drives, and it carries the bound the inaction
used to provide. A WorkUnit qualifies only when every one of its BLOCKED
milestones is transient-blocked, and each has at most `max_transient_resumes`
recorded transient failures; the failure beyond that stands for the operator.
A BLOCKED WorkUnit whose milestones already stand READY with no pending
delivery is also re-driven, because that shape is a resume that never reached a
runtime, and the retries in it were already permitted.

The sweep writes the same durable RESUME outbox row an operator resume writes,
so delivery, crash recovery, and the per-milestone retry decision all run
through `service.resume_work_unit`, and the ledger's failure history is itself
the counter: a crashed sweep forgets nothing and re-derives the same answer.

The unattended-re-drive hazard ("a reconciler in front of an unbounded spawn
path is a machine for re-running an over-permitted process") is answered with
scope rather than waived: every re-driven attempt passes spawn authority and
the capability gate afresh, only work those gates already shaped is re-run, a
judged failure is never retried from here, and the total re-drives per
milestone are bounded. `max_transient_resumes=0` disables the sweep.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import repository as repo
from .lifecycle import FailureClass, MilestoneExecutionStatus, WorkUnitStatus

DEFAULT_MAX_TRANSIENT_RESUMES = 3


@dataclass(frozen=True)
class AutoResumeEnqueued:
    """One BLOCKED WorkUnit now has a pending RESUME delivery."""

    work_unit_id: str
    reason: str


@dataclass(frozen=True)
class AutoResumeRefused:
    """One BLOCKED WorkUnit was considered and deliberately left parked.

    Only the bound produces this: a milestone whose transient failures exceed
    `max_transient_resumes` stands for the operator. Units that do not qualify
    at all (a judged failure, a pending decision, an already-pending delivery)
    are skipped silently, because the sweep runs every few seconds and a
    refusal that recurs on every pass is noise, not news.
    """

    work_unit_id: str
    milestone_key: str
    transient_failures: int


type AutoResumeOutcome = AutoResumeEnqueued | AutoResumeRefused


def sweep_transient_blocked(
    max_transient_resumes: int = DEFAULT_MAX_TRANSIENT_RESUMES,
) -> tuple[AutoResumeOutcome, ...]:
    """Enqueue a RESUME for every BLOCKED WorkUnit the bound permits."""

    if max_transient_resumes < 1:
        return ()
    outcomes: list[AutoResumeOutcome] = []
    for unit in repo.list_work_units(status=WorkUnitStatus.BLOCKED):
        outcome = _consider(unit.work_unit_id, max_transient_resumes)
        if outcome is not None:
            outcomes.append(outcome)
    return tuple(outcomes)


def _consider(work_unit_id: str, max_transient_resumes: int) -> AutoResumeOutcome | None:
    if repo.has_pending_enqueue(work_unit_id):
        return None
    executions = repo.list_milestone_executions(work_unit_id)
    blocked = [item for item in executions if item.status is MilestoneExecutionStatus.BLOCKED]
    if not blocked:
        ready = [item for item in executions if item.status is MilestoneExecutionStatus.READY]
        if not ready:
            return None
        # A unit halted BLOCKED with READY milestones and nothing pending is a
        # resume whose continuation was never delivered. Its retries were
        # already permitted, so re-driving spends nothing new; consuming the
        # READY is what keeps this branch from recurring.
        if not repo.enqueue_resume(work_unit_id):
            return None
        return AutoResumeEnqueued(
            work_unit_id=work_unit_id,
            reason="ready milestones had no pending delivery",
        )
    failures = repo.list_milestone_failure_attempts(work_unit_id)
    for execution in blocked:
        current = next(
            (
                failure.failure_class
                for failure in failures
                if failure.stable_key == execution.stable_key
                and failure.execution_ordinal == execution.attempt
            ),
            execution.failure_class,
        )
        if current is not FailureClass.TRANSIENT:
            # A judged failure spends budget on retry, and a missing class is
            # an operator matter; neither may be re-driven unattended.
            return None
    for execution in blocked:
        transient_failures = sum(
            1
            for failure in failures
            if failure.stable_key == execution.stable_key
            and failure.failure_class is FailureClass.TRANSIENT
        )
        if transient_failures > max_transient_resumes:
            return AutoResumeRefused(
                work_unit_id=work_unit_id,
                milestone_key=execution.stable_key,
                transient_failures=transient_failures,
            )
    if not repo.enqueue_resume(work_unit_id):
        return None
    return AutoResumeEnqueued(
        work_unit_id=work_unit_id,
        reason="every blocked milestone is transient-blocked within the bound",
    )


__all__ = [
    "DEFAULT_MAX_TRANSIENT_RESUMES",
    "AutoResumeEnqueued",
    "AutoResumeOutcome",
    "AutoResumeRefused",
    "sweep_transient_blocked",
]
