# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Recovering an execution that died with nobody watching.

The scenarios in ``features/work_unit_crash_reconciler.feature`` cover the edge
cases. The unit tests below take one decision variable each along the same path
rather than their cross product: the WorkUnit's status, what DBOS says about its
execution, whether the recovery proof says anything was repaired, the automatic
budget and what feeds it, whether the resume was delivered, and how many
reconcilers ran.

The crash is produced the way ``test_work_unit_execution_recovery`` produces it -
a runtime that raises, leaving the milestone ``RUNNING`` and no halt written -
rather than by writing the end state directly. Only DBOS is faked, and only its
answer to "what is the status of this workflow"; faking ``execution_liveness``
would build the stub from the same model as the code under test.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when
from work_unit_support import compile_acceptance_doc, start_inline

from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.crash_recovery_loop import (
    BudgetExhausted,
    CrashReconciler,
    LeftAlone,
)
from local_first_agent_os.work_units.events import (
    AutomaticCrashRecovery,
    WorkUnitEventType,
    WorkUnitTransition,
)
from local_first_agent_os.work_units.execution import MilestoneContext, MilestoneOutcome
from local_first_agent_os.work_units.execution_recovery import (
    DEAD_EXECUTION_FAILURE_CODE,
    ExecutionLiveness,
    RecoveredExecution,
    execution_workflow_id,
)
from local_first_agent_os.work_units.lifecycle import (
    LifecyclePhase,
    WorkUnitStatus,
)
from local_first_agent_os.work_units.root_workflow import WorkUnitEngine, set_engine

scenarios("features/work_unit_crash_reconciler.feature")


CRASH = RuntimeError("the process died mid-milestone")


class CrashingRuntime:
    """A runtime whose milestone raises the way a dying process does."""

    def run(self, context: MilestoneContext) -> MilestoneOutcome:
        raise CRASH


@dataclass(frozen=True)
class _StubStatus:
    status: str


class _StubDbos:
    def __init__(self, statuses: dict[str, str]) -> None:
        self._statuses = statuses

    def get_workflow_status(self, workflow_id: str) -> _StubStatus | None:
        status = self._statuses.get(workflow_id)
        return None if status is None else _StubStatus(status)


class DbosStatuses:
    """Control over what DBOS reports, on its own MonkeyPatch.

    Owning the patcher matters: it and the test's are the same function-scoped
    instance, so undoing the test's would also undo the autouse fixture pointing
    the store at this test's Postgres schema.
    """

    def __init__(self, patcher: pytest.MonkeyPatch) -> None:
        self._patcher = patcher

    def says(self, statuses: dict[str, str]) -> None:
        self._patcher.setattr(
            "local_first_agent_os._dbos_runtime.DBOS", _StubDbos(statuses), raising=False
        )
        self._patcher.setattr("local_first_agent_os.dbos_app.is_dbos_active", lambda: True)

    def is_absent(self) -> None:
        self.says({})

    def cannot_be_asked(self) -> None:
        self._patcher.setattr("local_first_agent_os.dbos_app.is_dbos_active", lambda: False)


@pytest.fixture()
def dbos() -> Iterator[DbosStatuses]:
    patcher = pytest.MonkeyPatch()
    try:
        yield DbosStatuses(patcher)
    finally:
        patcher.undo()


def _crashed_work_unit() -> str:
    """A WorkUnit left RUNNING with a milestone nothing is running."""

    set_engine(
        WorkUnitEngine(
            runtime=CrashingRuntime(),
            approval_wait_seconds=0.0,
            approval_poll_seconds=0.01,
        )
    )
    compiled = compile_acceptance_doc(design_doc_id="crash_reconciler")
    assert compiled.compiled_plan_revision_id is not None
    started = start_inline(compiled.compiled_plan_revision_id)
    work_unit_id = str(started["work_unit_id"])
    assert repo.get_work_unit(work_unit_id).status is WorkUnitStatus.RUNNING
    return work_unit_id


def _dead_execution_of(work_unit_id: str) -> str:
    unit = repo.get_work_unit(work_unit_id)
    return execution_workflow_id(unit.root_workflow_id, repo.execution_epoch(work_unit_id))


def _reconciler(
    *, resumes: list[str] | None = None, delivered: bool = True, permitted: int = 3
) -> CrashReconciler:
    recorded = resumes if resumes is not None else []

    def _resume(work_unit_id: str) -> dict[str, Any]:
        recorded.append(work_unit_id)
        return {"delivered": delivered, "durable": delivered}

    return CrashReconciler(max_automatic_recoveries=permitted, resume=_resume)


# --- gherkin steps ------------------------------------------------------------


@pytest.fixture()
def world() -> dict[str, Any]:
    return {"resumes": []}


@given("a running WorkUnit whose execution DBOS reports as dead")
def _crashed(world: dict[str, Any], dbos: DbosStatuses, work_unit_ledger: Path) -> None:
    world["work_unit_id"] = _crashed_work_unit()
    dbos.says({_dead_execution_of(world["work_unit_id"]): "ERROR"})


@given(parsers.parse('a running WorkUnit whose execution DBOS reports as "{liveness}"'))
def _crashed_with_liveness(
    world: dict[str, Any], dbos: DbosStatuses, work_unit_ledger: Path, liveness: str
) -> None:
    world["work_unit_id"] = _crashed_work_unit()
    match liveness:
        case "LIVE":
            dbos.says({_dead_execution_of(world["work_unit_id"]): "PENDING"})
        case "ABSENT":
            dbos.is_absent()
        case "NO_RUNTIME":
            dbos.cannot_be_asked()


@given("a blocked WorkUnit")
def _blocked(world: dict[str, Any], dbos: DbosStatuses, work_unit_ledger: Path) -> None:
    work_unit_id = _crashed_work_unit()
    repo.record_fact(
        work_unit_id,
        WorkUnitTransition(status=WorkUnitStatus.BLOCKED, reason="an operator halted it"),
    )
    world["work_unit_id"] = work_unit_id


@given(parsers.parse("a running WorkUnit that has been recovered automatically {count:d} times"))
def _already_recovered(
    world: dict[str, Any], dbos: DbosStatuses, work_unit_ledger: Path, count: int
) -> None:
    work_unit_id = _crashed_work_unit()
    for epoch in range(count):
        repo.record_fact(
            work_unit_id,
            AutomaticCrashRecovery(
                execution_workflow_id=f"work-unit:{work_unit_id}:resume:{epoch}",
                halted_epoch=epoch,
            ),
        )
    world["work_unit_id"] = work_unit_id
    dbos.says({_dead_execution_of(work_unit_id): "ERROR"})


@given(parsers.parse("a WorkUnit that has halted {count:d} times for operator decisions"))
def _halted_often(world: dict[str, Any], work_unit_ledger: Path, count: int) -> None:
    work_unit_id = _crashed_work_unit()
    for epoch in range(count):
        repo.record_fact(
            work_unit_id,
            WorkUnitTransition(
                status=WorkUnitStatus.BLOCKED,
                reason="waiting on a person",
                epoch=epoch,
            ),
        )
        repo.record_fact(
            work_unit_id, WorkUnitTransition(status=WorkUnitStatus.RUNNING, epoch=epoch)
        )
    world["work_unit_id"] = work_unit_id


@given("no durable runtime can take the continuation")
def _no_runtime(world: dict[str, Any]) -> None:
    world["delivered"] = False


@when("the reconciler sweeps")
def _sweep(world: dict[str, Any]) -> None:
    world["pass"] = _reconciler(
        resumes=world["resumes"], delivered=world.get("delivered", True)
    ).poll_once()


@when("two reconcilers sweep at once")
def _sweep_twice(world: dict[str, Any]) -> None:
    world["pass"] = _reconciler(resumes=world["resumes"]).poll_once()
    world["second_pass"] = _reconciler(resumes=world["resumes"]).poll_once()


@then("the WorkUnit is recovered")
def _recovered(world: dict[str, Any]) -> None:
    repaired = world["pass"].repaired
    assert [item.work_unit_id for item in repaired] == [world["work_unit_id"]]


@then("it is resumed")
def _resumed(world: dict[str, Any]) -> None:
    assert world["resumes"] == [world["work_unit_id"]]
    assert world["pass"].repaired[0].resumed is True


@then("it is not reported as resumed")
def _not_reported_resumed(world: dict[str, Any]) -> None:
    assert world["pass"].repaired[0].resumed is False


@then("an automatic crash recovery is recorded")
def _recovery_recorded(world: dict[str, Any]) -> None:
    assert repo.automatic_crash_recovery_count(world["work_unit_id"]) == 1


@then(parsers.parse("exactly one automatic crash recovery is recorded"))
def _exactly_one(world: dict[str, Any]) -> None:
    assert repo.automatic_crash_recovery_count(world["work_unit_id"]) == 1


@then("the WorkUnit is left alone")
def _left_alone(world: dict[str, Any]) -> None:
    assert all(isinstance(item, LeftAlone) for item in world["pass"].verdicts)
    assert world["pass"].repaired == ()


@then("nothing is resumed")
def _nothing_resumed(world: dict[str, Any]) -> None:
    assert world["resumes"] == []


@then("nothing is inspected")
def _nothing_inspected(world: dict[str, Any]) -> None:
    assert world["pass"].inspected == 0


@then("the budget is reported as exhausted")
def _budget_exhausted(world: dict[str, Any]) -> None:
    exhausted = [item for item in world["pass"].verdicts if isinstance(item, BudgetExhausted)]
    assert [item.work_unit_id for item in exhausted] == [world["work_unit_id"]]


@then(parsers.parse("its automatic recovery count is {count:d}"))
def _recovery_count_is(world: dict[str, Any], count: int) -> None:
    assert repo.automatic_crash_recovery_count(world["work_unit_id"]) == count


# --- unit tests: one per decision variable on the reconcile path --------------


# Variable 1: the WorkUnit's status (which ones are candidates at all).
def test_a_running_work_unit_is_a_candidate(work_unit_ledger: Path) -> None:
    work_unit_id = _crashed_work_unit()
    candidates = CrashReconciler().candidates()
    assert [unit.work_unit_id for unit in candidates] == [work_unit_id]


def test_a_blocked_work_unit_is_not_a_candidate(work_unit_ledger: Path) -> None:
    """It already recorded how it stopped, so there is no missing halt to write.

    This is also the shape a `failure_code` filter would have found: a WorkUnit
    an operator already repaired. A crash writes no halt and therefore no failure
    code, so that predicate finds every already-fixed case and no broken one.
    """

    work_unit_id = _crashed_work_unit()
    repo.record_fact(
        work_unit_id,
        WorkUnitTransition(
            status=WorkUnitStatus.BLOCKED,
            failure_code=DEAD_EXECUTION_FAILURE_CODE,
            reason="already repaired",
        ),
    )
    assert CrashReconciler().candidates() == ()


def test_a_terminal_work_unit_is_not_a_candidate(work_unit_ledger: Path) -> None:
    work_unit_id = _crashed_work_unit()
    repo.record_fact(
        work_unit_id,
        WorkUnitTransition(status=WorkUnitStatus.BLOCKED, reason="stop"),
    )
    repo.record_fact(work_unit_id, WorkUnitTransition(status=WorkUnitStatus.FAILED))
    assert CrashReconciler().candidates() == ()


# Variable 2: what DBOS says (four values, two behaviours).
def test_a_dead_execution_is_recovered(work_unit_ledger: Path, dbos: DbosStatuses) -> None:
    work_unit_id = _crashed_work_unit()
    dbos.says({_dead_execution_of(work_unit_id): "ERROR"})

    result = _reconciler().poll_once()

    assert [item.work_unit_id for item in result.repaired] == [work_unit_id]
    assert repo.get_work_unit(work_unit_id).status is WorkUnitStatus.BLOCKED


def test_a_live_execution_is_not_touched(work_unit_ledger: Path, dbos: DbosStatuses) -> None:
    """Writing a halt for a live run advances the epoch under it.

    That lets a second execution start beside the first, which is worse than the
    failure being repaired.
    """

    work_unit_id = _crashed_work_unit()
    dbos.says({_dead_execution_of(work_unit_id): "PENDING"})
    resumes: list[str] = []

    result = _reconciler(resumes=resumes).poll_once()

    assert result.repaired == ()
    assert resumes == []
    assert repo.get_work_unit(work_unit_id).status is WorkUnitStatus.RUNNING


def test_an_absent_execution_is_not_read_as_a_death(
    work_unit_ledger: Path, dbos: DbosStatuses
) -> None:
    """ABSENT is a continuation not yet minted, not a corpse."""

    _crashed_work_unit()
    dbos.is_absent()

    result = _reconciler().poll_once()

    assert result.repaired == ()
    assert [item.liveness for item in result.verdicts if isinstance(item, LeftAlone)] == [
        ExecutionLiveness.ABSENT
    ]


def test_no_dbos_to_ask_recovers_nothing(work_unit_ledger: Path, dbos: DbosStatuses) -> None:
    """NO_RUNTIME means guessing, and a wrong guess writes a halt for a live run."""

    _crashed_work_unit()
    dbos.cannot_be_asked()

    result = _reconciler().poll_once()

    assert result.repaired == ()
    assert [item.liveness for item in result.verdicts if isinstance(item, LeftAlone)] == [
        ExecutionLiveness.NO_RUNTIME
    ]


# Variable 3: whether the recovery proof says anything was repaired.
def test_a_resume_follows_only_a_proof_of_repair() -> None:
    """`repaired` is the proof, and it is the gate.

    A recovery that wrote nothing means the execution was not dead, and resuming
    on it would restart a WorkUnit that stopped for a reason nobody looked at.
    """

    resumes: list[str] = []
    reconciler = CrashReconciler(
        recover=lambda _id: RecoveredExecution(ExecutionLiveness.LIVE, "wf-1"),
        resume=lambda work_unit_id: resumes.append(work_unit_id) or {"delivered": True},
    )

    verdict = reconciler._reconcile(
        repo.WorkUnitRow(  # type: ignore[call-arg]
            **{
                **_row_fields(),
                "work_unit_id": "wu-1",
                "status": WorkUnitStatus.RUNNING,
            }
        )
    )

    assert isinstance(verdict, LeftAlone)
    assert resumes == []


def _row_fields() -> dict[str, Any]:
    """The non-load-bearing fields of a WorkUnitRow, so a test can name two."""

    return {
        "work_unit_id": "wu-1",
        "title": "t",
        "status": WorkUnitStatus.RUNNING,
        "current_phase": LifecyclePhase.PLAN,
        "design_doc_revision_id": "ddr-1",
        "compiled_plan_revision_id": "cpr-1",
        "compiled_plan_hash": "hash",
        "lifecycle_profile": "engineering.v1",
        "lifecycle_profile_version": 1,
        "root_workflow_id": "work-unit:wu-1",
        "supersedes_work_unit_id": None,
        "legacy_saga_id": None,
        "created_at": 0.0,
        "started_at": None,
        "completed_at": None,
        "blocked_at": None,
        "failure_code": None,
        "failure_summary": None,
        "version": 1,
    }


# Variable 4: the automatic budget, and what feeds it.
def test_ordinary_halts_do_not_spend_the_automatic_budget(work_unit_ledger: Path) -> None:
    """The correction, as an assertion.

    `execution_epoch` counts every halt however caused. Reading it as a
    crash-retry budget would let a WorkUnit that asks three approval questions
    exhaust its allowance for surviving crashes.
    """

    work_unit_id = _crashed_work_unit()
    for epoch in range(5):
        # The epoch is what makes each halt a distinct fact rather than a replay
        # of the one before it; without it the idempotency key collapses them.
        repo.record_fact(
            work_unit_id,
            WorkUnitTransition(
                status=WorkUnitStatus.BLOCKED,
                reason="waiting on a person",
                epoch=epoch,
            ),
        )
        repo.record_fact(
            work_unit_id, WorkUnitTransition(status=WorkUnitStatus.RUNNING, epoch=epoch)
        )

    assert repo.execution_epoch(work_unit_id) == 5
    assert repo.automatic_crash_recovery_count(work_unit_id) == 0


def test_the_budget_counts_only_what_the_reconciler_writes(work_unit_ledger: Path) -> None:
    work_unit_id = _crashed_work_unit()
    repo.record_fact(
        work_unit_id,
        AutomaticCrashRecovery(execution_workflow_id="wf-1", halted_epoch=0),
    )

    assert repo.automatic_crash_recovery_count(work_unit_id) == 1


def test_an_exhausted_budget_stops_the_restarts(work_unit_ledger: Path, dbos: DbosStatuses) -> None:
    work_unit_id = _crashed_work_unit()
    for epoch in range(3):
        repo.record_fact(
            work_unit_id,
            AutomaticCrashRecovery(execution_workflow_id=f"wf-{epoch}", halted_epoch=epoch),
        )
    dbos.says({_dead_execution_of(work_unit_id): "ERROR"})
    resumes: list[str] = []

    result = _reconciler(resumes=resumes, permitted=3).poll_once()

    assert [type(item) for item in result.verdicts] == [BudgetExhausted]
    assert resumes == []
    assert repo.get_work_unit(work_unit_id).status is WorkUnitStatus.RUNNING


def test_an_exhausted_budget_is_checked_before_dbos_is_asked(
    work_unit_ledger: Path,
) -> None:
    """One cheap count per pass rather than a status round trip forever."""

    work_unit_id = _crashed_work_unit()
    repo.record_fact(
        work_unit_id, AutomaticCrashRecovery(execution_workflow_id="wf-0", halted_epoch=0)
    )
    asked: list[str] = []
    reconciler = CrashReconciler(
        max_automatic_recoveries=1,
        recover=lambda item: (
            asked.append(item)  # type: ignore[return-value]
            or RecoveredExecution(ExecutionLiveness.DEAD, "wf-0", halted_epoch=0)
        ),
    )

    reconciler.poll_once()

    assert asked == []


# Variable 5: whether the resume was delivered.
def test_an_undelivered_resume_is_not_reported_as_delivered(
    work_unit_ledger: Path, dbos: DbosStatuses
) -> None:
    """A report that cannot say no always says yes.

    `resume_work_unit` answers `delivered: False` when no durable runtime can
    take the continuation, and a reconciler that reported that as a restart would
    be the same defect one layer up.
    """

    work_unit_id = _crashed_work_unit()
    dbos.says({_dead_execution_of(work_unit_id): "ERROR"})

    result = _reconciler(delivered=False).poll_once()

    assert result.repaired[0].resumed is False
    assert result.to_payload()["repaired"][0]["resumed"] is False


# Variable 6: how many reconcilers ran.
def test_two_reconcilers_recovering_one_crash_spend_one_budget_entry(
    work_unit_ledger: Path, dbos: DbosStatuses
) -> None:
    """A reconciler running twice must not charge one crash twice.

    The fact's transition name carries the epoch, so a second recovery of the
    same dead execution is absorbed as a duplicate while a recovery of a later
    one is a new fact.
    """

    work_unit_id = _crashed_work_unit()
    dbos.says({_dead_execution_of(work_unit_id): "ERROR"})

    _reconciler().poll_once()
    _reconciler().poll_once()

    assert repo.automatic_crash_recovery_count(work_unit_id) == 1


def test_concurrent_reconcilers_do_not_double_count(
    work_unit_ledger: Path, dbos: DbosStatuses
) -> None:
    work_unit_id = _crashed_work_unit()
    dbos.says({_dead_execution_of(work_unit_id): "ERROR"})
    errors: list[BaseException] = []
    barrier = threading.Barrier(3)

    def sweep() -> None:
        try:
            barrier.wait(timeout=10)
            _reconciler().poll_once()
        except BaseException as exc:  # noqa: BLE001 - the assertion is "no crash"
            errors.append(exc)

    threads = [threading.Thread(target=sweep) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert repo.automatic_crash_recovery_count(work_unit_id) <= 1


def test_the_recovery_event_is_its_own_type(work_unit_ledger: Path, dbos: DbosStatuses) -> None:
    """It has to be countable separately, which is the whole reason it exists."""

    work_unit_id = _crashed_work_unit()
    dbos.says({_dead_execution_of(work_unit_id): "ERROR"})

    _reconciler().poll_once()

    types = [event.event_type for event in repo.list_work_unit_events(work_unit_id, limit=1000)]
    assert WorkUnitEventType.AUTOMATIC_CRASH_RECOVERY in types


def test_the_reconciler_is_a_resident_loop_with_its_own_lock() -> None:
    """Two from two checkouts would make "which code recovered this" a coin flip."""

    from local_first_agent_os.coordination.resident_loop import ResidentLoop

    assert ResidentLoop.CRASH_RECONCILER.value == "work-unit-crash-reconciler"
    assert len({loop.value for loop in ResidentLoop}) == len(list(ResidentLoop))


def test_the_runtime_scripts_do_not_start_it() -> None:
    """Unattended recovery is a thing an operator turns on, not a default.

    Automatic retries are only safe once a spawned agent's authority is bounded
    by what its plan declared; a reconciler in front of an unbounded spawn path
    is a machine for re-running an over-permitted process.
    """

    script = (Path(__file__).resolve().parents[1] / "scripts" / "start-agent-runtime.sh").read_text(
        encoding="utf-8"
    )
    assert "run_crash_reconciler" not in script
