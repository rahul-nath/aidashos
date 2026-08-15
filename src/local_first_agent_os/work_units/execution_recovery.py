# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What the ledger still believes is running, checked against what actually is.

A WorkUnit records how each execution *ended*: `_halt` writes the halt, a
milestone writes its own settlement. Every one of those writes is performed by
the execution itself, which means an execution that dies performs none of them.
A crashed run therefore leaves durable state asserting that work is in flight
while no process is carrying it, and nothing else in the system corrects that.

Two facts go stale together, because they are the same lie told at two levels:

- **The execution epoch.** `execution_epoch` counts halts, and a crash records
  none, so the epoch keeps naming the dead execution. Continuation IDs and phase
  workflow IDs are both derived from it, and DBOS refuses to re-run an ID that
  already reached a terminal state, so every resume re-enters the corpse and
  replays its recorded error. The tell is a continuation ID of `:resume:0`: a
  legitimate resume always follows a halt, so a legitimate epoch is never zero.
- **A `RUNNING` milestone.** `compute_phase_work_set` classifies `RUNNING` as
  waiting rather than ready, which is right while something is running it and
  wrong forever once nothing is. The phase loop then finds nothing schedulable
  and exits to its blocked policy on its first iteration.

Recovery is deliberately not a background reaper. It runs when an operator
resumes, because that is the moment someone has asserted the work should
continue, and a repair made then is one the operator can see in the history. A
reaper would have to answer the same question with nobody watching, and its
wrong answers would be invisible.

This module only ever *records* a stop. Stopping things that are still running
is `cancellation.py`, and the two must not be confused: cancellation asks a live
execution to stop, recovery writes down that a dead one already did.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from . import repository as repo
from .events import MilestoneTransition, WorkUnitTransition
from .lifecycle import (
    TERMINAL_WORK_UNIT_STATUSES,
    MilestoneExecutionStatus,
    WorkUnitStatus,
)

SCHEMA_VERSION_WORK_UNIT_EXECUTION_RECOVERY = "work_unit_execution_recovery.v1"

# DBOS's own vocabulary. A workflow in any of these has returned or been given up
# on, and re-running its ID is refused rather than honoured.
_DEAD_DBOS_STATUSES = frozenset({"SUCCESS", "ERROR", "CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"})

DEAD_EXECUTION_FAILURE_CODE = "execution_died_without_recording_a_halt"


class ExecutionLiveness(StrEnum):
    """Whether a durable execution is still being carried by anything."""

    LIVE = "LIVE"
    """DBOS is running it or has it queued to run. Leave it alone."""

    DEAD = "DEAD"
    """DBOS holds it in a terminal state. Whatever the ledger says, it stopped."""

    ABSENT = "ABSENT"
    """DBOS has never heard of this ID.

    For a continuation that has not been minted yet this is the expected answer,
    so it is not a death. It stays distinct from `LIVE` because the two are
    different facts that happen to share a response, and collapsing them would
    make the one case where it matters unreadable.
    """

    NO_RUNTIME = "NO_RUNTIME"
    """There is no launched DBOS here to ask.

    A bare coordination CLI initializes DBOS without launching it. Recovering from
    one would be guessing, and a wrong guess writes a halt for a run still going.
    The resume that follows declines on its own account, so this needs no further
    handling: a process with no durable runtime could not carry a continuation
    either way, and it says so.
    """

    INDETERMINATE = "INDETERMINATE"
    """A runtime is here and the question could not be answered anyway.

    Split from `NO_RUNTIME` because the two share a response - repair nothing -
    and need opposite ones from the caller. With no runtime the resume declines
    and the operator starts one. Here the resume would *succeed*: DBOS is active,
    so a continuation is minted, and it lands on the same epoch that was never
    repaired. That is the original bug, reported as `delivered: true`.

    The cause is real rather than theoretical: the ledger and the DBOS system
    database are separate databases with separate pools, so one can answer while
    the other refuses.
    """


@dataclass(frozen=True)
class RecoveredExecution:
    """What recovery found, and what it changed.

    `halted_epoch` is the epoch whose death was recorded, or `None` when nothing
    needed recording. It is reported rather than kept private because an operator
    resuming a WorkUnit should be able to tell "this continued a run that parked
    for me" from "this recovered a run that crashed", and from the outside those
    are otherwise identical.
    """

    liveness: ExecutionLiveness
    execution_workflow_id: str
    halted_epoch: int | None = None
    abandoned_milestones: tuple[str, ...] = ()

    @property
    def repaired(self) -> bool:
        return self.halted_epoch is not None or bool(self.abandoned_milestones)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION_WORK_UNIT_EXECUTION_RECOVERY,
            "liveness": self.liveness.value,
            "execution_workflow_id": self.execution_workflow_id,
            "halted_epoch": self.halted_epoch,
            "abandoned_milestones": list(self.abandoned_milestones),
        }


def execution_workflow_id(root_workflow_id: str, epoch: int) -> str:
    """The DBOS identity of the execution that runs at `epoch`.

    Epoch zero is the original root execution; every later epoch is the
    continuation minted by the resume that followed the halt before it.
    `resume_root_workflow` derives the same ID when it mints one, and the two
    spellings have to agree, so this is the one both of them use.
    """

    if epoch == 0:
        return root_workflow_id
    return f"{root_workflow_id}:resume:{epoch}"


def execution_liveness(workflow_id: str) -> ExecutionLiveness:
    """Ask DBOS whether anything is still carrying `workflow_id`."""

    from .._dbos_runtime import DBOS
    from ..dbos_app import is_dbos_active

    if DBOS is None or not is_dbos_active():
        return ExecutionLiveness.NO_RUNTIME
    try:
        status = DBOS.get_workflow_status(workflow_id)
    except Exception:  # noqa: BLE001 - reported as indeterminate, not raised
        return ExecutionLiveness.INDETERMINATE
    if status is None:
        return ExecutionLiveness.ABSENT
    return (
        ExecutionLiveness.DEAD
        if str(status.status) in _DEAD_DBOS_STATUSES
        else ExecutionLiveness.LIVE
    )


def recover_dead_execution(work_unit_id: str) -> RecoveredExecution:
    """Record the halt that a dead execution never got to record.

    Writes nothing unless DBOS confirms the execution the ledger believes in is
    terminal. That confirmation is the whole safety argument: recording a halt
    for a live run would advance the epoch under it and let a second execution
    start alongside the first, which is worse than the failure being repaired.
    """

    unit = repo.get_work_unit(work_unit_id)
    epoch = repo.execution_epoch(work_unit_id)
    workflow_id = execution_workflow_id(unit.root_workflow_id, epoch)

    if unit.status in TERMINAL_WORK_UNIT_STATUSES:
        # Settled WorkUnits are not resumable at all, so there is no epoch to
        # repair. Refusing the resume is the caller's job, not this one's.
        return RecoveredExecution(ExecutionLiveness.DEAD, workflow_id)

    liveness = execution_liveness(workflow_id)
    if liveness is not ExecutionLiveness.DEAD:
        return RecoveredExecution(liveness, workflow_id)

    repo.record_fact(
        work_unit_id,
        WorkUnitTransition(
            status=WorkUnitStatus.BLOCKED,
            # No phase change. The WorkUnit stopped where it already was, and
            # `_halt` names a phase only because it is the phase boundary that
            # decided to stop. Recovery is not that boundary and has nothing to
            # add about which phase the run was in.
            current_phase=None,
            failure_code=DEAD_EXECUTION_FAILURE_CODE,
            failure_summary=(
                f"execution {workflow_id} reached a terminal state without recording "
                "how it ended, so its epoch was still current"
            ),
            epoch=epoch,
        ),
    )
    return RecoveredExecution(
        liveness,
        workflow_id,
        halted_epoch=epoch,
        abandoned_milestones=_block_abandoned_milestones(work_unit_id),
    )


def _block_abandoned_milestones(work_unit_id: str) -> tuple[str, ...]:
    """Record `RUNNING` milestones whose child workflow is dead as `BLOCKED`.

    `BLOCKED` and not `READY`, for two reasons. The lifecycle refuses
    `RUNNING -> READY` outright, and it is right to: a milestone that was running
    stopped for a reason, and jumping it straight back to schedulable would erase
    that. And retrying is a policy decision that already has an owner -
    `resume_work_unit` turns blocked milestones back to `READY` on its way past.
    This function's job is only to say the work stopped.

    Only milestones DBOS confirms are dead are touched. One whose child workflow
    is still live belongs to an execution still carrying it, and one with no
    child workflow ID never reached DBOS, so neither is this function's to touch.
    """

    abandoned: list[str] = []
    for execution in repo.list_milestone_executions(work_unit_id):
        if execution.status is not MilestoneExecutionStatus.RUNNING:
            continue
        if execution.child_workflow_id is None:
            continue
        if execution_liveness(execution.child_workflow_id) is not ExecutionLiveness.DEAD:
            continue
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=execution.phase,
                milestone_key=execution.stable_key,
                status=MilestoneExecutionStatus.BLOCKED,
                attempt=execution.attempt,
                failure_code=DEAD_EXECUTION_FAILURE_CODE,
                failure_summary=(
                    f"child workflow {execution.child_workflow_id} reached a terminal "
                    "state while the milestone was still marked running"
                ),
            ),
        )
        abandoned.append(execution.stable_key)
    return tuple(abandoned)


__all__ = [
    "DEAD_EXECUTION_FAILURE_CODE",
    "SCHEMA_VERSION_WORK_UNIT_EXECUTION_RECOVERY",
    "ExecutionLiveness",
    "RecoveredExecution",
    "execution_liveness",
    "execution_workflow_id",
    "recover_dead_execution",
]
