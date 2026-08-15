# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Whether a blocked milestone may be tried again, as a decision with a reason.

Resume used to turn every ``BLOCKED`` milestone into ``READY`` and increment its
attempt, unconditionally. The compiled plan carries a per-milestone
``max_attempts`` - three for most executor kinds, one for ``review.operator`` -
and the only place that consulted it ran while the WorkUnit was scheduling, and
only for milestones whose status was ``FAILED``. ``BLOCKED`` is where a real
executor failure lands, so the budget applied to the one status a failed run
never occupies. A ``BLOCKED`` attempt 3 became attempt 4, and N resumes bought N
attempts.

The reason it could not simply be tightened is that ``BLOCKED`` is reached from
four different places and only one of them spent a try:

- an executor failure classed ``CORRECTABLE`` / ``REQUIRES_REPLAN`` spent one;
- an executor failure classed ``TRANSIENT`` did not: the provider died in flight,
  so the milestone's own work was never judged;
- an approval whose wait elapsed did not - ``review.operator`` permits a single
  attempt, and counting its wait for a human as a spent try fails every approval
  gate the moment it asks for one;
- a dispatch wait that elapsed did not - the agent may still be working;
- a crash recovery did not, or rather it was spent by a dead process and not by
  the milestone's own work.

The milestone row persisted only ``failure_code``, free text written in four
places, so that distinction was destroyed at the write. It is a datatype problem
and it is fixed as one: ``FailureClass`` is now stored beside the code, and this
module reads the class.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

from .lifecycle import FailureClass, LifecyclePhase, MilestoneExecutionStatus


class RetryGrounds(StrEnum):
    """Why a retry is permitted, so a reader is not left inferring it."""

    WITHIN_BUDGET = "WITHIN_BUDGET"
    """The milestone spent a try and the plan still permits another."""

    NO_ATTEMPT_SPENT = "NO_ATTEMPT_SPENT"
    """It is waiting on something, not recovering from something.

    An approval gate parked for a human, or a dispatch whose wait elapsed while
    the agent kept working. Re-entering a wait is not a retry, and charging it to
    the budget fails milestones that never ran.
    """

    OPERATOR_OVERRIDE = "OPERATOR_OVERRIDE"
    """A person decided the budget should not stop this one."""


@dataclass(frozen=True)
class RetryPermitted:
    """This milestone may go back to READY on ``next_attempt``."""

    milestone_key: str
    phase: LifecyclePhase
    next_attempt: int
    grounds: RetryGrounds


@dataclass(frozen=True)
class RetryRefused:
    """This milestone has spent its budget and must not be tried again.

    Refusing is not the same as failing: the caller records the failure, because
    "what this means for the milestone" is the lifecycle's decision and not this
    module's. What this module owns is the answer, and the numbers behind it.
    """

    milestone_key: str
    phase: LifecyclePhase
    attempt: int
    permitted: int

    def describe(self) -> str:
        return (
            f"milestone {self.milestone_key} exhausted its {self.permitted} "
            f"permitted attempt(s); a new plan revision (a WorkUnit that "
            f"supersedes this one) or an explicit operator override is required"
        )


# The two answers a resume can get about one blocked milestone. A sum rather
# than a boolean because the refusal carries the numbers an operator needs and
# the permission carries the attempt to write.
type RetryDecision = RetryPermitted | RetryRefused


# Which failure classes mean the milestone's own work consumed an attempt.
# Derived from `FailureClass` rather than from `failure_code` strings, so adding
# a class is a type error here instead of a silent "spent nothing".
def spends_an_attempt(failure_class: FailureClass | None) -> bool:
    """Whether a milestone in this failure class used one of its permitted tries.

    ``None`` means the row records no failure at all, which a BLOCKED milestone
    can legitimately be: a WorkUnit blocked before this column existed, or a
    block written by something that had no class to give. Not charging it is the
    conservative reading - the budget exists to stop runaway retries, and the
    cost of under-charging is one extra try while the cost of over-charging is a
    milestone failed for work it never did.
    """

    if failure_class is None:
        return False
    match failure_class:
        case FailureClass.TRANSIENT:
            # The provider failed, not the work. Nothing was attempted and
            # nothing was learned, so there is no try to charge for.
            #
            # This class had no producer until a dropped API stream was being
            # recorded as `CORRECTABLE` and spending one of three attempts for a
            # request that died in flight. Three infrastructure failures in a
            # row then exhausted a milestone whose code had never been judged
            # once, and clearing that took an operator override.
            #
            # It stays safe from runaway retries because a BLOCKED milestone is
            # only re-entered by an explicit resume; nothing re-drives it on its
            # own, so a provider that keeps dropping keeps failing in front of
            # the operator rather than in a loop.
            return False
        case FailureClass.CORRECTABLE | FailureClass.REQUIRES_REPLAN:
            return True
        case (
            FailureClass.REQUIRES_OPERATOR
            | FailureClass.POLICY_VIOLATION
            | FailureClass.NONRECOVERABLE
        ):
            # None of these reach BLOCKED through `_status_for_failure`, and a
            # milestone that got here carrying one is parked rather than retried.
            return False
    assert_never(failure_class)


def decide_retry(
    *,
    milestone_key: str,
    phase: LifecyclePhase,
    status: MilestoneExecutionStatus,
    attempt: int,
    failure_class: FailureClass | None,
    max_attempts: int,
    operator_override: bool = False,
) -> RetryDecision:
    """Answer, for one blocked milestone, whether a resume may run it again.

    Pure: every input is a value the caller already holds, so the decision can be
    tested without a ledger and cannot read anything the caller did not pass.

    The attempt is never reset. It is monotonic in the row by construction and it
    is part of the child workflow identity and the dispatch idempotency key, so
    reusing a number would collide a fresh run with a DBOS workflow that already
    reached a terminal state.
    """

    assert status is MilestoneExecutionStatus.BLOCKED, (
        f"decide_retry is asked about a blocked milestone, not a {status.value} one"
    )
    if operator_override:
        return RetryPermitted(
            milestone_key=milestone_key,
            phase=phase,
            next_attempt=attempt + 1,
            grounds=RetryGrounds.OPERATOR_OVERRIDE,
        )
    if not spends_an_attempt(failure_class):
        return RetryPermitted(
            milestone_key=milestone_key,
            phase=phase,
            next_attempt=attempt + 1,
            grounds=RetryGrounds.NO_ATTEMPT_SPENT,
        )
    if attempt >= max_attempts:
        return RetryRefused(
            milestone_key=milestone_key,
            phase=phase,
            attempt=attempt,
            permitted=max_attempts,
        )
    return RetryPermitted(
        milestone_key=milestone_key,
        phase=phase,
        next_attempt=attempt + 1,
        grounds=RetryGrounds.WITHIN_BUDGET,
    )


ATTEMPT_BUDGET_EXHAUSTED = "attempt_budget_exhausted"
"""The failure code a refused retry records, shared with the scheduler's check.

One spelling for one decision. The scheduler's FAILED-path check writes the same
code, and two literals agreeing only by inspection is how the budget came to be
enforced on one status and not the other.
"""


__all__ = [
    "ATTEMPT_BUDGET_EXHAUSTED",
    "RetryDecision",
    "RetryGrounds",
    "RetryPermitted",
    "RetryRefused",
    "decide_retry",
    "spends_an_attempt",
]
