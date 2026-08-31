# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""How many times a blocked milestone may be tried again.

The scenarios in ``features/work_unit_retry_budget.feature`` cover the edge
cases. The unit tests below take one decision variable each along the same path
rather than their cross product: the failure class, the attempt against the
budget, the operator override and its two answers, whether the class survived
the write, which status the row is in, and how many times a resume is asked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from pytest_bdd import given, parsers, scenarios, then, when
from work_unit_support import compile_acceptance_doc

from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units import service
from local_first_agent_os.work_units.events import (
    DecisionKindMismatch,
    DecisionRequestKind,
    MilestoneTransition,
    OperatorDecision,
    RetryOverridden,
    RetryRefusalUpheld,
    decision_outcome,
)
from local_first_agent_os.work_units.executors import _BOUNDED_RETRY
from local_first_agent_os.work_units.lifecycle import (
    FailureClass,
    LifecyclePhase,
    MilestoneExecutionStatus,
)
from local_first_agent_os.work_units.retry import (
    ChargedFailure,
    ChargedFailureBudget,
    OperatorOnly,
    RetryGrounds,
    RetryPermitted,
    RetryRefused,
    UnchargedFailure,
    attempt_charge,
    decide_retry,
    retry_policy_exhausted,
    retry_policy_from_legacy_max_attempts,
)
from local_first_agent_os.work_units.root_workflow import EnqueueDelivery, WorkUnitEngine

# DURABLE, not INLINE. The decision under test is the one the resume makes about
# BLOCKED milestones before it hands the continuation on; INLINE would then drive
# the whole lifecycle in-process, terminate the WorkUnit, and make the second and
# third resume in these scenarios raise instead of being refused.

scenarios("features/work_unit_retry_budget.feature")


FIRST_MILESTONE = "a"


# The attempt on which the declared budget is spent, read from the declaration
# rather than spelled again. The prose pins below (the feature file's
# "permits N charged failures" and the route-contract assertions) state the
# number on purpose; these mechanics only need "one more than permitted", and
# a literal here is what made raising the budget a five-file edit.
EXHAUSTED_AT = _BOUNDED_RETRY.max_charged_failures


def _blocked_work_unit(
    *,
    attempt: int,
    failure_class: FailureClass | None,
    failure_code: str,
) -> str:
    """A WorkUnit whose first milestone is BLOCKED on ``attempt``.

    Driven through `record_fact` rather than written directly, so the transition
    the lifecycle permits is the one under test.
    """

    return _work_unit_with_failure_history(
        tuple(failure_class for _ in range(attempt)), failure_code=failure_code
    )


def _work_unit_with_failure_history(
    failure_classes: tuple[FailureClass | None, ...], *, failure_code: str
) -> str:
    compiled = compile_acceptance_doc(design_doc_id="retry_budget")
    assert compiled.compiled_plan_revision_id is not None
    started = repo.start_work_unit(compiled.compiled_plan_revision_id, title="retry budget")
    work_unit_id = started.work_unit.work_unit_id
    for index, failure_class in enumerate(failure_classes, start=1):
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase.PLAN,
                milestone_key=FIRST_MILESTONE,
                status=MilestoneExecutionStatus.READY,
                attempt=index,
            ),
        )
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase.PLAN,
                milestone_key=FIRST_MILESTONE,
                status=MilestoneExecutionStatus.RUNNING,
                attempt=index,
            ),
        )
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase.PLAN,
                milestone_key=FIRST_MILESTONE,
                status=MilestoneExecutionStatus.BLOCKED,
                attempt=index,
                failure_code=failure_code,
                failure_class=failure_class,
            ),
        )
    return work_unit_id


def _milestone(work_unit_id: str) -> repo.MilestoneExecutionRow:
    return next(
        item
        for item in repo.list_milestone_executions(work_unit_id)
        if item.stable_key == FIRST_MILESTONE
    )


def _retry_policy(work_unit_id: str):
    unit = repo.get_work_unit(work_unit_id)
    plan = repo.get_compiled_plan_revision(unit.compiled_plan_revision_id).plan
    return plan.milestone(FIRST_MILESTONE).failure_policy.retry_policy


# --- gherkin steps ------------------------------------------------------------


@pytest.fixture()
def world() -> dict[str, Any]:
    return {}


@given("a WorkUnit whose first milestone permits 5 charged failures")
def _permits_five(world: dict[str, Any], work_unit_ledger: Path) -> None:
    world["expected_budget"] = 5


@given(
    parsers.parse("the milestone is blocked on execution {attempt:d} after a correctable failure")
)
def _blocked_correctable(world: dict[str, Any], attempt: int) -> None:
    world["work_unit_id"] = _blocked_work_unit(
        attempt=attempt,
        failure_class=FailureClass.CORRECTABLE,
        failure_code="the agent could not finish",
    )
    policy = _retry_policy(world["work_unit_id"])
    assert isinstance(policy, ChargedFailureBudget)
    assert policy.max_charged_failures == world["expected_budget"]


@given(
    parsers.parse(
        "the milestone is blocked on execution {attempt:d} waiting for an operator decision"
    )
)
def _blocked_no_fault(world: dict[str, Any], attempt: int) -> None:
    # The real no-fault writer passes no class at all, which is what makes the
    # column's absence meaningful rather than merely unset.
    world["work_unit_id"] = _blocked_work_unit(
        attempt=attempt,
        failure_class=None,
        failure_code="operator_decision_pending",
    )


@given("the WorkUnit was resumed and refused")
def _resumed_and_refused(world: dict[str, Any]) -> None:
    world["first_resume"] = service.resume_work_unit(
        world["work_unit_id"], delivery=EnqueueDelivery.DURABLE
    )
    assert world["first_resume"]["exhausted"]


@when("the WorkUnit is resumed")
def _resumed(world: dict[str, Any]) -> None:
    world["resume"] = service.resume_work_unit(
        world["work_unit_id"], delivery=EnqueueDelivery.DURABLE
    )


@when("the WorkUnit is resumed again")
def _resumed_again(world: dict[str, Any]) -> None:
    world["resume"] = service.resume_work_unit(
        world["work_unit_id"], delivery=EnqueueDelivery.DURABLE
    )


@when(parsers.parse("the WorkUnit is resumed {count:d} times"))
def _resumed_n_times(world: dict[str, Any], count: int) -> None:
    for _ in range(count):
        world["resume"] = service.resume_work_unit(
            world["work_unit_id"], delivery=EnqueueDelivery.DURABLE
        )


@when("the operator approves the override")
def _approves(world: dict[str, Any]) -> None:
    _answer_override(world["work_unit_id"], "APPROVED")


@when("the operator denies the override")
def _denies(world: dict[str, Any]) -> None:
    _answer_override(world["work_unit_id"], "DENIED")


def _answer_override(work_unit_id: str, decision: str) -> None:
    request_id = service.retry_override_request_id(work_unit_id, FIRST_MILESTONE)
    service.submit_work_unit_decision(
        work_unit_id, request_id, decision, f"idem-{request_id}-{decision}"
    )


@then(parsers.parse("the milestone is ready on execution {attempt:d}"))
def _ready_on(world: dict[str, Any], attempt: int) -> None:
    milestone = _milestone(world["work_unit_id"])
    assert milestone.status is MilestoneExecutionStatus.READY
    assert milestone.attempt == attempt


@then("nothing is reported as exhausted")
def _nothing_exhausted(world: dict[str, Any]) -> None:
    assert world["resume"]["exhausted"] == ()


@then(parsers.parse("the milestone is still blocked on execution {attempt:d}"))
def _still_blocked_on(world: dict[str, Any], attempt: int) -> None:
    milestone = _milestone(world["work_unit_id"])
    assert milestone.status is MilestoneExecutionStatus.BLOCKED
    assert milestone.attempt == attempt


@then("the resume reports it as exhausted")
def _reports_exhausted(world: dict[str, Any]) -> None:
    exhausted = world["resume"]["exhausted"]
    assert [item["milestone_key"] for item in exhausted] == [FIRST_MILESTONE]
    assert exhausted[0]["charged_failures"] == world["expected_budget"]
    assert exhausted[0]["retry_policy"] == {
        "kind": "charged_failure_budget",
        "max_charged_failures": world["expected_budget"],
    }


@then("an operator override decision is waiting")
def _override_waiting(world: dict[str, Any]) -> None:
    pending = service.pending_operator_decisions(world["work_unit_id"])
    request_id = service.retry_override_request_id(world["work_unit_id"], FIRST_MILESTONE)
    assert request_id in {item["request_id"] for item in pending}


# --- unit tests: one per decision variable on the retry path ------------------


# Variable 1: the failure class (six values, two answers).
@pytest.mark.parametrize(
    "failure_class",
    [FailureClass.CORRECTABLE, FailureClass.REQUIRES_REPLAN],
)
def test_a_class_that_reaches_blocked_spends_an_attempt(failure_class: FailureClass) -> None:
    assert isinstance(attempt_charge(failure_class), ChargedFailure)


@pytest.mark.parametrize(
    "failure_class",
    [
        FailureClass.REQUIRES_OPERATOR,
        FailureClass.POLICY_VIOLATION,
        FailureClass.NONRECOVERABLE,
    ],
)
def test_a_class_that_parks_or_fails_spends_no_attempt(failure_class: FailureClass) -> None:
    assert isinstance(attempt_charge(failure_class), UnchargedFailure)


def test_a_provider_that_died_in_flight_spends_no_attempt() -> None:
    """`TRANSIENT` reaches BLOCKED like the others and is charged unlike them.

    It is the one class where the milestone's work was never judged: the request
    died in flight, so there is no attempt to bill. Charging it is how three
    infrastructure failures in a row exhausted a milestone whose code had not
    been read once.
    """

    assert isinstance(attempt_charge(FailureClass.TRANSIENT), UnchargedFailure)


def test_only_transient_reaches_a_retry_decision_uncharged() -> None:
    """The uncharged classes are safe because most of them never get here.

    `attempt_charge` says REQUIRES_OPERATOR, POLICY_VIOLATION and NONRECOVERABLE
    spend nothing, and read alone that would mean a milestone carrying one could
    be resumed without limit. It cannot: `_status_for_failure` never routes them
    to BLOCKED, and `decide_retry` refuses anything else. The two functions live
    in different modules, so nothing but this test holds them together - routing
    one of them to BLOCKED later would silently make its retries unbounded.

    TRANSIENT is the deliberate exception, bounded by resume being operator-driven
    rather than by the budget.
    """

    blockable = {
        failure_class
        for failure_class in FailureClass
        if WorkUnitEngine._status_for_failure(cast(Any, None), failure_class)
        is MilestoneExecutionStatus.BLOCKED
    }
    uncharged = {
        failure_class
        for failure_class in blockable
        if isinstance(attempt_charge(failure_class), UnchargedFailure)
    }

    assert uncharged == {FailureClass.TRANSIENT}


def test_an_absent_class_is_read_conservatively() -> None:
    """A block with no recorded class is not charged.

    The budget exists to stop runaway retries. Under-charging costs one extra
    try; over-charging fails a milestone for work it never did, and a WorkUnit
    blocked before the column existed carries no class at all.
    """

    assert isinstance(attempt_charge(None), UnchargedFailure)


# Variable 2: the attempt against the budget.
def test_a_retry_inside_the_budget_is_permitted() -> None:
    decision = decide_retry(
        milestone_key="a",
        phase=LifecyclePhase.PLAN,
        status=MilestoneExecutionStatus.BLOCKED,
        execution_ordinal=1,
        charged_failures=1,
        failure_class=FailureClass.CORRECTABLE,
        retry_policy=ChargedFailureBudget(3),
    )
    assert isinstance(decision, RetryPermitted)
    assert decision.grounds is RetryGrounds.WITHIN_BUDGET
    assert decision.next_execution_ordinal == 2


def test_the_last_permitted_attempt_refuses_the_next_one() -> None:
    """The defect, as an assertion: attempt 3 of 3 must not become attempt 4."""

    decision = decide_retry(
        milestone_key="a",
        phase=LifecyclePhase.PLAN,
        status=MilestoneExecutionStatus.BLOCKED,
        execution_ordinal=3,
        charged_failures=3,
        failure_class=FailureClass.CORRECTABLE,
        retry_policy=ChargedFailureBudget(3),
    )
    assert isinstance(decision, RetryRefused)
    assert (decision.execution_ordinal, decision.charged_failures) == (3, 3)


def test_an_operator_only_policy_refuses_after_one_charged_failure() -> None:
    decision = decide_retry(
        milestone_key="e",
        phase=LifecyclePhase.REVIEW,
        status=MilestoneExecutionStatus.BLOCKED,
        execution_ordinal=1,
        charged_failures=1,
        failure_class=FailureClass.CORRECTABLE,
        retry_policy=OperatorOnly(),
    )
    assert isinstance(decision, RetryRefused)


def test_transient_executions_do_not_consume_a_later_correctable_failure_budget(
    work_unit_ledger: Path,
) -> None:
    work_unit_id = _work_unit_with_failure_history(
        (
            FailureClass.TRANSIENT,
            FailureClass.TRANSIENT,
            FailureClass.CORRECTABLE,
        ),
        failure_code="execution stopped",
    )

    result = service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.DURABLE)

    milestone = _milestone(work_unit_id)
    assert milestone.status is MilestoneExecutionStatus.READY
    # Four because three executions happened, not because of the budget: two of
    # them were transient and charged nothing, so the one correctable failure
    # leaves the budget almost untouched and the retry is permitted.
    assert milestone.attempt == 4
    assert result["exhausted"] == ()


# Variable 3: whether the block spent a try at all.
def test_a_no_fault_block_is_permitted_past_the_budget() -> None:
    """Re-entering a wait is not a retry.

    An approval gate permits a single attempt, so charging its wait for a human
    would fail every gate the moment it asked for one.
    """

    decision = decide_retry(
        milestone_key="e",
        phase=LifecyclePhase.REVIEW,
        status=MilestoneExecutionStatus.BLOCKED,
        execution_ordinal=9,
        charged_failures=0,
        failure_class=None,
        retry_policy=OperatorOnly(),
    )
    assert isinstance(decision, RetryPermitted)
    assert decision.grounds is RetryGrounds.NO_ATTEMPT_SPENT


# Variable 4: the operator override and its two answers.
def test_an_override_permits_a_retry_past_the_budget() -> None:
    decision = decide_retry(
        milestone_key="a",
        phase=LifecyclePhase.PLAN,
        status=MilestoneExecutionStatus.BLOCKED,
        execution_ordinal=3,
        charged_failures=3,
        failure_class=FailureClass.CORRECTABLE,
        retry_policy=ChargedFailureBudget(3),
        operator_override=True,
    )
    assert isinstance(decision, RetryPermitted)
    assert decision.grounds is RetryGrounds.OPERATOR_OVERRIDE


def test_an_approved_override_is_a_distinct_outcome_from_an_approval() -> None:
    """Sharing `Approved` would let a milestone approval read as permission to retry."""

    assert isinstance(
        decision_outcome(DecisionRequestKind.RETRY_BUDGET_OVERRIDE, OperatorDecision.APPROVED),
        RetryOverridden,
    )
    assert isinstance(
        decision_outcome(DecisionRequestKind.RETRY_BUDGET_OVERRIDE, OperatorDecision.DENIED),
        RetryRefusalUpheld,
    )


def test_an_override_cannot_be_answered_with_a_clarification() -> None:
    with pytest.raises(DecisionKindMismatch):
        decision_outcome(DecisionRequestKind.RETRY_BUDGET_OVERRIDE, OperatorDecision.ANSWERED)


def test_the_override_request_does_not_collide_with_the_approval_request(
    work_unit_ledger: Path,
) -> None:
    """The approval request is derived from work-unit plus milestone alone.

    Anything that did not also name the kind would land on the same row and one
    would silently answer the other.
    """

    from local_first_agent_os.ids import sha256_text

    work_unit_id = "wu-1"
    approval = f"wud_{sha256_text(f'{work_unit_id}:{FIRST_MILESTONE}')[:24]}"
    override = service.retry_override_request_id(work_unit_id, FIRST_MILESTONE)
    assert approval != override


# Variable 5: whether the class survived the write.
def test_the_failure_class_is_persisted_beside_the_code(work_unit_ledger: Path) -> None:
    """The datatype fix, as an assertion.

    Without the column, `operator_decision_pending` and a genuine executor
    failure are both strings, and the retry decision has to guess.
    """

    work_unit_id = _blocked_work_unit(
        attempt=1,
        failure_class=FailureClass.CORRECTABLE,
        failure_code="the agent could not finish",
    )
    milestone = _milestone(work_unit_id)
    assert milestone.failure_class is FailureClass.CORRECTABLE
    assert milestone.failure_code == "the agent could not finish"


def test_a_status_change_carrying_no_class_does_not_erase_the_one_on_the_row(
    work_unit_ledger: Path,
) -> None:
    """Coalesced like the code beside it."""

    work_unit_id = _blocked_work_unit(
        attempt=1,
        failure_class=FailureClass.CORRECTABLE,
        failure_code="the agent could not finish",
    )
    repo.record_fact(
        work_unit_id,
        MilestoneTransition(
            phase=LifecyclePhase.PLAN,
            milestone_key=FIRST_MILESTONE,
            status=MilestoneExecutionStatus.READY,
            attempt=2,
        ),
    )
    assert _milestone(work_unit_id).failure_class is FailureClass.CORRECTABLE


# Variable 6: how many times the resume is asked.
def test_repeated_resumes_do_not_buy_attempts(work_unit_ledger: Path) -> None:
    """The live shape: N resumes used to give N attempts.

    Nothing bounded the resume path at all, because the only budget check ran in
    the scheduler and only on FAILED.
    """

    work_unit_id = _blocked_work_unit(
        attempt=EXHAUSTED_AT,
        failure_class=FailureClass.CORRECTABLE,
        failure_code="the agent could not finish",
    )

    for _ in range(4):
        service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.DURABLE)

    milestone = _milestone(work_unit_id)
    assert milestone.status is MilestoneExecutionStatus.BLOCKED
    assert milestone.attempt == EXHAUSTED_AT


def test_a_refusal_opens_the_decision_that_could_lift_it(work_unit_ledger: Path) -> None:
    """A refusal with no named way out is a dead end an operator has to read code to escape."""

    work_unit_id = _blocked_work_unit(
        attempt=EXHAUSTED_AT,
        failure_class=FailureClass.CORRECTABLE,
        failure_code="the agent could not finish",
    )

    result = service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.DURABLE)

    request_id = result["exhausted"][0]["override_request_id"]
    request = repo.get_decision_request(request_id)
    assert request is not None
    assert request.request_kind is DecisionRequestKind.RETRY_BUDGET_OVERRIDE
    assert "supersedes this one" in (request.prompt or "")


def test_an_approved_override_lets_exactly_one_more_attempt_through(
    work_unit_ledger: Path,
) -> None:
    work_unit_id = _blocked_work_unit(
        attempt=EXHAUSTED_AT,
        failure_class=FailureClass.CORRECTABLE,
        failure_code="the agent could not finish",
    )
    service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.DURABLE)
    _answer_override(work_unit_id, "APPROVED")

    service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.DURABLE)

    milestone = _milestone(work_unit_id)
    assert milestone.status is MilestoneExecutionStatus.READY
    assert milestone.attempt == EXHAUSTED_AT + 1


def test_a_denied_override_leaves_the_budget_standing(work_unit_ledger: Path) -> None:
    work_unit_id = _blocked_work_unit(
        attempt=EXHAUSTED_AT,
        failure_class=FailureClass.CORRECTABLE,
        failure_code="the agent could not finish",
    )
    service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.DURABLE)
    _answer_override(work_unit_id, "DENIED")

    result = service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.DURABLE)

    milestone = _milestone(work_unit_id)
    assert milestone.status is MilestoneExecutionStatus.BLOCKED
    assert milestone.attempt == EXHAUSTED_AT
    assert result["exhausted"][0]["milestone_key"] == FIRST_MILESTONE


def test_the_resume_payload_still_satisfies_the_route_contract(
    work_unit_ledger: Path,
) -> None:
    """`WorkUnitResumeResult` forbids extra keys, and `exhausted` is a new one.

    This exact regression has happened twice on this route: a field added to the
    service payload and not declared here turns the resume into a 500 that means
    "it worked, I could not tell you".
    """

    from local_first_agent_os.work_units.projection import (
        ChargedFailureBudgetView,
        WorkUnitResumeResult,
    )

    work_unit_id = _blocked_work_unit(
        attempt=5,
        failure_class=FailureClass.CORRECTABLE,
        failure_code="the agent could not finish",
    )

    payload = service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.DURABLE)

    result = WorkUnitResumeResult.model_validate(payload)
    assert result.exhausted[0].milestone_key == FIRST_MILESTONE
    assert result.exhausted[0].charged_failures == 5
    retry_policy = result.exhausted[0].retry_policy
    assert isinstance(retry_policy, ChargedFailureBudgetView)
    assert retry_policy.max_charged_failures == 5


def test_the_decision_refuses_a_milestone_that_is_not_blocked() -> None:
    """The precondition is stated in the type's usage, and checked at the boundary."""

    with pytest.raises(AssertionError, match="blocked milestone"):
        decide_retry(
            milestone_key="a",
            phase=LifecyclePhase.PLAN,
            status=MilestoneExecutionStatus.RUNNING,
            execution_ordinal=1,
            charged_failures=1,
            failure_class=FailureClass.CORRECTABLE,
            retry_policy=ChargedFailureBudget(3),
        )


# The refusal text is durable operator-facing state, not a log line: `_refuse_retry`
# and the scheduler both write `describe()` into the ledger's `failure_summary`.
def test_the_refusal_names_both_ways_out_whichever_policy_refused() -> None:
    """A refusal that names only the override loses the plan-revision escape route.

    The two arms differ in the arithmetic they report, and that is the only thing
    they are allowed to differ in. An operator reading `failure_summary` off a
    FAILED row has no other place to learn that superseding the plan revision
    also lifts the refusal, so both arms have to carry it.
    """

    budget = RetryRefused(
        milestone_key="a",
        phase=LifecyclePhase.PLAN,
        execution_ordinal=4,
        charged_failures=3,
        retry_policy=ChargedFailureBudget(3),
    ).describe()
    operator_only = RetryRefused(
        milestone_key="a",
        phase=LifecyclePhase.PLAN,
        execution_ordinal=2,
        charged_failures=1,
        retry_policy=OperatorOnly(),
    ).describe()

    for message in (budget, operator_only):
        assert "milestone a" in message
        assert "new plan revision that supersedes this one" in message
        assert "explicit operator override" in message

    assert "3 permitted charged failure(s)" in budget
    assert "operator-only retry policy" in operator_only


# A budget is a count of charged failures, so the illegal values are the ones that
# would make the count meaningless rather than merely unusual.
def test_a_budget_that_permits_no_charged_failure_is_refused_at_construction() -> None:
    """`OperatorOnly` is how "no charged failure is tolerated" is spelled.

    Letting `ChargedFailureBudget(0)` mean the same thing would give the sum type
    two spellings for one state, which is the ambiguity the type exists to remove.
    """

    with pytest.raises(ValueError, match="max_charged_failures must be positive"):
        ChargedFailureBudget(0)


def test_a_legacy_plan_cannot_name_a_budget_below_one_attempt() -> None:
    """v3/v4 `max_attempts` counted attempts, and zero attempts was never legal."""

    with pytest.raises(ValueError, match="legacy max_attempts must be positive"):
        retry_policy_from_legacy_max_attempts(0)


def test_a_negative_charged_failure_count_is_refused_rather_than_read_as_room() -> None:
    """`charged_failures` is derived by counting history, so negative means a bug.

    Comparing it against the budget would silently report "not exhausted", which
    hands a milestone an unbounded retry on the strength of a miscount.
    """

    with pytest.raises(ValueError, match="charged_failures cannot be negative"):
        retry_policy_exhausted(ChargedFailureBudget(3), charged_failures=-1)


def test_the_decision_refuses_an_execution_ordinal_below_the_first_execution() -> None:
    """The ordinal is the idempotency identity, and it is 1-based.

    A zero would make `next_execution_ordinal` collide with the first real
    execution, so a retry would reuse an ordinal the ledger already has.
    """

    with pytest.raises(ValueError, match="execution_ordinal must be positive"):
        decide_retry(
            milestone_key="a",
            phase=LifecyclePhase.PLAN,
            status=MilestoneExecutionStatus.BLOCKED,
            execution_ordinal=0,
            charged_failures=0,
            failure_class=FailureClass.CORRECTABLE,
            retry_policy=ChargedFailureBudget(3),
        )
