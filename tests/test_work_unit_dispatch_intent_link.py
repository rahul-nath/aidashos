# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The durable link between a milestone attempt and its dispatch intent.

The scenarios in ``features/work_unit_dispatch_intent_link.feature`` cover the
edge cases. The unit tests below take one decision variable each along the same
path rather than their cross product: whether the write goes through the real
repository or an injected recorder, first intent versus retry, replay versus
fresh, whether the milestone row moved underneath, and which downstream reader
consults the column.

These deliberately go through ``repo.record_fact`` and re-read the row. The
existing coverage injected a ``fact_recorder`` and asserted the fact *object*
was constructed, which is exactly how a fact that was emitted, accepted, and
then dropped on the floor passed review.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when
from work_unit_support import compile_acceptance_doc

from local_first_agent_os.coordination.store import tx
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.events import (
    DispatchIntentCreated,
    MilestoneTransition,
    WorkUnitEventType,
)
from local_first_agent_os.work_units.lifecycle import (
    LifecyclePhase,
    MilestoneExecutionStatus,
)

scenarios("features/work_unit_dispatch_intent_link.feature")


FIRST_MILESTONE = "a"


def _running_work_unit(attempt: int = 1) -> str:
    """A started WorkUnit whose first milestone is RUNNING on ``attempt``.

    This is the state the live path is in when `DispatchIntentCreated` arrives:
    `root_workflow` records RUNNING and only then submits the intent.
    """

    compiled = compile_acceptance_doc()
    assert compiled.compiled_plan_revision_id is not None
    started = repo.start_work_unit(compiled.compiled_plan_revision_id, title="link test")
    work_unit_id = started.work_unit.work_unit_id
    for status in (MilestoneExecutionStatus.READY, MilestoneExecutionStatus.RUNNING):
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase.PLAN,
                milestone_key=FIRST_MILESTONE,
                status=status,
                attempt=attempt,
            ),
        )
    return work_unit_id


def _create_intent(work_unit_id: str, intent_id: str, *, attempt: int = 1) -> repo.FactOutcome:
    return repo.record_fact(
        work_unit_id,
        DispatchIntentCreated(
            phase=LifecyclePhase.PLAN,
            milestone_key=FIRST_MILESTONE,
            attempt=attempt,
            dispatch_intent_id=intent_id,
            tier="junior",
            kind="advisory",
        ),
    )


def _first_milestone(work_unit_id: str) -> repo.MilestoneExecutionRow:
    executions = repo.list_milestone_executions(work_unit_id)
    return next(item for item in executions if item.stable_key == FIRST_MILESTONE)


# --- gherkin steps ------------------------------------------------------------


@pytest.fixture()
def world() -> dict[str, Any]:
    return {}


@given("a started WorkUnit whose first milestone is running")
def _started(world: dict[str, Any], work_unit_ledger: Path) -> None:
    world["work_unit_id"] = _running_work_unit()


@when(parsers.parse('the milestone creates dispatch intent "{intent_id}"'))
def _creates(world: dict[str, Any], intent_id: str) -> None:
    world["outcome"] = _create_intent(world["work_unit_id"], intent_id)


@when("the same creation is replayed")
def _replayed(world: dict[str, Any]) -> None:
    world["replay"] = _create_intent(world["work_unit_id"], "intent-alpha")


@when(parsers.parse('a second attempt creates dispatch intent "{intent_id}"'))
def _second_attempt(world: dict[str, Any], intent_id: str) -> None:
    _create_intent(world["work_unit_id"], intent_id, attempt=2)


@when("the milestone attempt is modified underneath the write")
def _modified_underneath(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _stale_the_loaded_version(monkeypatch)


@then(parsers.parse('the milestone attempt names "{intent_id}"'))
def _names(world: dict[str, Any], intent_id: str) -> None:
    assert _first_milestone(world["work_unit_id"]).dispatch_intent_id == intent_id


@then(parsers.parse('the event log also names "{intent_id}"'))
def _event_names(world: dict[str, Any], intent_id: str) -> None:
    events = repo.list_work_unit_events(world["work_unit_id"])
    created = [
        event for event in events if event.event_type is WorkUnitEventType.DISPATCH_INTENT_CREATED
    ]
    assert [event.payload["dispatch_intent_id"] for event in created] == [intent_id]


@then("exactly one dispatch intent event was recorded")
def _one_event(world: dict[str, Any]) -> None:
    assert world["replay"].applied is False
    events = repo.list_work_unit_events(world["work_unit_id"])
    assert (
        sum(1 for event in events if event.event_type is WorkUnitEventType.DISPATCH_INTENT_CREATED)
        == 1
    )


@then(parsers.parse('cancelling the WorkUnit asks to stop "{intent_id}"'))
def _cancellation_reaches(
    world: dict[str, Any], intent_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _cancel_and_record_intent_stops(world["work_unit_id"], monkeypatch)
    assert intent_id in asked


@then("recording the intent creation fails loudly")
def _fails_loudly(world: dict[str, Any]) -> None:
    with pytest.raises(repo.WorkUnitError, match="concurrent modification"):
        _create_intent(world["work_unit_id"], "intent-alpha")
    assert _first_milestone(world["work_unit_id"]).dispatch_intent_id is None


def _stale_the_loaded_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `record_fact` guard on a version the row no longer has."""

    import dataclasses

    original = repo._load_milestone_execution

    def _stale(*args: Any, **kwargs: Any) -> repo.MilestoneExecutionRow:
        row = original(*args, **kwargs)
        return dataclasses.replace(row, version=row.version + 1)

    monkeypatch.setattr(repo, "_load_milestone_execution", _stale)


def _cancel_and_record_intent_stops(
    work_unit_id: str, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """Run the real cascade, recording which intents it tried to stop."""

    from local_first_agent_os.work_units import cancellation

    asked: list[str] = []

    def _cancel(intent_id: str, *, reason: str = "") -> dict[str, Any]:
        asked.append(intent_id)
        return {"ok": True}

    monkeypatch.setattr(
        "local_first_agent_os.coordination.dispatch.cancel_dispatch_intent", _cancel
    )
    monkeypatch.setattr(cancellation, "live_execution_leases_for_intent", lambda _intent_id: ())
    monkeypatch.setattr(cancellation, "_stop_dbos_workflow", lambda _workflow_id: None)
    cancellation.run_cancellation_cascade(work_unit_id, reason="test")
    return asked


# --- unit tests: one per decision variable on the link path -------------------


# Variable 1: the write path - real repository versus an injected recorder.
def test_creating_an_intent_persists_the_link_on_the_milestone_attempt(
    work_unit_ledger: Path,
) -> None:
    """The defect, as an assertion.

    The API reported no dispatch intent for a milestone whose event log named
    one, because the fact appended an event and updated nothing.
    """

    work_unit_id = _running_work_unit()
    assert _first_milestone(work_unit_id).dispatch_intent_id is None

    _create_intent(work_unit_id, "intent-alpha")

    assert _first_milestone(work_unit_id).dispatch_intent_id == "intent-alpha"


def test_the_link_and_the_event_are_one_transaction(work_unit_ledger: Path) -> None:
    """Either both land or neither does.

    The event is the history and the column is the state; a crash that left one
    without the other would put the ledger back in the shape this fixes.
    """

    work_unit_id = _running_work_unit()
    _create_intent(work_unit_id, "intent-alpha")

    with tx() as c:
        row = dict(
            c.execute(
                "SELECT COUNT(*) AS n FROM work_unit_events WHERE work_unit_id=? AND event_type=?",
                (work_unit_id, WorkUnitEventType.DISPATCH_INTENT_CREATED.value),
            ).fetchone()
        )
    assert row["n"] == 1
    assert _first_milestone(work_unit_id).dispatch_intent_id == "intent-alpha"


# Variable 2: first intent versus a retry's intent.
def test_a_retry_intent_replaces_the_one_before_it(work_unit_ledger: Path) -> None:
    """The column names the live intent, not the first one ever created.

    `_update_milestone_execution` coalesces the same column, which is right for a
    status change that carries no intent. Here a second creation is a genuinely
    new intent - the intent's idempotency key includes the attempt - so keeping
    the older id would point cancellation at work that already ended.
    """

    work_unit_id = _running_work_unit()
    _create_intent(work_unit_id, "intent-alpha")
    _create_intent(work_unit_id, "intent-beta", attempt=2)

    assert _first_milestone(work_unit_id).dispatch_intent_id == "intent-beta"


def test_the_first_intent_survives_a_later_status_change(work_unit_ledger: Path) -> None:
    """A status transition that carries no intent must not clear the link.

    `_update_milestone_execution` coalesces the column for exactly this reason,
    and the two writers have to agree: the creation overwrites because it names a
    new intent, the transition preserves because it names none.
    """

    work_unit_id = _running_work_unit()
    _create_intent(work_unit_id, "intent-alpha")
    repo.record_fact(
        work_unit_id,
        MilestoneTransition(
            phase=LifecyclePhase.PLAN,
            milestone_key=FIRST_MILESTONE,
            status=MilestoneExecutionStatus.BLOCKED,
            attempt=1,
        ),
    )

    assert _first_milestone(work_unit_id).dispatch_intent_id == "intent-alpha"


# Variable 3: a fresh fact versus a replayed one.
def test_a_replayed_creation_is_not_applied_twice(work_unit_ledger: Path) -> None:
    work_unit_id = _running_work_unit()
    first = _create_intent(work_unit_id, "intent-alpha")
    second = _create_intent(work_unit_id, "intent-alpha")

    assert first.applied is True
    assert second.applied is False
    assert _first_milestone(work_unit_id).dispatch_intent_id == "intent-alpha"


def test_a_replay_does_not_bump_the_optimistic_version(work_unit_ledger: Path) -> None:
    """The idempotency short-circuit returns before any write.

    Which means a replayed creation cannot invalidate a version another writer is
    holding, and cannot double-count the row's history.
    """

    work_unit_id = _running_work_unit()
    _create_intent(work_unit_id, "intent-alpha")
    after_first = _first_milestone(work_unit_id).version
    _create_intent(work_unit_id, "intent-alpha")

    assert _first_milestone(work_unit_id).version == after_first


# Variable 4: whether the milestone row moved underneath the write.
def test_a_stale_milestone_version_refuses_rather_than_losing_the_link(
    work_unit_ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The link write joins the same optimistic-lock discipline as every other.

    A write path that silently no-ops is how the column came to be NULL in the
    first place, and a refusal is the only outcome an operator can act on. The
    staleness is injected into the row `record_fact` loaded rather than written
    around it, because `record_fact` re-reads the row inside its own transaction
    behind a `FOR UPDATE` on the parent - no second fact writer can get between
    the read and the write. The guard is there for a writer that does not take
    that lock, and this is what such a writer looks like from inside.
    """

    work_unit_id = _running_work_unit()
    _stale_the_loaded_version(monkeypatch)

    with pytest.raises(repo.WorkUnitError, match="concurrent modification"):
        _create_intent(work_unit_id, "intent-alpha")


def test_the_link_write_bumps_the_version_it_guarded_on(work_unit_ledger: Path) -> None:
    work_unit_id = _running_work_unit()
    before = _first_milestone(work_unit_id).version

    _create_intent(work_unit_id, "intent-alpha")

    assert _first_milestone(work_unit_id).version == before + 1


# Variable 5: which downstream reader consults the column.
def test_cancellation_can_now_reach_the_intent_the_milestone_started(
    work_unit_ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The consequence that is not cosmetic.

    `run_cancellation_cascade` derives the intents to refuse from this column.
    While it was NULL a cancellation stopped DBOS workflows and left the agent
    process running, which is the failure `cancellation.py` says it exists to fix.
    """

    work_unit_id = _running_work_unit()
    _create_intent(work_unit_id, "intent-alpha")

    assert _cancel_and_record_intent_stops(work_unit_id, monkeypatch) == ["intent-alpha"]


def test_cancellation_asks_for_the_leases_that_intent_started(
    work_unit_ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease set is derived from the same column, one indirection further.

    The lease is the only handle on the agent process, so a NULL column lost the
    process as well as the intent.
    """

    from local_first_agent_os.work_units import cancellation

    work_unit_id = _running_work_unit()
    _create_intent(work_unit_id, "intent-alpha")

    asked_for_leases: list[str] = []
    monkeypatch.setattr(
        "local_first_agent_os.coordination.dispatch.cancel_dispatch_intent",
        lambda intent_id, *, reason="": {"ok": True},
    )
    monkeypatch.setattr(
        cancellation,
        "live_execution_leases_for_intent",
        lambda intent_id: asked_for_leases.append(intent_id) or (),
    )
    monkeypatch.setattr(cancellation, "_stop_dbos_workflow", lambda _workflow_id: None)
    cancellation.run_cancellation_cascade(work_unit_id, reason="test")

    assert asked_for_leases == ["intent-alpha"]


def test_a_terminal_milestone_is_not_asked_to_stop(
    work_unit_ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The link is durable; the cascade still only stops what is still live."""

    work_unit_id = _running_work_unit()
    _create_intent(work_unit_id, "intent-alpha")
    repo.record_fact(
        work_unit_id,
        MilestoneTransition(
            phase=LifecyclePhase.PLAN,
            milestone_key=FIRST_MILESTONE,
            status=MilestoneExecutionStatus.CANCELLED,
            attempt=1,
        ),
    )

    assert _cancel_and_record_intent_stops(work_unit_id, monkeypatch) == []
    assert _first_milestone(work_unit_id).dispatch_intent_id == "intent-alpha"
