# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The reaper `execution_recovery` deliberately was not, built deliberately.

``execution_recovery`` records the halt a dead execution never wrote, and its
docstring says recovery is operator-triggered: "a reaper would have to answer the
same question with nobody watching, and its wrong answers would be invisible."
That is the right objection, and it is an argument about *evidence*, not about
reapers. It is answered here rather than dodged:

- the question is never answered by inference. A crash writes no halt and
  therefore no failure code, so filtering on
  ``failure_code == execution_died_without_recording_a_halt`` would find exactly
  the WorkUnits an operator had already repaired and none of the ones that
  crashed. The candidate set is "nonterminal and claiming an execution", and the
  answer comes from DBOS.
- nothing is recovered on anything but ``ExecutionLiveness.DEAD``. ``ABSENT`` is
  a continuation not yet minted and ``NO_RUNTIME`` is a process with no DBOS to ask;
  both mean do nothing, for opposite reasons.
- nothing is resumed unless the recovery *proof* says it repaired something.
  ``RecoveredExecution.repaired`` is that proof, and a resume without it is a
  resume of a WorkUnit that stopped for a reason nobody has looked at.
- the wrong answers are not invisible. Every repair writes an
  ``AUTOMATIC_CRASH_RECOVERY`` event naming the epoch it repaired, which is both
  the audit trail and the budget.

The budget is its own counter, and that is the second correction. ``execution_epoch``
counts ``WORK_UNIT_BLOCKED`` and ``WORK_UNIT_WAITING_FOR_OPERATOR`` - every halt
however caused. Reading it as a crash-retry counter would let a WorkUnit that
asks three approval questions exhaust its allowance for surviving crashes, and
let one that crashes repeatedly between approvals never touch it.

Not started by ``scripts/start-agent-runtime.sh``. Unattended recovery is a thing
an operator turns on, and it is worth saying why in one place rather than in the
shell script: automatic retries are only safe once a spawned agent's authority is
bounded by what its plan declared, which is what ``spawn_authority`` now does. A
reconciler in front of an unbounded spawn path is a machine for re-running an
over-permitted process.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..progress_events import emit_progress
from . import repository as repo
from . import service
from .events import AutomaticCrashRecovery
from .execution_recovery import (
    ExecutionLiveness,
    RecoveredExecution,
    execution_workflow_id,
    recover_dead_execution,
)
from .lifecycle import TERMINAL_WORK_UNIT_STATUSES, WorkUnitStatus
from .root_workflow import EnqueueDelivery

DEFAULT_IDLE_INTERVAL_SECONDS = 30.0
"""Slower than the drainer's five seconds, on purpose.

A crash is rare and a reconciler that asks DBOS about every running WorkUnit is
not free. The cost of noticing a crash a minute late is a minute; the cost of
polling hard is paid every minute forever.
"""

DEFAULT_MAX_AUTOMATIC_RECOVERIES = 3
"""How many times one WorkUnit may be recovered without a person.

A WorkUnit whose execution keeps dying is not a WorkUnit that needs another
restart. Past this it is left exactly where the recovery put it - ``BLOCKED``,
resumable - for an operator who can ask why.
"""

# Only these can be claiming a live execution. The others are either terminal,
# or have not started, or are already halted and waiting on somebody.
_CLAIMING_STATUSES = (WorkUnitStatus.RUNNING, WorkUnitStatus.CANCELLING)


@dataclass(frozen=True)
class Repaired:
    """This WorkUnit's execution was dead, and the reconciler restarted it."""

    work_unit_id: str
    recovery: RecoveredExecution
    resumed: bool
    """Whether the continuation was actually delivered.

    False is a real answer: `resume_work_unit` reports `delivered: False` when no
    durable runtime could take the continuation, and reporting that as a repair
    would be the "a report that cannot say no always says yes" failure again.
    """


@dataclass(frozen=True)
class LeftAlone:
    """This WorkUnit was inspected and not touched, with the reason why."""

    work_unit_id: str
    liveness: ExecutionLiveness
    execution_workflow_id: str


@dataclass(frozen=True)
class BudgetExhausted:
    """This WorkUnit has been recovered automatically as often as it may be."""

    work_unit_id: str
    recoveries: int
    permitted: int

    def describe(self) -> str:
        return (
            f"work unit {self.work_unit_id} has been recovered automatically "
            f"{self.recoveries} time(s) of {self.permitted} permitted; it is left "
            "blocked for an operator"
        )


# What the reconciler decided about one WorkUnit. A sum, because "did nothing"
# has three different reasons and an operator reading the log needs which one.
type ReconcileVerdict = Repaired | LeftAlone | BudgetExhausted


@dataclass(frozen=True)
class ReconcilePass:
    """One sweep: what it looked at, and what it decided about each."""

    inspected: int = 0
    verdicts: tuple[ReconcileVerdict, ...] = ()

    @property
    def repaired(self) -> tuple[Repaired, ...]:
        return tuple(item for item in self.verdicts if isinstance(item, Repaired))

    @property
    def productive(self) -> bool:
        return bool(self.repaired)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": "work_unit_crash_reconcile_pass.v1",
            "inspected": self.inspected,
            "repaired": [
                {
                    "work_unit_id": item.work_unit_id,
                    "execution_workflow_id": item.recovery.execution_workflow_id,
                    "halted_epoch": item.recovery.halted_epoch,
                    "abandoned_milestones": list(item.recovery.abandoned_milestones),
                    "resumed": item.resumed,
                }
                for item in self.repaired
            ],
            "budget_exhausted": [
                item.work_unit_id for item in self.verdicts if isinstance(item, BudgetExhausted)
            ],
            "left_alone": [
                {"work_unit_id": item.work_unit_id, "liveness": item.liveness.value}
                for item in self.verdicts
                if isinstance(item, LeftAlone)
            ],
        }


@dataclass
class CrashReconciler:
    """Find WorkUnits whose execution died, and put them back on the rails.

    The dependencies are injected rather than imported at the call site so a test
    can drive the whole decision without a DBOS runtime. They default to the real
    ones, so a caller that wants the real thing writes ``CrashReconciler()``.
    """

    max_automatic_recoveries: int = DEFAULT_MAX_AUTOMATIC_RECOVERIES
    recover: Callable[[str], RecoveredExecution] = recover_dead_execution
    resume: Callable[[str], dict[str, Any]] = field(
        default=lambda work_unit_id: service.resume_work_unit(
            work_unit_id, delivery=EnqueueDelivery.DURABLE
        )
    )

    def ensure_delivery_target(self) -> None:
        """Launch DBOS before the first pass.

        Without it every liveness answer is ``NO_RUNTIME`` and the reconciler is a
        loop that inspects nothing and reports nothing wrong, which is the worst
        of the available failures. The enqueue drainer opens with the same call
        for the same reason.
        """

        from ..dbos_app import launch_dbos

        launch_dbos()

    def candidates(self) -> Sequence[repo.WorkUnitRow]:
        """WorkUnits that are nonterminal and claim an execution is carrying them.

        Not a filter on ``failure_code``. A crash records no halt and therefore no
        failure code, so filtering on the dead-execution code would find exactly
        the WorkUnits an operator had already repaired and none that crashed.

        ``RUNNING`` and ``CANCELLING`` are the two statuses that assert something
        is in flight. A ``BLOCKED`` or ``WAITING_FOR_OPERATOR`` WorkUnit has
        already recorded how it stopped, so there is no missing halt to write.
        """

        return tuple(
            unit
            for status in _CLAIMING_STATUSES
            for unit in repo.list_work_units(status)
            if unit.status not in TERMINAL_WORK_UNIT_STATUSES
        )

    def poll_once(self) -> ReconcilePass:
        units = self.candidates()
        verdicts = [self._reconcile(unit) for unit in units]
        result = ReconcilePass(inspected=len(units), verdicts=tuple(verdicts))
        if result.productive:
            emit_progress(
                f"recovered {len(result.repaired)} crashed execution(s) of "
                f"{result.inspected} inspected",
                phase="crash_recovery_pass",
                inspected=result.inspected,
                repaired=len(result.repaired),
            )
        return result

    def _reconcile(self, unit: repo.WorkUnitRow) -> ReconcileVerdict:
        recoveries = repo.automatic_crash_recovery_count(unit.work_unit_id)
        if recoveries >= self.max_automatic_recoveries:
            # Checked before DBOS is asked, so an exhausted WorkUnit costs one
            # cheap count per pass rather than a status round trip forever.
            verdict = BudgetExhausted(
                work_unit_id=unit.work_unit_id,
                recoveries=recoveries,
                permitted=self.max_automatic_recoveries,
            )
            emit_progress(
                verdict.describe(),
                phase="crash_recovery_budget_exhausted",
                work_unit_id=unit.work_unit_id,
            )
            return verdict

        recovery = self.recover(unit.work_unit_id)
        if not recovery.repaired:
            # Covers LIVE, ABSENT, NO_RUNTIME, and INDETERMINATE in one condition,
            # deliberately.
            # The distinction between them is what `recover` used to decide; what
            # this needs is whether anything was written, and only DEAD writes.
            return LeftAlone(
                work_unit_id=unit.work_unit_id,
                liveness=recovery.liveness,
                execution_workflow_id=recovery.execution_workflow_id,
            )

        assert recovery.halted_epoch is not None  # `repaired` implies one or both
        repo.record_fact(
            unit.work_unit_id,
            AutomaticCrashRecovery(
                execution_workflow_id=recovery.execution_workflow_id,
                halted_epoch=recovery.halted_epoch,
                abandoned_milestones=recovery.abandoned_milestones,
            ),
        )
        delivery = self.resume(unit.work_unit_id)
        return Repaired(
            work_unit_id=unit.work_unit_id,
            recovery=recovery,
            resumed=bool(delivery.get("delivered")),
        )

    def run(
        self,
        *,
        interval_seconds: float = DEFAULT_IDLE_INTERVAL_SECONDS,
        max_polls: int | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> int:
        """Sweep until ``max_polls``, or forever. Returns how many it repaired."""

        self.ensure_delivery_target()
        sleep = sleeper or time.sleep
        repaired = 0
        polls = 0
        while max_polls is None or polls < max_polls:
            polls += 1
            result = self.poll_once()
            repaired += len(result.repaired)
            if max_polls is not None and polls >= max_polls:
                break
            # A productive pass may have unblocked more work; ask again at once.
            if not result.productive:
                sleep(interval_seconds)
        return repaired


__all__ = [
    "DEFAULT_IDLE_INTERVAL_SECONDS",
    "DEFAULT_MAX_AUTOMATIC_RECOVERIES",
    "BudgetExhausted",
    "CrashReconciler",
    "LeftAlone",
    "ReconcilePass",
    "ReconcileVerdict",
    "Repaired",
    "execution_workflow_id",
]
