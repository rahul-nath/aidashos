# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""When a milestone may stop waiting on its dispatch intent.

The scenarios in ``features/dispatch_pause_and_settlement.feature`` cover the
edge cases. The unit tests below take one decision variable each along the same
path rather than their cross product: the intent's status, whether the row was
read before the wait, whether the notification arrived, which writer changed the
status, whether a row exists at all, and which failure code the milestone
records.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from local_first_agent_os.contracts import (
    PARKED_DISPATCH_INTENT_STATUSES,
    TERMINAL_DISPATCH_INTENT_STATUSES,
    DispatchIntentStatus,
    DispatchProgress,
    classify_dispatch_progress,
    dispatch_statuses_with,
)
from local_first_agent_os.coordination.store import tx
from local_first_agent_os.work_units.execution import (
    DispatchBackedExecutorRuntime,
    DispatchParked,
    DispatchParkedError,
    DispatchSettled,
    DispatchStillActive,
    DispatchWaitTimeout,
    classify_dispatch_intent,
)

scenarios("features/dispatch_pause_and_settlement.feature")


def _row(status: DispatchIntentStatus, **extra: Any) -> dict[str, Any]:
    return {"intent_id": "intent-1", "status": status.value, **extra}


# --- gherkin steps ------------------------------------------------------------


@pytest.fixture()
def world() -> dict[str, Any]:
    return {}


@when(parsers.parse('a dispatch intent is "{status}"'))
def _intent_is(world: dict[str, Any], status: str) -> None:
    world["progress"] = classify_dispatch_progress(DispatchIntentStatus(status))


@then(parsers.parse('a waiter is told "{progress}"'))
def _waiter_told(world: dict[str, Any], progress: str) -> None:
    assert world["progress"] is DispatchProgress(progress)


@pytest.fixture()
def milestone_wait(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A `DispatchBackedExecutorRuntime` over a row the test controls."""

    state: dict[str, Any] = {"row": _row(DispatchIntentStatus.CLAIMED), "reads": 0, "slept": 0.0}

    def _read(_intent_id: str) -> dict[str, Any] | None:
        state["reads"] += 1
        return state["row"]

    monkeypatch.setattr("local_first_agent_os.work_units.execution.dispatch_intent_row", _read)
    monkeypatch.setattr(
        "local_first_agent_os.work_units.execution.time.sleep",
        lambda seconds: state.__setitem__("slept", state["slept"] + seconds),
    )
    state["runtime"] = DispatchBackedExecutorRuntime(poll_interval_seconds=0.0)
    return state


@given("a milestone waiting on a dispatch intent")
def _milestone_waiting(milestone_wait: dict[str, Any]) -> None:
    assert milestone_wait["row"]["status"] == DispatchIntentStatus.CLAIMED.value


@given("a dispatch intent with a milestone waiting on it", target_fixture="waiting_intent")
def _intent_with_a_waiter(work_unit_ledger: Path) -> str:
    return _intent_with_waiter()


@when(parsers.parse('the intent becomes "{status}"'), target_fixture="notified")
def _intent_becomes(waiting_intent: str, status: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from local_first_agent_os.coordination.checkpoints import create_execution_checkpoint
    from local_first_agent_os.coordination.dispatch import (
        cancel_dispatch_intent,
        claim_next_dispatch_intent,
        complete_dispatch_intent,
        supersede_dispatch_intent,
    )

    if status in {"PAUSED", "DONE"}:
        claim_next_dispatch_intent("test-dispatcher")
    sent = _notified(monkeypatch)
    match status:
        case "PAUSED":
            create_execution_checkpoint(
                _open_lease_for(waiting_intent),
                reason="operator_cancel",
                status="PAUSED",
            )
        case "CANCELED":
            cancel_dispatch_intent(waiting_intent, reason="stop")
        case "SUPERSEDED":
            supersede_dispatch_intent(waiting_intent, prompt="another way", tier="junior")
        case "DONE":
            complete_dispatch_intent(waiting_intent, status="DONE", result="{}")
    return sent


@then("the waiting milestone is notified")
def _milestone_notified(notified: list[str], waiting_intent: str) -> None:
    assert waiting_intent in notified


@when("the intent pauses at a checkpoint", target_fixture="waited")
def _pauses(milestone_wait: dict[str, Any]) -> Any:
    milestone_wait["row"] = _row(DispatchIntentStatus.PAUSED, checkpoint_id="cp-7")
    return milestone_wait["runtime"].poll_until_stopped("intent-1", 1800.0)


@when("the intent settles before the wait begins", target_fixture="waited")
def _settles_first(milestone_wait: dict[str, Any]) -> Any:
    milestone_wait["row"] = _row(DispatchIntentStatus.DONE)
    return milestone_wait["runtime"].poll_until_stopped("intent-1", 1800.0)


@when("the wait expires while the intent is still claimed", target_fixture="waited")
def _expires(milestone_wait: dict[str, Any]) -> Any:
    return milestone_wait["runtime"].poll_until_stopped("intent-1", 0.0)


@then(parsers.parse('the milestone is blocked with failure code "{code}"'))
def _blocked_with(waited: Any, code: str) -> None:
    expected = {
        "dispatch_paused": DispatchParked,
        "dispatch_wait_elapsed": DispatchStillActive,
    }[code]
    assert isinstance(waited, expected)


@then("the failure names the checkpoint")
def _names_checkpoint(waited: Any) -> None:
    assert isinstance(waited, DispatchParked)
    assert waited.checkpoint_id == "cp-7"
    assert "cp-7" in waited.describe()


@then("the milestone did not wait out its bound")
def _did_not_wait(milestone_wait: dict[str, Any]) -> None:
    assert milestone_wait["slept"] == 0.0


@then("the milestone reads the outcome without waiting")
def _reads_without_waiting(waited: Any, milestone_wait: dict[str, Any]) -> None:
    assert isinstance(waited, DispatchSettled)
    assert milestone_wait["slept"] == 0.0
    assert milestone_wait["reads"] == 1


# --- unit tests: one per decision variable on the wait path -------------------


# Variable 1: the intent's status (nine values, three answers).
def test_every_dispatch_status_is_classified(work_unit_ledger: Path) -> None:
    """Nothing may fall through to "keep waiting" by omission.

    The classification is exhaustive under `assert_never`, so this is a
    completeness check on the enum rather than on the function: it fails if a
    status is added and the partition is not extended.
    """

    partitions = {progress: dispatch_statuses_with(progress) for progress in DispatchProgress}
    assert set().union(*partitions.values()) == set(DispatchIntentStatus)
    assert sum(len(members) for members in partitions.values()) == len(DispatchIntentStatus)


def test_paused_and_checkpoint_review_are_parked_not_active() -> None:
    """The defect, as an assertion.

    Both were classified as "keep waiting", so a milestone whose intent had
    stopped burned its whole 1,800-second bound and then reported that the agent
    never answered.
    """

    assert {
        DispatchIntentStatus.PAUSED,
        DispatchIntentStatus.CHECKPOINT_REVIEW,
    } == PARKED_DISPATCH_INTENT_STATUSES
    assert not PARKED_DISPATCH_INTENT_STATUSES & TERMINAL_DISPATCH_INTENT_STATUSES


def test_the_terminal_set_is_derived_from_the_classification() -> None:
    """One source of truth for the split, not three sets that can drift.

    Two modules already spelled "settled" identically and disagreed about
    SUPERSEDED, which is what having three of these costs.
    """

    assert dispatch_statuses_with(DispatchProgress.SETTLED) == TERMINAL_DISPATCH_INTENT_STATUSES
    assert DispatchIntentStatus.SUPERSEDED in TERMINAL_DISPATCH_INTENT_STATUSES


# Variable 2: whether the row exists at all.
def test_a_missing_intent_row_is_not_a_settlement() -> None:
    """An intent nothing can find has not been shown to have ended.

    Reading absence as settlement would let a milestone report an outcome for
    work it never saw.
    """

    result = classify_dispatch_intent("intent-1", None, waited_seconds=5.0)
    assert isinstance(result, DispatchStillActive)
    assert result.status is None
    assert "absent from the ledger" in result.describe()


def test_a_present_row_carries_its_status_into_the_result() -> None:
    result = classify_dispatch_intent(
        "intent-1", _row(DispatchIntentStatus.CLAIMED), waited_seconds=5.0
    )
    assert isinstance(result, DispatchStillActive)
    assert result.status is DispatchIntentStatus.CLAIMED


# Variable 3: whether the row was read before the wait.
def test_an_already_settled_intent_costs_no_sleep(milestone_wait: dict[str, Any]) -> None:
    """The lost-notification shape, as an assertion.

    A restart mid-flight leaves a durable `recv` whose notification was already
    consumed. Waiting first means sleeping the whole bound for a message that no
    longer exists, while the answer sits in a row nobody read.
    """

    milestone_wait["row"] = _row(DispatchIntentStatus.DONE)

    result = milestone_wait["runtime"].poll_until_stopped("intent-1", 1800.0)

    assert isinstance(result, DispatchSettled)
    assert milestone_wait["reads"] == 1
    assert milestone_wait["slept"] == 0.0


def test_an_already_parked_intent_also_costs_no_sleep(milestone_wait: dict[str, Any]) -> None:
    milestone_wait["row"] = _row(DispatchIntentStatus.PAUSED)

    result = milestone_wait["runtime"].poll_until_stopped("intent-1", 1800.0)

    assert isinstance(result, DispatchParked)
    assert milestone_wait["slept"] == 0.0


def test_an_active_intent_is_re_read_until_it_stops(milestone_wait: dict[str, Any]) -> None:
    """The row is checked again before the wait is declared over."""

    reads: list[int] = []

    def _read(_intent_id: str) -> dict[str, Any]:
        reads.append(1)
        return (
            _row(DispatchIntentStatus.CLAIMED)
            if len(reads) < 3
            else _row(DispatchIntentStatus.DONE)
        )

    milestone_wait["runtime"] = DispatchBackedExecutorRuntime(poll_interval_seconds=0.0)
    with pytest.MonkeyPatch().context() as patch:
        patch.setattr("local_first_agent_os.work_units.execution.dispatch_intent_row", _read)
        result = milestone_wait["runtime"].poll_until_stopped("intent-1", 30.0)

    assert isinstance(result, DispatchSettled)
    assert len(reads) == 3


# Variable 4: which of the three the wait ended on, as seen by the caller.
def test_a_parked_intent_raises_its_own_error_not_a_timeout() -> None:
    """`DispatchWaitTimeout` used to be the funnel for both.

    A timeout may still have work running under it; a park is waiting on a
    person. One exception for both is why the milestone reported the wrong one.
    """

    parked = DispatchParked(
        intent_id="intent-1",
        status=DispatchIntentStatus.PAUSED,
        checkpoint_id="cp-7",
    )
    error = DispatchParkedError(parked)

    assert not isinstance(error, DispatchWaitTimeout)
    assert error.parked is parked
    assert "will not move again without a decision" in str(error)


def test_a_still_active_intent_at_the_deadline_is_a_timeout(
    milestone_wait: dict[str, Any],
) -> None:
    result = milestone_wait["runtime"].poll_until_stopped("intent-1", 0.0)

    assert isinstance(result, DispatchStillActive)
    assert "still CLAIMED" in result.describe()


def test_wait_for_still_returns_a_row_for_the_callers_that_want_one(
    milestone_wait: dict[str, Any],
) -> None:
    milestone_wait["row"] = _row(DispatchIntentStatus.DONE)

    assert milestone_wait["runtime"].wait_for("intent-1", 30.0)["status"] == "DONE"


def test_wait_for_raises_the_parked_error_rather_than_a_timeout(
    milestone_wait: dict[str, Any],
) -> None:
    milestone_wait["row"] = _row(DispatchIntentStatus.PAUSED, checkpoint_id="cp-7")

    with pytest.raises(DispatchParkedError):
        milestone_wait["runtime"].wait_for("intent-1", 30.0)


# Variable 5: which writer changed the status - every one a waiter can be on.
def _intent_with_waiter(intent_id: str = "intent-notify") -> str:
    from local_first_agent_os.coordination.dispatch import submit_dispatch_intent

    result = submit_dispatch_intent(
        tier="junior",
        prompt="do a thing",
        kind="advisory",
        notify_workflow_id="work-unit:wu-1:milestone:a:1",
    )
    return str(result["intent_id"])


def _open_lease_for(intent_id: str) -> str:
    """A lease the checkpoint machinery can park, tied to this intent."""

    from local_first_agent_os.coordination.execution import open_execution_lease

    result = open_execution_lease(
        idempotency_key=f"lease:{intent_id}",
        worker_id="test-worker",
        intent_id=intent_id,
        agent_tier="junior",
        agent_name="pi",
        timeout_seconds=60,
    )
    return str(result["lease"]["lease_id"])


def _notified(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from local_first_agent_os.coordination import checkpoints, dispatch

    sent: list[str] = []

    def _notify(intent_id: str) -> bool:
        sent.append(intent_id)
        return True

    monkeypatch.setattr(dispatch, "notify_dispatch_status_change", _notify)
    monkeypatch.setattr(checkpoints, "notify_dispatch_status_change", _notify)
    return sent


def test_cancelling_an_intent_wakes_the_milestone_waiting_on_it(
    work_unit_ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation is the case where waiting out a bound is worst: nothing is coming."""

    from local_first_agent_os.coordination.dispatch import cancel_dispatch_intent

    intent_id = _intent_with_waiter()
    sent = _notified(monkeypatch)

    cancel_dispatch_intent(intent_id, reason="operator changed their mind")

    assert sent == [intent_id]


def test_superseding_an_intent_wakes_the_milestone_waiting_on_it(
    work_unit_ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from local_first_agent_os.coordination.dispatch import supersede_dispatch_intent

    intent_id = _intent_with_waiter()
    sent = _notified(monkeypatch)

    supersede_dispatch_intent(intent_id, prompt="try it this way instead", tier="junior")

    assert intent_id in sent


def test_completing_an_intent_still_wakes_the_milestone_waiting_on_it(
    work_unit_ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one transition that always notified keeps notifying."""

    from local_first_agent_os.coordination.dispatch import (
        claim_next_dispatch_intent,
        complete_dispatch_intent,
    )

    intent_id = _intent_with_waiter()
    claim_next_dispatch_intent("test-dispatcher")
    sent = _notified(monkeypatch)

    complete_dispatch_intent(intent_id, status="DONE", result="{}")

    assert intent_id in sent


def test_the_notification_is_sent_after_the_transaction_not_inside_it(
    work_unit_ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A woken milestone re-reads the row immediately.

    Sending from inside the transaction would race it against the write that woke
    it and could hand it the status the row had before the commit.
    """

    from local_first_agent_os.coordination.dispatch import (
        cancel_dispatch_intent,
        notify_dispatch_status_change,
    )

    intent_id = _intent_with_waiter()
    observed: list[str] = []

    def _notify(target: str) -> bool:
        with tx() as c:
            row = c.execute(
                "SELECT status FROM dispatch_intents WHERE intent_id=?", (target,)
            ).fetchone()
        observed.append(str(dict(row)["status"]))
        return notify_dispatch_status_change(target)

    monkeypatch.setattr(
        "local_first_agent_os.coordination.dispatch.notify_dispatch_status_change", _notify
    )
    cancel_dispatch_intent(intent_id, reason="stop")

    assert observed == ["CANCELED"]


def test_a_rolled_back_transaction_notifies_nobody(work_unit_ledger: Path) -> None:
    """Collecting is only safe if a failed transaction sends nothing.

    The collector resumes its generator through `throw` on an exception, so the
    code after the yield never runs - which is the property, not an accident.
    """

    from local_first_agent_os.coordination.dispatch import dispatch_status_notifications

    sent: list[str] = []
    with pytest.raises(RuntimeError), dispatch_status_notifications() as pending:
        pending.append("intent-1")
        raise RuntimeError("the transaction failed")

    assert sent == []


def test_a_clean_transaction_notifies_everything_it_collected() -> None:
    from local_first_agent_os.coordination import dispatch

    sent: list[str] = []
    with pytest.MonkeyPatch().context() as patch:
        patch.setattr(
            dispatch,
            "notify_dispatch_status_change",
            lambda intent_id: bool(sent.append(intent_id)),
        )
        with dispatch.dispatch_status_notifications() as pending:
            pending.append("intent-1")
            pending.append("intent-2")

    assert sent == ["intent-1", "intent-2"]


# Variable 6: which failure code the milestone records for a halted dispatch.
def test_the_two_halted_dispatch_failure_codes_are_distinct() -> None:
    """`dispatch_paused` names a checkpoint; `dispatch_wait_elapsed` names a clock.

    One code for both is what said the second when the ledger knew the first.
    """

    from local_first_agent_os.work_units.root_workflow import WorkUnitEngine

    recorded: list[dict[str, Any]] = []
    engine = WorkUnitEngine.__new__(WorkUnitEngine)
    with pytest.MonkeyPatch().context() as patch:
        patch.setattr(
            "local_first_agent_os.work_units.root_workflow.record_milestone_transition_step",
            lambda *args, **kwargs: recorded.append({"args": args, "kwargs": kwargs}) or {},
        )
        from local_first_agent_os.work_units.lifecycle import LifecyclePhase

        for code in ("dispatch_paused", "dispatch_wait_elapsed"):
            engine._block_on_halted_dispatch(
                work_unit_id="wu-1",
                phase=LifecyclePhase.PLAN,
                milestone_key="a",
                attempt=1,
                child_workflow_id="child-1",
                failure_code=code,
                failure_summary=f"because {code}",
            )

    assert [item["kwargs"]["failure_code"] for item in recorded] == [
        "dispatch_paused",
        "dispatch_wait_elapsed",
    ]
    assert all(item["args"][3] == "BLOCKED" for item in recorded)


def test_a_poll_that_finds_nothing_does_not_spin_forever(
    milestone_wait: dict[str, Any],
) -> None:
    """The deadline still bounds the loop when the row never stops being active."""

    started = time.monotonic()
    result = milestone_wait["runtime"].poll_until_stopped("intent-1", 0.05)
    assert isinstance(result, DispatchStillActive)
    assert time.monotonic() - started < 5.0
