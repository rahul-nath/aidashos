# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One refinery run over one project's queue: the rule, the git, and the rows.

Three things exist separately on purpose and this is where they meet. `bisect`
decides order and attribution and touches no git. `stack` builds worktrees and
merges commits and decides nothing. `coordination.integration_queue` writes rows
and knows neither. A driver that folded any two of them together would be the
shape this design was written against, so this module is deliberately small and
does nothing either of the other two could have done.

What milestone 3 does and does not do
=====================================

It builds the stack and stops. Merges happen in queue order, a conflict is
attributed to the request that would not apply and parked durably, the built
stack is checked for containment and provenance, and the worktree is removed on
every path. **The integrated branch is never advanced**, so a stack that builds
cleanly ends the run as `StackAbandoned` under
`StackAbandonment.INTEGRATED_BRANCH_ADVANCE_UNIMPLEMENTED` and its members go
back to `Queued`.

That makes conflict attribution the only durable verdict this milestone
produces, and it is a real one: requests ahead of a conflict applied cleanly by
construction, so the culprit is known without running a gate at all. Milestone 4
adds the gate and the fast-forward, at which point a green stack lands instead
of abandoning.

Why the base is re-read every attempt
=====================================

`integrated_tip` is read at the top of each attempt rather than once per run. In
milestone 3 the tip never moves, so this is invisible; from milestone 4 on, a run
that bisects lands ``L`` before attempting ``R``, and ``R`` must be verified on
top of whatever ``L`` landed. Reading it once would build ``R`` on a base that is
no longer the branch, which is exactly the clause that makes the bisect rule
complete rather than merely plausible.

Why a parked request is written the moment it is isolated
=========================================================

Not at the end of the run. The row needs the base the stack was built on when it
failed, and that base belongs to the attempt, not to the run: a request isolated
under one combination may land under another, and an operator deciding what to do
needs to know which one refused it. Collecting isolations and writing them at the
end would mean either threading their bases through the rule, which does not
model shas, or writing one base for all of them, which would be wrong for any run
that bisected.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import assert_never

from ..coordination.integration_queue import (
    claim_requests_for_attempt,
    record_bisected_out,
    return_requests_to_queue,
)
from ..coordination.store import ConnectionLike
from ..project_center import LinkedProject
from .bisect import (
    AwaitingStack,
    IntegrationOutcome,
    Isolation,
    RunAbandoned,
    RunCompleted,
    StackAbandoned,
    StackAbandonment,
    StackAttempt,
    StackMergeConflict,
    StackOutcome,
    begin_integration,
    record_stack_outcome,
)
from .queue import SelectedBatch
from .requests import (
    InFlight,
    IntegrationAttemptId,
    IntegrationBatchId,
    IntegrationRequestId,
    IntegrationSubject,
)
from .stack import (
    GitFailure,
    IntegrationWorkspace,
    ProvenanceBroken,
    ProvenanceHeld,
    StackBuilder,
    StackBuilt,
    StackConflicted,
    StackUnbuildable,
)

_NO_EVIDENCE_ARTIFACT = ""
"""A merge conflict's evidence is its conflicted paths, which the cause carries.

Milestone 5 introduces evidence artifacts for gate failures, where the command
output is too large for a row and genuinely needs somewhere to live. A conflict
does not have that problem, and minting an artifact id that resolves to nothing
would be worse than an empty field a reader can see is empty.
"""


@dataclass(frozen=True)
class RefineryRun:
    """What one drained batch did, for the loop and for an operator's report."""

    batch_id: IntegrationBatchId
    outcome: IntegrationOutcome

    @property
    def decided_anything(self) -> bool:
        """Whether this run produced a durable verdict about any request.

        The loop polls again immediately when it did and sleeps when it did not.
        A run that abandoned decided nothing and would abandon the same way on
        the next pass, so draining again would spin against a condition that
        only changes outside the queue.
        """

        return bool(self.outcome.integrated or self.outcome.isolated)


def integrate_batch(
    c: ConnectionLike,
    batch: SelectedBatch,
    *,
    project: LinkedProject,
    builder: StackBuilder,
    now: float,
) -> RefineryRun:
    """Drive one batch to a verdict, leaving no worktree and no moved ref.

    The caller holds this project's refinery lock and has already returned any
    outstanding `InFlight` rows to `Queued`; `select_next_batch` crashes rather
    than proceeding if that was skipped.
    """

    batch_id = IntegrationBatchId(f"ib_{uuid.uuid4().hex[:24]}")
    in_flight = {
        request.subject.request_id: request
        for request in claim_requests_for_attempt(
            c,
            batch.requests,
            batch_id=batch_id,
            attempt_id=IntegrationAttemptId(f"ia_{uuid.uuid4().hex[:24]}"),
            recorded_at=now,
        )
    }

    progress: AwaitingStack | IntegrationOutcome = begin_integration(batch)
    while isinstance(progress, AwaitingStack):
        awaiting = progress
        outcome, base_sha = _attempt(
            awaiting.attempt,
            batch=batch,
            project=project,
            builder=builder,
            batch_id=batch_id,
        )
        progress = record_stack_outcome(awaiting, outcome)
        for isolation in _newly_isolated(awaiting.isolated, progress.isolated):
            record_bisected_out(
                c,
                in_flight[isolation.request_id],
                cause=isolation.cause,
                stack_beneath=isolation.stack_beneath,
                stack_base_sha=base_sha,
                evidence_artifact_id=_NO_EVIDENCE_ARTIFACT,
                recorded_at=now,
            )

    _release_undecided(c, progress, in_flight=in_flight, now=now)
    return RefineryRun(batch_id=batch_id, outcome=progress)


def _attempt(
    attempt: StackAttempt,
    *,
    batch: SelectedBatch,
    project: LinkedProject,
    builder: StackBuilder,
    batch_id: IntegrationBatchId,
) -> tuple[StackOutcome, str]:
    """Build one stack and say what happened to it, plus the base it was cut from.

    The base comes back with the outcome because a parked request's row needs it
    and the rule that produced the isolation does not model shas.
    """

    try:
        base_sha = builder.integrated_tip(project.integrated_branch)
    except GitFailure:
        # Enqueue refused any request whose project declares a branch the
        # repository does not have, so reaching this means the branch was deleted
        # after an approval. Nobody is parked for that.
        return (
            StackAbandoned(StackAbandonment.INTEGRATION_WORKTREE_UNAVAILABLE),
            _UNREADABLE_BASE,
        )

    allocation = builder.allocate(batch_id=batch_id, base_sha=base_sha)
    if isinstance(allocation, StackUnbuildable):
        return StackAbandoned(StackAbandonment.INTEGRATION_WORKTREE_UNAVAILABLE), base_sha

    try:
        return _build_and_check(allocation, attempt, batch=batch, builder=builder), base_sha
    finally:
        # Every path, green or red or exceptional. `git worktree list` on the
        # target project must be clean after every run, and the one way to make
        # that true for the exceptional path too is to not have a path that skips
        # it.
        builder.teardown(allocation)


def _build_and_check(
    workspace: IntegrationWorkspace,
    attempt: StackAttempt,
    *,
    batch: SelectedBatch,
    builder: StackBuilder,
) -> StackOutcome:
    subjects = _subjects_for(batch, attempt.candidates)
    built = builder.build(workspace, subjects)
    match built:
        case StackConflicted(request_id=request_id, conflict=conflict):
            return StackMergeConflict(request_id=request_id, conflict=conflict)
        case StackUnbuildable():
            return StackAbandoned(StackAbandonment.INTEGRATION_WORKTREE_UNAVAILABLE)
        case StackBuilt(tip_sha=tip_sha):
            return _check_provenance(workspace, subjects, builder=builder, tip_sha=tip_sha)
    assert_never(built)


def _check_provenance(
    workspace: IntegrationWorkspace,
    subjects: Sequence[IntegrationSubject],
    *,
    builder: StackBuilder,
    tip_sha: str,
) -> StackOutcome:
    """The stack applied. Prove it carries exactly the approved work, then stop.

    Milestone 4 replaces the final return with the gate and the fast-forward.
    Until then a proven stack is still abandoned, because reporting it as landed
    would tell the rule that a branch had moved when it had not.
    """

    try:
        verdict = builder.verify_provenance(workspace, subjects, tip_sha=tip_sha)
    except GitFailure:
        return StackAbandoned(StackAbandonment.INTEGRATION_WORKTREE_UNAVAILABLE)
    match verdict:
        case ProvenanceBroken():
            return StackAbandoned(StackAbandonment.PROVENANCE_NOT_ESTABLISHED)
        case ProvenanceHeld():
            return StackAbandoned(StackAbandonment.INTEGRATED_BRANCH_ADVANCE_UNIMPLEMENTED)
    assert_never(verdict)


def _subjects_for(
    batch: SelectedBatch,
    request_ids: Sequence[IntegrationRequestId],
) -> tuple[IntegrationSubject, ...]:
    return tuple(batch.subject_for(request_id) for request_id in request_ids)


def _newly_isolated(
    before: Sequence[Isolation],
    after: Sequence[Isolation],
) -> tuple[Isolation, ...]:
    """The isolations one step added. At most one, and asserted rather than assumed."""

    added = tuple(after[len(before) :])
    if list(after[: len(before)]) != list(before):
        raise RuntimeError(
            "recording a stack outcome rewrote an isolation the run had already made; "
            "isolations are append-only and a parked request is terminal"
        )
    if len(added) > 1:
        raise RuntimeError(f"one stack outcome isolated {len(added)} requests; at most one can")
    return added


def _release_undecided(
    c: ConnectionLike,
    outcome: IntegrationOutcome,
    *,
    in_flight: dict[IntegrationRequestId, InFlight],
    now: float,
) -> None:
    """Return whatever the run did not decide, and refuse to lose anything.

    The partition check is the point. A run that dropped a request would leave an
    `InFlight` row nobody ever looks at again, which is invisible when it happens
    and expensive later: the next poll's `select_next_batch` crashes on it, a
    long way from the code that lost it.
    """

    match outcome:
        case RunCompleted():
            returned: tuple[IntegrationRequestId, ...] = ()
        case RunAbandoned():
            returned = outcome.returned_to_queue
        case _:
            assert_never(outcome)

    accounted = set(outcome.integrated) | {i.request_id for i in outcome.isolated} | set(returned)
    if accounted != set(in_flight):
        raise RuntimeError(
            f"the run accounted for {sorted(accounted)} but claimed {sorted(in_flight)}; "
            "every claimed request must land, be parked, or go back to the queue"
        )

    return_requests_to_queue(c, [in_flight[rid] for rid in returned], recorded_at=now)


_UNREADABLE_BASE = "0" * 40
"""Stands in for a base that could not be read, so the run can still be reported.

Only ever reaches a `BisectedOut` row if a request were isolated in the same
attempt whose base failed to read, which cannot happen: the read is the first
thing the attempt does and its failure returns before any merge.
"""


__all__ = [
    "RefineryRun",
    "integrate_batch",
]
