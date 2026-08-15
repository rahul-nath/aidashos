# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What the integration queue decides, before any of it touches git.

The bisect rule is the piece worth having exactly right and it needs no
repository to exercise: every scenario below is the rule driven against a fake
gate that decides greenness from the set of request ids on the stack. The fake is
the point. It lets a test say "these two are individually fine and together they
are not", which is the case a merge queue exists for and the one a git fixture
would take a hundred lines to stage.

Two properties are asserted on every step of every run rather than in one
scenario of their own, because they are what the rule promises and a scenario
only samples them: the not-yet-decided work stays a subsequence of the batch in
enqueue order through every split, and no request is ever lost or counted twice.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from itertools import chain

import pytest

from local_first_agent_os.refinery.bisect import (
    AwaitingStack,
    IntegrationOutcome,
    IntegrationProgress,
    Isolation,
    RunAbandoned,
    RunCompleted,
    StackAbandoned,
    StackAbandonment,
    StackAttempt,
    StackGateRed,
    StackLanded,
    StackMergeConflict,
    StackOutcome,
    begin_integration,
    decided_request_ids,
    record_stack_outcome,
)
from local_first_agent_os.refinery.queue import (
    NothingToIntegrate,
    SelectedBatch,
    select_next_batch,
)
from local_first_agent_os.refinery.requests import (
    BisectedOut,
    GateFailed,
    InFlight,
    Integrated,
    IntegrationAttemptId,
    IntegrationBatchId,
    IntegrationRequest,
    IntegrationRequestId,
    IntegrationRequestState,
    IntegrationSubject,
    MergeConflict,
    Queued,
    WithdrawalReason,
    Withdrawn,
    require_integration_transition,
    state_of,
)

PROJECT = "demo_project"
OTHER_PROJECT = "other_project"

FAILING_GATE = GateFailed(
    command="uv run pytest -q",
    exit_code=1,
    output_excerpt="1 failed, 42 passed",
)


def _sha(seed: str) -> str:
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()


def _ids(names: str) -> tuple[IntegrationRequestId, ...]:
    return tuple(IntegrationRequestId(name) for name in names)


def _queued(
    name: str,
    *,
    enqueued_at: float,
    project: str = PROJECT,
) -> Queued:
    return Queued(subject=_subject(name, enqueued_at=enqueued_at, project=project))


def _subject(
    name: str,
    *,
    enqueued_at: float,
    project: str = PROJECT,
) -> IntegrationSubject:
    return IntegrationSubject(
        request_id=IntegrationRequestId(name),
        target_project_id=project,
        branch_name=f"agent/pow-wow-{name}-9f2c",
        base_head_sha=_sha("base"),
        commit_sha=_sha(f"commit:{name}"),
        approval_id=f"approval-{name}",
        intent_id=f"intent-{name}",
        pow_wow_id=f"pow-wow-{name}",
        milestone_key=f"milestone-{name}",
        changed_files=(f"src/{name}.py",),
        enqueued_at=enqueued_at,
    )


def _batch(names: str, *, project: str = PROJECT) -> SelectedBatch:
    return SelectedBatch(
        target_project_id=project,
        requests=tuple(
            _queued(name, enqueued_at=float(position), project=project)
            for position, name in enumerate(names)
        ),
    )


Gate = Callable[[StackAttempt], StackOutcome]


def _always_green(attempt: StackAttempt) -> StackOutcome:
    del attempt
    return StackLanded()


def _red_for(bad: str) -> Gate:
    """Red whenever any of these requests is anywhere on the stack."""

    forbidden = set(_ids(bad))

    def gate(attempt: StackAttempt) -> StackOutcome:
        if forbidden & set(attempt.combination):
            return StackGateRed(failure=FAILING_GATE)
        return StackLanded()

    return gate


def _incompatible(pair: str) -> Gate:
    """Red only when both of these are on the stack together.

    Each is green on its own, neither conflicts, and the combination fails: the
    symbol-rename case that a bisect verifying each half against the original
    base would land twice and leave the integrated branch red.
    """

    together = set(_ids(pair))

    def gate(attempt: StackAttempt) -> StackOutcome:
        if together <= set(attempt.combination):
            return StackGateRed(failure=FAILING_GATE)
        return StackLanded()

    return gate


def _conflicts_on(name: str, *, paths: tuple[str, ...] = ("src/shared.py",)) -> Gate:
    conflicting = IntegrationRequestId(name)

    def gate(attempt: StackAttempt) -> StackOutcome:
        if conflicting in attempt.candidates:
            return StackMergeConflict(
                request_id=conflicting,
                conflict=MergeConflict(conflicted_paths=paths),
            )
        return StackLanded()

    return gate


def _abandons_after(green_attempts: int, reason: StackAbandonment) -> Gate:
    remaining = [green_attempts]

    def gate(attempt: StackAttempt) -> StackOutcome:
        del attempt
        if remaining[0] <= 0:
            return StackAbandoned(reason=reason)
        remaining[0] -= 1
        return StackLanded()

    return gate


@dataclass
class DrivenRun:
    """One run of the rule against a fake gate, with everything it was asked."""

    outcome: IntegrationOutcome
    attempts: list[StackAttempt] = field(default_factory=list)
    outcomes: list[StackOutcome] = field(default_factory=list)

    @property
    def gate_runs(self) -> int:
        """Attempts that actually spent a verification run.

        A conflict is diagnosed while merging, before the gate is reached, and an
        abandoned attempt never got there either.
        """

        return sum(
            1 for outcome in self.outcomes if isinstance(outcome, StackLanded | StackGateRed)
        )

    @property
    def isolated_ids(self) -> tuple[IntegrationRequestId, ...]:
        return tuple(isolation.request_id for isolation in self.outcome.isolated)


def _is_subsequence(
    part: Sequence[IntegrationRequestId],
    whole: Sequence[IntegrationRequestId],
) -> bool:
    remaining = iter(whole)
    return all(item in remaining for item in part)


def _assert_run_invariants(
    batch_ids: tuple[IntegrationRequestId, ...],
    progress: IntegrationProgress,
) -> None:
    if isinstance(progress, AwaitingStack):
        pending = tuple(chain.from_iterable(progress.pending))
        isolated = tuple(isolation.request_id for isolation in progress.isolated)
        accounted = progress.integrated + isolated + pending
        assert _is_subsequence(pending, batch_ids), "a split reordered the pending queue"
        assert all(progress.pending), "an empty segment would ask for a stack of nothing"
    else:
        accounted = decided_request_ids(progress)
    assert sorted(accounted) == sorted(batch_ids), "a request was lost or counted twice"
    assert _is_subsequence(progress.integrated, batch_ids), "requests landed out of enqueue order"


def _drive(batch: SelectedBatch, gate: Gate, *, step_limit: int = 128) -> DrivenRun:
    batch_ids = batch.request_ids
    progress: IntegrationProgress = begin_integration(batch)
    attempts: list[StackAttempt] = []
    outcomes: list[StackOutcome] = []
    for _ in range(step_limit):
        _assert_run_invariants(batch_ids, progress)
        if not isinstance(progress, AwaitingStack):
            return DrivenRun(outcome=progress, attempts=attempts, outcomes=outcomes)
        attempt = progress.attempt
        outcome = gate(attempt)
        attempts.append(attempt)
        outcomes.append(outcome)
        progress = record_stack_outcome(progress, outcome)
    raise AssertionError(f"the rule did not terminate within {step_limit} attempts")


# --------------------------------------------------------------------------
# Batch selection
# --------------------------------------------------------------------------


def test_an_empty_ledger_selects_nothing_rather_than_a_batch_of_zero() -> None:
    assert select_next_batch([], target_project_id=PROJECT) == NothingToIntegrate(PROJECT)


def test_a_batch_cannot_be_constructed_empty() -> None:
    # The zero case is refused by the type, not by a check at the top of the
    # recursion, so no caller can reach a trivially green empty stack.
    with pytest.raises(ValueError, match="at least one request"):
        SelectedBatch(target_project_id=PROJECT, requests=())


def test_selection_takes_queued_requests_only() -> None:
    subject = _subject("landed", enqueued_at=1.0)
    rows: list[IntegrationRequest] = [
        Integrated(
            subject=subject,
            batch_id=IntegrationBatchId("batch-1"),
            integration_commit_sha=_sha("integrated"),
            integrated_at=2.0,
        ),
        Withdrawn(
            subject=_subject("gone", enqueued_at=1.5),
            reason=WithdrawalReason.APPROVAL_REVOKED,
            withdrawn_at=2.0,
        ),
        _queued("waiting", enqueued_at=3.0),
    ]

    selection = select_next_batch(rows, target_project_id=PROJECT)

    assert isinstance(selection, SelectedBatch)
    assert selection.request_ids == (IntegrationRequestId("waiting"),)


def test_selection_orders_by_enqueue_time_then_request_id() -> None:
    rows = [
        _queued("c", enqueued_at=9.0),
        _queued("b", enqueued_at=1.0),
        _queued("a", enqueued_at=1.0),
    ]

    selection = select_next_batch(rows, target_project_id=PROJECT)

    assert isinstance(selection, SelectedBatch)
    assert selection.request_ids == _ids("abc")


def test_a_batch_never_spans_two_projects() -> None:
    rows = [
        _queued("mine", enqueued_at=1.0),
        _queued("theirs", enqueued_at=2.0, project=OTHER_PROJECT),
    ]

    selection = select_next_batch(rows, target_project_id=PROJECT)

    assert isinstance(selection, SelectedBatch)
    assert selection.request_ids == (IntegrationRequestId("mine"),)
    assert select_next_batch(rows, target_project_id=OTHER_PROJECT) == SelectedBatch(
        target_project_id=OTHER_PROJECT,
        requests=(_queued("theirs", enqueued_at=2.0, project=OTHER_PROJECT),),
    )


def test_an_outstanding_attempt_refuses_selection_rather_than_being_skipped() -> None:
    # Skipping it would build a second stack on a base the first refinery was
    # about to invalidate, and one of the two would silently lose its batch.
    rows: list[IntegrationRequest] = [
        InFlight(
            subject=_subject("running", enqueued_at=1.0),
            batch_id=IntegrationBatchId("batch-1"),
            attempt_id=IntegrationAttemptId("attempt-1"),
        ),
        _queued("waiting", enqueued_at=2.0),
    ]

    with pytest.raises(ValueError, match="recover outstanding attempts"):
        select_next_batch(rows, target_project_id=PROJECT)


def test_two_rows_claiming_one_request_identity_crash() -> None:
    rows = [_queued("a", enqueued_at=1.0), _queued("a", enqueued_at=2.0)]

    with pytest.raises(ValueError, match="two rows claim"):
        select_next_batch(rows, target_project_id=PROJECT)


def test_a_batch_assembled_out_of_order_is_refused() -> None:
    with pytest.raises(ValueError, match="order"):
        SelectedBatch(
            target_project_id=PROJECT,
            requests=(_queued("b", enqueued_at=2.0), _queued("a", enqueued_at=1.0)),
        )


def test_the_batch_maps_an_id_back_to_what_it_lands() -> None:
    batch = _batch("ab")

    assert batch.subject_for(IntegrationRequestId("b")).commit_sha == _sha("commit:b")
    with pytest.raises(KeyError):
        batch.subject_for(IntegrationRequestId("zzz"))


# --------------------------------------------------------------------------
# The bisect rule: the degenerate cases
# --------------------------------------------------------------------------


def test_a_green_batch_lands_whole_in_one_gate_run() -> None:
    run = _drive(_batch("abcd"), _always_green)

    assert run.outcome == RunCompleted(integrated=_ids("abcd"), isolated=())
    assert run.gate_runs == 1
    assert run.attempts == [StackAttempt(landed=(), candidates=_ids("abcd"))]


def test_a_single_green_request_lands_without_a_split() -> None:
    run = _drive(_batch("a"), _always_green)

    assert run.outcome == RunCompleted(integrated=_ids("a"), isolated=())
    assert run.gate_runs == 1


def test_a_single_red_request_is_parked_without_recursing() -> None:
    run = _drive(_batch("a"), _red_for("a"))

    assert run.gate_runs == 1
    assert run.outcome.integrated == ()
    assert run.isolated_ids == _ids("a")
    (isolation,) = run.outcome.isolated
    assert isolation.cause == FAILING_GATE
    assert isolation.stack_beneath == (), "nothing was beneath it; it is red on its own"


def test_a_batch_where_nothing_can_land_parks_everyone() -> None:
    run = _drive(_batch("abcd"), _red_for("abcd"))

    assert run.outcome.integrated == ()
    assert sorted(run.isolated_ids) == sorted(_ids("abcd"))
    assert run.gate_runs == 2 * 4 - 1, "the all-bad bound is 2N-1"


@pytest.mark.parametrize("culprit", ["a", "b", "c"])
def test_one_bad_member_is_isolated_wherever_it_sits(culprit: str) -> None:
    batch = _batch("abc")

    run = _drive(batch, _red_for(culprit))

    assert run.isolated_ids == _ids(culprit)
    assert run.outcome.integrated == _ids("".join(n for n in "abc" if n != culprit))
    assert run.gate_runs <= 5


@pytest.mark.parametrize("culprit", list("abcdefgh"))
def test_one_culprit_in_eight_costs_the_logarithmic_bound(culprit: str) -> None:
    run = _drive(_batch("abcdefgh"), _red_for(culprit))

    assert run.isolated_ids == _ids(culprit)
    assert run.gate_runs <= 2 * 3 + 1


def test_an_isolated_request_records_the_combination_that_refused_it() -> None:
    # A request parked beneath one combination may land beneath another, so the
    # operator is told which one said no, not merely that one did.
    run = _drive(_batch("abc"), _red_for("c"))

    (isolation,) = run.outcome.isolated
    assert isolation.request_id == IntegrationRequestId("c")
    assert isolation.stack_beneath == _ids("ab")


# --------------------------------------------------------------------------
# The bisect rule: the cases it exists for
# --------------------------------------------------------------------------


def test_two_individually_green_but_incompatible_requests_park_the_later_one() -> None:
    batch = _batch("abc")
    gate = _incompatible("ac")

    run = _drive(batch, gate)

    assert run.outcome.integrated == _ids("ab")
    assert run.isolated_ids == _ids("c"), "FIFO decides who adapts, so the later one parks"
    assert gate(StackAttempt(landed=(), candidates=run.outcome.integrated)) == StackLanded(), (
        "the integrated branch has to be green after the run, not merely smaller"
    )


def test_a_merge_conflict_is_attributed_without_spending_a_gate_run() -> None:
    run = _drive(_batch("abc"), _conflicts_on("b"))

    assert run.outcome == RunCompleted(
        integrated=_ids("ac"),
        isolated=(
            Isolation(
                request_id=IntegrationRequestId("b"),
                cause=MergeConflict(conflicted_paths=("src/shared.py",)),
                stack_beneath=_ids("a"),
            ),
        ),
    )
    assert run.gate_runs == 1, "the conflict was diagnosed while merging, not by the gate"
    assert len(run.attempts) == 2


def test_a_conflict_on_the_first_request_of_a_batch_parks_it_alone() -> None:
    run = _drive(_batch("abc"), _conflicts_on("a"))

    assert run.outcome.integrated == _ids("bc")
    assert run.isolated_ids == _ids("a")
    assert run.outcome.isolated[0].stack_beneath == ()


def test_the_same_queue_and_the_same_gate_replay_to_the_same_answer() -> None:
    first = _drive(_batch("abcdef"), _red_for("bd"))
    second = _drive(_batch("abcdef"), _red_for("bd"))

    assert first.outcome == second.outcome
    assert first.attempts == second.attempts


def test_every_split_keeps_the_queue_in_enqueue_order() -> None:
    batch = _batch("abcdefgh")
    progress: IntegrationProgress = begin_integration(batch)
    gate = _red_for("cf")
    seen_a_split = False

    while isinstance(progress, AwaitingStack):
        pending = tuple(chain.from_iterable(progress.pending))
        assert _is_subsequence(pending, batch.request_ids)
        if len(progress.pending) > 1:
            seen_a_split = True
        progress = record_stack_outcome(progress, gate(progress.attempt))

    assert seen_a_split, "this queue is supposed to exercise the split path"
    assert progress.integrated == _ids("abdegh")


# --------------------------------------------------------------------------
# Outcomes that judge nobody
# --------------------------------------------------------------------------


def test_a_refused_fast_forward_returns_the_whole_batch_and_parks_nobody() -> None:
    run = _drive(_batch("abc"), _abandons_after(0, StackAbandonment.FAST_FORWARD_REFUSED))

    assert run.outcome == RunAbandoned(
        integrated=(),
        isolated=(),
        returned_to_queue=_ids("abc"),
        reason=StackAbandonment.FAST_FORWARD_REFUSED,
    )


def test_abandoning_after_a_split_keeps_what_already_landed() -> None:
    # What landed was verified and the branch was already advanced to it, so it
    # cannot be returned to the queue; only the undecided work goes back.
    batch = _batch("abcd")
    progress: IntegrationProgress = begin_integration(batch)
    progress = record_stack_outcome(progress, StackGateRed(failure=FAILING_GATE))
    assert isinstance(progress, AwaitingStack)
    progress = record_stack_outcome(progress, StackLanded())
    assert isinstance(progress, AwaitingStack)
    progress = record_stack_outcome(
        progress,
        StackAbandoned(reason=StackAbandonment.INTEGRATION_WORKTREE_UNAVAILABLE),
    )

    assert progress == RunAbandoned(
        integrated=_ids("ab"),
        isolated=(),
        returned_to_queue=_ids("cd"),
        reason=StackAbandonment.INTEGRATION_WORKTREE_UNAVAILABLE,
    )


# --------------------------------------------------------------------------
# Contract violations crash rather than being absorbed
# --------------------------------------------------------------------------


def test_a_conflict_reported_for_a_request_not_under_attempt_crashes() -> None:
    progress = begin_integration(_batch("ab"))

    with pytest.raises(ValueError, match="can only be reported against a candidate"):
        record_stack_outcome(
            progress,
            StackMergeConflict(
                request_id=IntegrationRequestId("zzz"),
                conflict=MergeConflict(conflicted_paths=()),
            ),
        )


def test_a_run_with_nothing_pending_is_not_representable() -> None:
    with pytest.raises(ValueError, match="nothing to attempt"):
        AwaitingStack(integrated=(), isolated=(), pending=())
    with pytest.raises(ValueError, match="empty pending segment"):
        AwaitingStack(integrated=(), isolated=(), pending=((),))


# --------------------------------------------------------------------------
# The request lifecycle
# --------------------------------------------------------------------------


def test_the_five_states_are_told_apart_by_one_discriminator() -> None:
    subject = _subject("a", enqueued_at=1.0)
    batch_id = IntegrationBatchId("batch-1")
    states = {
        state_of(request)
        for request in (
            Queued(subject=subject),
            InFlight(
                subject=subject,
                batch_id=batch_id,
                attempt_id=IntegrationAttemptId("attempt-1"),
            ),
            Integrated(
                subject=subject,
                batch_id=batch_id,
                integration_commit_sha=_sha("integrated"),
                integrated_at=2.0,
            ),
            BisectedOut(
                subject=subject,
                batch_id=batch_id,
                cause=FAILING_GATE,
                stack_beneath=_ids("z"),
                stack_base_sha=_sha("stack base"),
                evidence_artifact_id="artifact:bisect:1",
                bisected_at=2.0,
            ),
            Withdrawn(
                subject=subject,
                reason=WithdrawalReason.APPROVAL_REVOKED,
                withdrawn_at=2.0,
            ),
        )
    }

    assert states == set(IntegrationRequestState)


def test_a_request_cannot_land_without_an_attempt() -> None:
    subject = _subject("a", enqueued_at=1.0)
    landed = Integrated(
        subject=subject,
        batch_id=IntegrationBatchId("batch-1"),
        integration_commit_sha=_sha("integrated"),
        integrated_at=2.0,
    )

    with pytest.raises(ValueError, match="QUEUED -> INTEGRATED"):
        require_integration_transition(Queued(subject=subject), landed)


def test_an_attempt_may_end_in_any_of_its_four_ways() -> None:
    subject = _subject("a", enqueued_at=1.0)
    in_flight = InFlight(
        subject=subject,
        batch_id=IntegrationBatchId("batch-1"),
        attempt_id=IntegrationAttemptId("attempt-1"),
    )

    for target in (
        Queued(subject=subject),
        Integrated(
            subject=subject,
            batch_id=IntegrationBatchId("batch-1"),
            integration_commit_sha=_sha("integrated"),
            integrated_at=2.0,
        ),
        Withdrawn(
            subject=subject,
            reason=WithdrawalReason.APPROVAL_REVOKED,
            withdrawn_at=2.0,
        ),
    ):
        require_integration_transition(in_flight, target)


def test_a_landed_request_is_terminal() -> None:
    subject = _subject("a", enqueued_at=1.0)
    landed = Integrated(
        subject=subject,
        batch_id=IntegrationBatchId("batch-1"),
        integration_commit_sha=_sha("integrated"),
        integrated_at=2.0,
    )

    with pytest.raises(ValueError, match="INTEGRATED -> QUEUED"):
        require_integration_transition(landed, Queued(subject=subject))


def test_a_transition_may_not_change_what_is_being_landed() -> None:
    # The hole this closes is the same one merging by branch name would open,
    # arriving through the ledger instead of through git.
    original = _subject("a", enqueued_at=1.0)
    rewritten = replace(original, commit_sha=_sha("something else"))

    with pytest.raises(ValueError, match="may not change its subject"):
        require_integration_transition(
            Queued(subject=original),
            InFlight(
                subject=rewritten,
                batch_id=IntegrationBatchId("batch-1"),
                attempt_id=IntegrationAttemptId("attempt-1"),
            ),
        )


def test_an_abbreviated_sha_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="full lowercase object name"):
        IntegrationSubject(
            request_id=IntegrationRequestId("a"),
            target_project_id=PROJECT,
            branch_name="agent/pow-wow-a-9f2c",
            base_head_sha=_sha("base"),
            commit_sha="deadbee",
            approval_id="approval-a",
            intent_id="intent-a",
            pow_wow_id="pow-wow-a",
            milestone_key=None,
            changed_files=(),
            enqueued_at=1.0,
        )


def test_a_request_to_land_its_own_base_is_refused() -> None:
    with pytest.raises(ValueError, match="nothing to integrate"):
        IntegrationSubject(
            request_id=IntegrationRequestId("a"),
            target_project_id=PROJECT,
            branch_name="agent/pow-wow-a-9f2c",
            base_head_sha=_sha("base"),
            commit_sha=_sha("base"),
            approval_id="approval-a",
            intent_id="intent-a",
            pow_wow_id="pow-wow-a",
            milestone_key=None,
            changed_files=(),
            enqueued_at=1.0,
        )
