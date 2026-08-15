# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Resuming a WorkUnit whose execution died instead of halting.

Every resume test before these ones halted the run *cleanly*: a milestone parked,
a phase blocked, the engine wrote the halt on its way out. That is the path where
the execution records how it ended, and it is the only path anything covered.

The first real WorkUnit took the other path. A missing column raised out of a
step, the process died with the milestone still `RUNNING`, and no halt was ever
written. `execution_epoch` counts halts, so it stayed at zero, so every resume
re-derived the dead run's own workflow IDs and DBOS replayed its recorded error.
A green suite could not see it, because a crash was a shape no test produced.

These tests produce that shape. `_crash_mid_plan` uses a runtime that raises,
which is faithful rather than convenient: only `DispatchWaitTimeout` is caught in
the milestone boundary, so any other exception escapes exactly as the real one
did, after `MILESTONE_STARTED` is recorded and before any halt.

DBOS is the only thing faked here, and only its answer to "what is the status of
this workflow". Faking `execution_liveness` itself would build the stub from the
same model as the code under test, which is the mistake that let the evidence
gate ship unable to fail.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from work_unit_support import compile_acceptance_doc, start_inline

from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units import service
from local_first_agent_os.work_units.events import WorkUnitEventType
from local_first_agent_os.work_units.execution import MilestoneContext, MilestoneOutcome
from local_first_agent_os.work_units.execution_recovery import (
    DEAD_EXECUTION_FAILURE_CODE,
    ExecutionLiveness,
    execution_liveness,
    execution_workflow_id,
    recover_dead_execution,
)
from local_first_agent_os.work_units.lifecycle import (
    MilestoneExecutionStatus,
    WorkUnitStatus,
)
from local_first_agent_os.work_units.root_workflow import (
    EnqueueDelivery,
    WorkUnitEngine,
    set_engine,
)

CRASH = RuntimeError('column "idempotency_key" of relation "dispatch_intents" does not exist')


class CrashingRuntime:
    """A runtime whose milestone raises the way a dying process does.

    It records nothing and returns nothing, so the ledger is left exactly as the
    real incident left it: the milestone marked `RUNNING` by the boundary that
    called this, and no fact about how the execution ended.
    """

    def run(self, context: MilestoneContext) -> MilestoneOutcome:
        raise CRASH


@dataclass(frozen=True)
class _StubStatus:
    status: str


class _StubDbos:
    """Stands in for DBOS's view of which workflow IDs are still live."""

    def __init__(self, statuses: dict[str, str]) -> None:
        self._statuses = statuses
        self.asked: list[str] = []

    def get_workflow_status(self, workflow_id: str) -> _StubStatus | None:
        self.asked.append(workflow_id)
        status = self._statuses.get(workflow_id)
        return None if status is None else _StubStatus(status)


class DbosStatuses:
    """Control over what DBOS reports, withdrawable independently.

    It owns its own `MonkeyPatch` rather than taking the test's. They are the
    same function-scoped instance, so undoing the test's would also undo the
    autouse fixture that points the store at this test's Postgres schema, and
    the failure that produces is an unrelated `UnknownWorkUnit` several frames
    away from the cause.
    """

    def __init__(self, patcher: pytest.MonkeyPatch) -> None:
        self._patcher = patcher

    def says(self, statuses: dict[str, str]) -> _StubDbos:
        stub = _StubDbos(statuses)
        self._patcher.setattr("local_first_agent_os._dbos_runtime.DBOS", stub, raising=False)
        self._patcher.setattr("local_first_agent_os.dbos_app.is_dbos_active", lambda: True)
        return stub

    def raises(self, error: Exception) -> None:
        """Make DBOS present but unable to answer, as a separate pool outage does."""

        class _Failing:
            def get_workflow_status(self, workflow_id: str) -> object:
                raise error

        self._patcher.setattr("local_first_agent_os._dbos_runtime.DBOS", _Failing(), raising=False)
        self._patcher.setattr("local_first_agent_os.dbos_app.is_dbos_active", lambda: True)

    def withdraw(self) -> None:
        """Put the real (unlaunched) DBOS back, so the lifecycle runs inline."""

        self._patcher.undo()


@pytest.fixture()
def dbos() -> Iterator[DbosStatuses]:
    patcher = pytest.MonkeyPatch()
    try:
        yield DbosStatuses(patcher)
    finally:
        patcher.undo()


def _crash_mid_plan() -> str:
    """Start a WorkUnit and let its first milestone kill the execution.

    The raise is swallowed by `drain_enqueue_outbox`, which marks the outbox row
    failed and moves on, so nothing surfaces here. That is the point: the caller
    who asked for the run gets no error, and the only trace is a WorkUnit left
    `RUNNING` with a milestone nothing is running.
    """

    set_engine(
        WorkUnitEngine(
            runtime=CrashingRuntime(),
            approval_wait_seconds=0.0,
            approval_poll_seconds=0.01,
        )
    )
    compiled = compile_acceptance_doc()
    assert compiled.compiled_plan_revision_id is not None
    started = start_inline(compiled.compiled_plan_revision_id)
    work_unit_id = str(started["work_unit_id"])
    unit = repo.get_work_unit(work_unit_id)
    assert unit.status is WorkUnitStatus.RUNNING, f"expected a stranded run, got {unit.status}"
    return work_unit_id


def _event_types(work_unit_id: str) -> list[str]:
    return [
        event.event_type.value for event in repo.list_work_unit_events(work_unit_id, limit=1000)
    ]


def test_a_crashed_execution_records_no_halt_and_leaves_the_epoch_at_zero(
    work_unit_ledger: Path,
) -> None:
    """The state the first real WorkUnit was found in, reproduced.

    This is the regression witness: if any of these three stop being true the
    later tests are no longer exercising the failure they were written for.
    """

    work_unit_id = _crash_mid_plan()

    types = _event_types(work_unit_id)
    assert types[-1] == WorkUnitEventType.MILESTONE_STARTED.value
    assert WorkUnitEventType.WORK_UNIT_BLOCKED.value not in types
    assert WorkUnitEventType.WORK_UNIT_WAITING_FOR_OPERATOR.value not in types
    assert repo.execution_epoch(work_unit_id) == 0


def test_the_epoch_of_a_crashed_run_names_the_workflow_that_died(
    work_unit_ledger: Path,
) -> None:
    """Why the resume could never work, stated as the identity it derives.

    A resume mints its continuation from the epoch. While the epoch is zero the
    ID it yields is the root execution itself, which DBOS holds in `ERROR` and
    refuses to re-run, so the resume replays the recorded failure rather than
    doing anything.
    """

    work_unit_id = _crash_mid_plan()
    unit = repo.get_work_unit(work_unit_id)

    epoch = repo.execution_epoch(work_unit_id)

    assert execution_workflow_id(unit.root_workflow_id, epoch) == unit.root_workflow_id


def test_recovery_records_the_halt_the_dead_execution_never_wrote(
    work_unit_ledger: Path, dbos: DbosStatuses
) -> None:
    work_unit_id = _crash_mid_plan()
    unit = repo.get_work_unit(work_unit_id)
    dbos.says({unit.root_workflow_id: "ERROR"})

    recovered = recover_dead_execution(work_unit_id)

    assert recovered.liveness is ExecutionLiveness.DEAD
    assert recovered.halted_epoch == 0
    assert recovered.repaired is True
    assert repo.execution_epoch(work_unit_id) == 1
    assert repo.get_work_unit(work_unit_id).failure_code == DEAD_EXECUTION_FAILURE_CODE


def test_recovery_moves_the_epoch_off_the_dead_workflow_id(
    work_unit_ledger: Path, dbos: DbosStatuses
) -> None:
    """The point of the repair: the next continuation lands somewhere fresh."""

    work_unit_id = _crash_mid_plan()
    unit = repo.get_work_unit(work_unit_id)
    dbos.says({unit.root_workflow_id: "ERROR"})

    recover_dead_execution(work_unit_id)

    successor = execution_workflow_id(unit.root_workflow_id, repo.execution_epoch(work_unit_id))
    assert successor == f"{unit.root_workflow_id}:resume:1"
    assert successor != unit.root_workflow_id


def test_recovery_records_an_abandoned_milestone_as_blocked(
    work_unit_ledger: Path, dbos: DbosStatuses
) -> None:
    """A `RUNNING` milestone nothing is running is never scheduled again.

    `compute_phase_work_set` classifies `RUNNING` as waiting, so without this the
    resumed run finds nothing ready and exits to its blocked policy on the first
    iteration - a resume that advances the epoch and still does no work.

    Recorded as `BLOCKED` rather than `READY`: the lifecycle refuses
    `RUNNING -> READY`, and returning blocked milestones to the schedulable set
    is already `resume_work_unit`'s job. Recovery only says the work stopped.
    """

    work_unit_id = _crash_mid_plan()
    unit = repo.get_work_unit(work_unit_id)
    stranded = next(
        item
        for item in repo.list_milestone_executions(work_unit_id)
        if item.status is MilestoneExecutionStatus.RUNNING
    )
    assert stranded.child_workflow_id is not None
    dbos.says({unit.root_workflow_id: "ERROR", stranded.child_workflow_id: "ERROR"})

    recovered = recover_dead_execution(work_unit_id)

    assert recovered.abandoned_milestones == (stranded.stable_key,)
    refreshed = next(
        item
        for item in repo.list_milestone_executions(work_unit_id)
        if item.stable_key == stranded.stable_key
    )
    assert refreshed.status is MilestoneExecutionStatus.BLOCKED
    assert refreshed.attempt == stranded.attempt


def test_recovery_leaves_a_milestone_whose_workflow_is_still_live(
    work_unit_ledger: Path, dbos: DbosStatuses
) -> None:
    """The root died; this milestone did not. Resetting it would run it twice."""

    work_unit_id = _crash_mid_plan()
    unit = repo.get_work_unit(work_unit_id)
    stranded = next(
        item
        for item in repo.list_milestone_executions(work_unit_id)
        if item.status is MilestoneExecutionStatus.RUNNING
    )
    assert stranded.child_workflow_id is not None
    dbos.says({unit.root_workflow_id: "ERROR", stranded.child_workflow_id: "PENDING"})

    recovered = recover_dead_execution(work_unit_id)

    assert recovered.abandoned_milestones == ()
    refreshed = next(
        item
        for item in repo.list_milestone_executions(work_unit_id)
        if item.stable_key == stranded.stable_key
    )
    assert refreshed.status is MilestoneExecutionStatus.RUNNING


def test_recovery_writes_nothing_while_the_execution_is_still_running(
    work_unit_ledger: Path, dbos: DbosStatuses
) -> None:
    """The one failure worse than the one being repaired.

    Recording a halt for a live run advances the epoch under it, and the next
    resume then starts a second execution beside the first.
    """

    work_unit_id = _crash_mid_plan()
    unit = repo.get_work_unit(work_unit_id)
    dbos.says({unit.root_workflow_id: "PENDING"})
    events_before = len(repo.list_work_unit_events(work_unit_id, limit=1000))

    recovered = recover_dead_execution(work_unit_id)

    assert recovered.liveness is ExecutionLiveness.LIVE
    assert recovered.repaired is False
    assert repo.execution_epoch(work_unit_id) == 0
    assert len(repo.list_work_unit_events(work_unit_id, limit=1000)) == events_before


def test_an_unminted_continuation_is_absent_rather_than_dead(
    work_unit_ledger: Path, dbos: DbosStatuses
) -> None:
    """`ABSENT` must not read as a death, or every resume inflates the epoch.

    After a clean halt the epoch has already advanced, so the ID recovery asks
    about is the continuation that has not been minted yet. Counting that as a
    death would write a second halt for a run that halted once.
    """

    work_unit_id = _crash_mid_plan()
    unit = repo.get_work_unit(work_unit_id)
    dbos.says({unit.root_workflow_id: "ERROR"})
    recover_dead_execution(work_unit_id)
    assert repo.execution_epoch(work_unit_id) == 1

    again = recover_dead_execution(work_unit_id)

    assert again.liveness is ExecutionLiveness.ABSENT
    assert again.repaired is False
    assert repo.execution_epoch(work_unit_id) == 1


def test_liveness_is_no_runtime_without_a_launched_dbos(work_unit_ledger: Path) -> None:
    """A bare coordination CLI initializes DBOS without launching it.

    Recovering from such a process would be guessing at whether a workflow in
    another process is alive, so it declines to answer rather than guess.
    """

    assert execution_liveness("work-unit:whatever") is ExecutionLiveness.NO_RUNTIME


def test_a_runtime_that_cannot_answer_is_not_the_same_as_no_runtime(
    work_unit_ledger: Path, dbos: DbosStatuses
) -> None:
    """The split that makes the refusal below possible.

    Both mean "repair nothing", so one member covering both is tempting. They need
    opposite responses: with no runtime the resume declines on its own, and here it
    would succeed - minting a continuation on an epoch nothing repaired.
    """

    dbos.raises(RuntimeError("connection pool exhausted"))

    assert execution_liveness("work-unit:whatever") is ExecutionLiveness.INDETERMINATE


def test_a_resume_refuses_when_liveness_cannot_be_determined(
    work_unit_ledger: Path, dbos: DbosStatuses
) -> None:
    """The last silent path in the recovery code.

    DBOS is active, so the resume would mint a continuation and report
    `delivered: true`, on an epoch that was never repaired. That is precisely the
    bug this module exists to fix, returned as a success.

    It must also refuse *before* writing anything, so retrying after the runtime
    recovers starts from the state the operator left.
    """

    work_unit_id = _crash_mid_plan()
    dbos.raises(RuntimeError("connection pool exhausted"))
    events_before = len(repo.list_work_unit_events(work_unit_id, limit=1000))

    result = service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.INLINE)

    assert result["delivered"] is False
    assert "could not determine" in str(result["reason"])
    assert result["recovered"]["liveness"] == ExecutionLiveness.INDETERMINATE.value
    assert len(repo.list_work_unit_events(work_unit_id, limit=1000)) == events_before


def test_a_resume_after_a_crash_runs_the_stranded_milestone_and_finishes(
    work_unit_ledger: Path, dbos: DbosStatuses
) -> None:
    """End to end: the run that could not be resumed is resumed.

    The DBOS stub is withdrawn before the resume so the lifecycle runs inline in
    this process. That is the honest split: recovery is the part that needs to
    ask DBOS anything, and the run that follows is ordinary execution.
    """

    from work_unit_support import install_simulated_engine, settle_operator_decisions

    work_unit_id = _crash_mid_plan()
    unit = repo.get_work_unit(work_unit_id)
    stranded = next(
        item
        for item in repo.list_milestone_executions(work_unit_id)
        if item.status is MilestoneExecutionStatus.RUNNING
    )
    assert stranded.child_workflow_id is not None
    dbos.says({unit.root_workflow_id: "ERROR", stranded.child_workflow_id: "ERROR"})
    recover_dead_execution(work_unit_id)
    dbos.withdraw()

    runtime = install_simulated_engine()
    service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.INLINE)
    settle_operator_decisions(work_unit_id)

    assert stranded.stable_key in runtime.started, "the stranded milestone must run again"
    view = service.get_work_unit(work_unit_id)
    assert view.status is WorkUnitStatus.SUCCEEDED
