# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Which stack to build next, and who to blame when one is not green.

This is the whole bisect rule and it touches no git. It is a step machine, not a
recursion over a callback: `begin_integration` names the first stack to build,
`record_stack_outcome` takes what happened to it and names the next one, and the
run is over when there is nothing pending. The driver owns the worktree, the
merges, the verification gate, and the fast-forward; this module owns order and
attribution, which are the parts that are wrong quietly.

**A stack, in this module, is a list of request ids.** ``StackAttempt.landed`` is
what the integrated branch already carries and ``StackAttempt.candidates`` is
what goes on top of it, in that order. The design doc phrases the same recursion
in terms of a base sha that changes as sub-batches settle; ids are the honest
form of that, because "which combination refused this request" is a fact about
requests, and the sha it adds up to is the driver's translation of it.

**Reporting `StackLanded` means the fast-forward already happened.** The next
attempt is built on ``landed``, so a green stack that had not yet advanced the
integrated branch would have the rule planning merges onto a base that does not
exist. Verifying before advancing and never after is the invariant; keeping the
two in one reported outcome is how this module cannot be the thing that breaks
it.

The rule
========

Let ``S`` be the segment being attempted, ``n = len(S)``.

- **Merge conflict on request ``k``.** Requests before ``k`` in ``S`` applied
  cleanly by construction, so ``k`` is attributable in O(1) with no gate run at
  all. Park it, drop it, and re-attempt the rest of ``S`` from the same base.
  Conflicts are the cheap case and sending them through bisection would pay
  ``log n`` gate runs for an answer already in hand.
- **``n == 1`` and red.** That request is parked. No split, no recursion. This is
  the base case, and it is why the machine terminates.
- **``n > 1`` and red.** Split preserving order into ``L = S[:ceil(n/2)]`` and
  ``R = S[ceil(n/2):]``, and attempt ``L`` first.
- **Green.** Every member of ``S`` landed.

``R`` is attempted on top of whatever ``L`` landed, never against the original
base. This is the clause that makes the rule complete rather than merely
plausible: two requests that are each individually green but semantically
incompatible, one renaming a symbol the other calls, produce no merge conflict
and no individual failure, and a bisect that verified each half against the
original base would land both and leave the integrated branch red. Because
``pending`` is processed head-first and ``L`` is pushed ahead of ``R``, every
descendant of ``L`` settles before ``R`` is attempted, and ``landed`` has grown
by then. The later request in enqueue order is the one parked, which is a rule
rather than a judgment.

Termination
===========

Give the pending work the measure ``sum(2 * len(segment) - 1)``, the node count
of a full binary tree over each segment's members. Every outcome strictly
decreases it: green removes a segment, a conflict takes a segment from ``n`` to
``n - 1`` (``-2``), a red singleton removes one (``-1``), and a red split turns
``2n - 1`` into ``(2*ceil(n/2) - 1) + (2*floor(n/2) - 1) = 2n - 2`` (``-1``). The
measure is a non-negative integer, so the run ends. It also bounds the work: one
culprit in a batch of ``N`` costs at most ``2 * ceil(log2 N) + 1`` gate runs, and
a batch where nothing can land degrades to ``2N - 1``, which is the case where
nothing was going to land anyway.

What this module refuses to decide
==================================

Whether a stack is green. That is the target project's own declared verification
commands, and the refinery adds no second definition of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from itertools import chain
from typing import assert_never

from .queue import SelectedBatch
from .requests import BisectCause, GateFailed, IntegrationRequestId, MergeConflict

type QueueSegment = tuple[IntegrationRequestId, ...]
"""A run of not-yet-decided requests, in enqueue order. Never empty."""


@dataclass(frozen=True)
class StackAttempt:
    """One thing the driver is being asked to build, verify, and land.

    Merge ``candidates`` onto the integrated branch in this order, run the
    project's verification gate on the result, and fast-forward the integrated
    branch to it if the gate is green. ``landed`` is what the branch already
    carries, and it is here so the attempt can be read on its own: the pair is
    exactly the combination the gate is about to judge.
    """

    landed: tuple[IntegrationRequestId, ...]
    candidates: QueueSegment

    @property
    def combination(self) -> tuple[IntegrationRequestId, ...]:
        """Everything the gate will see, which is what a red verdict is about."""

        return self.landed + self.candidates


@dataclass(frozen=True)
class StackLanded:
    """Merged cleanly, gate green, integrated branch fast-forwarded to it."""


@dataclass(frozen=True)
class StackGateRed:
    """Merged cleanly, and the project's own gate refused the combination.

    A statement about the combination and not about any one member, which is why
    it carries no request id: deciding which member to blame is this module's job
    and reporting a guess here would let the driver make it instead.
    """

    failure: GateFailed


@dataclass(frozen=True)
class StackMergeConflict:
    """A candidate would not apply onto the candidates before it.

    Attributable without a gate run, so this outcome does name its request.
    """

    request_id: IntegrationRequestId
    conflict: MergeConflict


class StackAbandonment(StrEnum):
    """Why an attempt returned no verdict about anybody's diff.

    None of these are statements about a commit. A fast-forward the target
    repository refuses because an operator left the working tree dirty says
    nothing about the stack, and parking someone for it would discard reviewed,
    approved work over an open editor.

    A run that ends in one of these is also the reason the loop sleeps rather
    than draining again: the condition that stopped it is outside the queue, so
    re-attempting immediately would spin without changing anything.
    """

    FAST_FORWARD_REFUSED = "FAST_FORWARD_REFUSED"
    INTEGRATION_WORKTREE_UNAVAILABLE = "INTEGRATION_WORKTREE_UNAVAILABLE"
    PROVENANCE_NOT_ESTABLISHED = "PROVENANCE_NOT_ESTABLISHED"
    """The built stack carries work no approved request contributed.

    Not attributable to a member, because the refinery adds only merge commits
    and never resolves a conflict, so it introduces no content of its own. A
    stack that nonetheless carries an unapproved commit means something the
    refinery does not model happened to the repository, and the safe reading is
    that this run cannot say anything about anyone. It fails the same way on the
    next poll, which is correct: it should stay loud rather than park good work.
    """

    INTEGRATED_BRANCH_ADVANCE_UNIMPLEMENTED = "INTEGRATED_BRANCH_ADVANCE_UNIMPLEMENTED"
    """Milestone 3 built the stack and is not allowed to land it.

    The dry run is the whole of milestone 3: allocate, merge in order, attribute
    conflicts, check provenance, tear down, and stop. Reporting `StackLanded`
    instead would be a lie with consequences, because the next attempt is built
    on ``landed`` and the branch it names would not have moved.

    **Milestone 4 deletes this member.** It is here rather than as a flag on the
    driver so that the one place a green stack currently stops is a value a test
    can assert on and a reader can grep for.
    """


@dataclass(frozen=True)
class StackAbandoned:
    """The attempt could not be carried out. Nobody is parked for it."""

    reason: StackAbandonment


type StackOutcome = StackLanded | StackGateRed | StackMergeConflict | StackAbandoned


@dataclass(frozen=True)
class Isolation:
    """One request this rule attributes a conflict or a red gate to.

    ``stack_beneath`` is the pure form of "which combination refused it": the
    requests already integrated when it failed, plus, for a conflict, the
    candidates ahead of it that applied cleanly. A request isolated beneath one
    combination may land beneath another, and an operator choosing what to do
    needs to know which one said no.
    """

    request_id: IntegrationRequestId
    cause: BisectCause
    stack_beneath: tuple[IntegrationRequestId, ...]


@dataclass(frozen=True)
class AwaitingStack:
    """A run in progress, and the one stack it wants built next.

    ``pending`` is the work the run has not decided yet, as segments in enqueue
    order; flattening it always yields a subsequence of the batch, and the head
    is the segment under attempt. It is a field rather than a call stack because
    that is what makes the run a value: it can be asserted about, replayed, and
    later written to a row, and the design's requirement that the queue never be
    reconstructed by inspecting git starts with the queue being something other
    than a stack frame.
    """

    integrated: tuple[IntegrationRequestId, ...]
    isolated: tuple[Isolation, ...]
    pending: tuple[QueueSegment, ...]

    def __post_init__(self) -> None:
        if not self.pending:
            raise ValueError("AwaitingStack has nothing to attempt; the run is over")
        if any(not segment for segment in self.pending):
            raise ValueError("an empty pending segment would ask for a stack of nothing")

    @property
    def attempt(self) -> StackAttempt:
        """Derived, not stored, so the attempt cannot drift from the run."""

        return StackAttempt(landed=self.integrated, candidates=self.pending[0])


@dataclass(frozen=True)
class RunCompleted:
    """Every request in the batch either landed or was parked."""

    integrated: tuple[IntegrationRequestId, ...]
    isolated: tuple[Isolation, ...]


@dataclass(frozen=True)
class RunAbandoned:
    """The run stopped for a reason that judges nobody.

    Separate from `RunCompleted` because ``returned_to_queue`` and ``reason`` are
    exactly the fields a completed run has nothing to put in. The returned
    requests go back to `Queued` and the batch is re-attempted from the new tip;
    what already landed stays landed, because it was verified and the branch was
    already advanced to it.
    """

    integrated: tuple[IntegrationRequestId, ...]
    isolated: tuple[Isolation, ...]
    returned_to_queue: tuple[IntegrationRequestId, ...]
    reason: StackAbandonment


type IntegrationOutcome = RunCompleted | RunAbandoned
type IntegrationProgress = AwaitingStack | RunCompleted | RunAbandoned


def begin_integration(batch: SelectedBatch) -> AwaitingStack:
    """Start a run by asking for the whole batch as one stack.

    Total, and it always returns an attempt: `SelectedBatch` cannot be empty, so
    there is no "began with nothing" case to encode here and no way for a caller
    to reach a trivially green empty stack through this function.

    The first attempt is the whole batch rather than one request at a time
    because the common case is that everything works together, and that case
    costs exactly one gate run.
    """

    return AwaitingStack(integrated=(), isolated=(), pending=(batch.request_ids,))


def record_stack_outcome(
    awaiting: AwaitingStack,
    outcome: StackOutcome,
) -> IntegrationProgress:
    """Fold what happened to one stack into the run, and name the next stack.

    Pure and total. Taking the run as a value rather than resuming a coroutine is
    what lets the same queue and the same outcomes replay to the same answer, and
    what lets a test assert the intermediate states rather than only the result.
    """

    head = awaiting.pending[0]
    rest = awaiting.pending[1:]
    match outcome:
        case StackLanded():
            return _advance(
                integrated=awaiting.integrated + head,
                isolated=awaiting.isolated,
                pending=rest,
            )
        case StackMergeConflict(request_id=request_id, conflict=conflict):
            if request_id not in head:
                raise ValueError(
                    f"{request_id} conflicted, but the stack under attempt is {head}; "
                    "a conflict can only be reported against a candidate"
                )
            position = head.index(request_id)
            isolation = Isolation(
                request_id=request_id,
                cause=conflict,
                stack_beneath=awaiting.integrated + head[:position],
            )
            remaining = head[:position] + head[position + 1 :]
            return _advance(
                integrated=awaiting.integrated,
                isolated=awaiting.isolated + (isolation,),
                pending=((remaining,) if remaining else ()) + rest,
            )
        case StackGateRed(failure=failure):
            if len(head) == 1:
                isolation = Isolation(
                    request_id=head[0],
                    cause=failure,
                    stack_beneath=awaiting.integrated,
                )
                return _advance(
                    integrated=awaiting.integrated,
                    isolated=awaiting.isolated + (isolation,),
                    pending=rest,
                )
            split = (len(head) + 1) // 2
            return _advance(
                integrated=awaiting.integrated,
                isolated=awaiting.isolated,
                pending=(head[:split], head[split:]) + rest,
            )
        case StackAbandoned(reason=reason):
            return RunAbandoned(
                integrated=awaiting.integrated,
                isolated=awaiting.isolated,
                returned_to_queue=tuple(chain.from_iterable(awaiting.pending)),
                reason=reason,
            )
    assert_never(outcome)


def _advance(
    *,
    integrated: tuple[IntegrationRequestId, ...],
    isolated: tuple[Isolation, ...],
    pending: tuple[QueueSegment, ...],
) -> IntegrationProgress:
    if not pending:
        return RunCompleted(integrated=integrated, isolated=isolated)
    return AwaitingStack(integrated=integrated, isolated=isolated, pending=pending)


def decided_request_ids(outcome: IntegrationOutcome) -> tuple[IntegrationRequestId, ...]:
    """Every request the run accounted for, in no particular order.

    Exists so the driver can assert the partition before it writes rows: a run
    that lost a request would leave a `Queued` row nobody ever looks at again,
    and that is invisible at the point it happens and expensive later.
    """

    isolated = tuple(isolation.request_id for isolation in outcome.isolated)
    match outcome:
        case RunCompleted():
            return outcome.integrated + isolated
        case RunAbandoned():
            return outcome.integrated + isolated + outcome.returned_to_queue
    assert_never(outcome)


__all__ = [
    "AwaitingStack",
    "IntegrationOutcome",
    "IntegrationProgress",
    "Isolation",
    "QueueSegment",
    "RunAbandoned",
    "RunCompleted",
    "StackAbandoned",
    "StackAbandonment",
    "StackAttempt",
    "StackGateRed",
    "StackLanded",
    "StackMergeConflict",
    "StackOutcome",
    "begin_integration",
    "decided_request_ids",
    "record_stack_outcome",
]
