# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Stopping a WorkUnit: the cascade that a cancellation request implies.

Cancelling used to be one write. It recorded `CANCELLED` milestone facts and
stopped nothing: not the dispatch intent, not the DBOS workflow, not the agent.
The ledger said the work had stopped while the work continued, which is the worst
kind of wrong a ledger can be.

The fix is to stop treating cancellation as a fact and start treating it as a
request with a cascade behind it. `CANCELLING` is the state that distinction
needs: it says stopping was asked for and has not finished. `CANCELLED` is then
earned rather than asserted, written only once every stoppable thing has been
told to stop.

Three kinds of thing get stopped, because three different things can be holding
the work:

- A `PENDING` or `PAUSED` dispatch intent becomes `CANCELED` and cannot start
  or resume work.
- A DBOS workflow is cancelled, so nothing resumes it on recovery.
- An **execution lease** is asked to cancel, which is what actually reaches a
  running agent. `agent_execution_leases.intent_id` is a foreign key to the
  intent, so the lease is findable from the same intent ids the cascade already
  has; the supervisor holding it terminates its OS process group at the next
  heartbeat.

The intent and the lease are a pair, and neither alone is enough.
`cancel_dispatch_intent` refuses active states, so cancelling a claimed intent
stops nothing that is running. That refusal is reported rather than swallowed,
and the lease stop is what covers the case.
Leases are resolved *before* anything is stopped, because the intent is about to
be refused and the lease is the only remaining handle on the process it started.

Each stop carries a `StopVerdict` rather than a boolean, because asking a
supervisor to cancel is not the same as having stopped: an operator reading the
result needs to know which of "done", "asked, wait a beat", and "nothing here
will stop it, go kill it" they are looking at.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..coordination.execution import live_execution_leases_for_intent
from . import repository as repo
from .events import MilestoneTransition, WorkUnitTransition
from .lifecycle import (
    TERMINAL_MILESTONE_STATUSES,
    TERMINAL_WORK_UNIT_STATUSES,
    MilestoneExecutionStatus,
    WorkUnitStatus,
)

SCHEMA_VERSION_WORK_UNIT_CANCELLATION = "work_unit_cancellation.v1"


class StopTargetKind(StrEnum):
    """What sort of thing the cascade tried to stop.

    A sum rather than a boolean pair because these fail for unrelated reasons and
    an operator's next move differs: a refused intent may mean an agent is still
    running, while a refused workflow means recovery could restart something.
    """

    DISPATCH_INTENT = "DISPATCH_INTENT"
    DBOS_WORKFLOW = "DBOS_WORKFLOW"
    EXECUTION_LEASE = "EXECUTION_LEASE"


class StopVerdict(StrEnum):
    """What became of one attempt to stop something.

    Three states rather than a boolean, because stopping an agent is not
    synchronous and a boolean would have to lie in one direction or the other.
    A lease that has been asked to cancel is neither stopped nor refused: a
    supervisor will terminate its process group at the next heartbeat. Calling
    that `True` claims the process is dead; calling it `False` reads as a failure
    the operator must act on. It is a third thing, and it maps to a third
    operator response, which is to wait a moment and re-check.
    """

    STOPPED = "STOPPED"
    """It cannot do more work. Nothing further is required."""

    CANCELLATION_REQUESTED = "CANCELLATION_REQUESTED"
    """Asked to stop cooperatively. A supervisor acts on its next heartbeat."""

    REFUSED = "REFUSED"
    """Could not be stopped, and nothing here will stop it. Intervene by hand."""


@dataclass(frozen=True)
class StopAttempt:
    """One thing the cascade tried to stop, and what actually happened."""

    kind: StopTargetKind
    identifier: str
    verdict: StopVerdict
    detail: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "identifier": self.identifier,
            "verdict": self.verdict.value,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class CancellationResult:
    """What the cascade did, in enough detail to act on."""

    work_unit_id: str
    status: WorkUnitStatus
    cancelled: bool
    reason: str
    cancelled_milestones: tuple[str, ...] = ()
    attempts: tuple[StopAttempt, ...] = ()

    @property
    def refused(self) -> tuple[StopAttempt, ...]:
        """Things nothing here will stop. The operator has to intervene."""

        return tuple(item for item in self.attempts if item.verdict is StopVerdict.REFUSED)

    @property
    def awaiting_stop(self) -> tuple[StopAttempt, ...]:
        """Asked to stop, not yet stopped. Expected, and worth re-checking."""

        return tuple(
            item for item in self.attempts if item.verdict is StopVerdict.CANCELLATION_REQUESTED
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION_WORK_UNIT_CANCELLATION,
            "work_unit_id": self.work_unit_id,
            "status": self.status.value,
            "cancelled": self.cancelled,
            "reason": self.reason,
            "cancelled_milestones": list(self.cancelled_milestones),
            "attempts": [item.to_payload() for item in self.attempts],
            "refused": [item.to_payload() for item in self.refused],
            "awaiting_stop": [item.to_payload() for item in self.awaiting_stop],
        }


def _stop_dispatch_intent(intent_id: str, reason: str) -> StopAttempt:
    from ..coordination.dispatch import cancel_dispatch_intent

    try:
        result = cancel_dispatch_intent(intent_id, reason=reason)
    except Exception as exc:  # noqa: BLE001 - one failed stop must not abort the cascade
        return StopAttempt(
            kind=StopTargetKind.DISPATCH_INTENT,
            identifier=intent_id,
            verdict=StopVerdict.REFUSED,
            detail=f"{type(exc).__name__}: {exc}",
        )
    if result.get("ok"):
        return StopAttempt(
            kind=StopTargetKind.DISPATCH_INTENT,
            identifier=intent_id,
            verdict=StopVerdict.STOPPED,
        )
    # `not_cancelable` means the intent is active or terminal. An active process
    # is covered by `_stop_execution_lease`; a terminal row is reported because
    # this function cannot infer which of those two cases the refusal represents.
    return StopAttempt(
        kind=StopTargetKind.DISPATCH_INTENT,
        identifier=intent_id,
        verdict=StopVerdict.REFUSED,
        detail=str(result.get("message") or result.get("error") or "refused"),
    )


def _stop_execution_lease(lease_id: str, reason: str) -> StopAttempt:
    """Ask the supervisor holding this lease to terminate its agent.

    Cooperative and therefore never immediate. `request_execution_cancel` flips
    the lease to `CANCEL_REQUESTED`; the supervisor reads that on its next
    heartbeat and kills the process group it owns. So the honest verdict is
    `CANCELLATION_REQUESTED`, not `STOPPED`.

    An already-terminal lease is `STOPPED`, because the process it supervised is
    gone whatever the cascade does next.
    """

    from ..coordination.execution import request_execution_cancel

    try:
        result = request_execution_cancel(lease_id, reason=reason)
    except Exception as exc:  # noqa: BLE001 - one failed stop must not abort the cascade
        return StopAttempt(
            kind=StopTargetKind.EXECUTION_LEASE,
            identifier=lease_id,
            verdict=StopVerdict.REFUSED,
            detail=f"{type(exc).__name__}: {exc}",
        )
    if result.get("ok"):
        return StopAttempt(
            kind=StopTargetKind.EXECUTION_LEASE,
            identifier=lease_id,
            verdict=StopVerdict.CANCELLATION_REQUESTED,
            detail="the supervisor terminates its process group at the next heartbeat",
        )
    if result.get("error") == "already_terminal":
        return StopAttempt(
            kind=StopTargetKind.EXECUTION_LEASE,
            identifier=lease_id,
            verdict=StopVerdict.STOPPED,
            detail="lease was already terminal",
        )
    return StopAttempt(
        kind=StopTargetKind.EXECUTION_LEASE,
        identifier=lease_id,
        verdict=StopVerdict.REFUSED,
        detail=str(result.get("message") or result.get("error") or "refused"),
    )


def _stop_dbos_workflow(workflow_id: str) -> StopAttempt:
    from .._dbos_runtime import DBOS
    from ..dbos_app import is_dbos_active

    if DBOS is None or not is_dbos_active():
        # Nothing is running it, so there is nothing to stop. Reported as stopped
        # because the question is "can this do more work", and it cannot.
        return StopAttempt(
            kind=StopTargetKind.DBOS_WORKFLOW,
            identifier=workflow_id,
            verdict=StopVerdict.STOPPED,
            detail="no active DBOS runtime",
        )
    try:
        DBOS.cancel_workflow(workflow_id)
    except Exception as exc:  # noqa: BLE001 - one failed stop must not abort the cascade
        return StopAttempt(
            kind=StopTargetKind.DBOS_WORKFLOW,
            identifier=workflow_id,
            verdict=StopVerdict.REFUSED,
            detail=f"{type(exc).__name__}: {exc}",
        )
    return StopAttempt(
        kind=StopTargetKind.DBOS_WORKFLOW,
        identifier=workflow_id,
        verdict=StopVerdict.STOPPED,
    )


def run_cancellation_cascade(
    work_unit_id: str,
    *,
    reason: str = "cancelled by operator",
    max_workers: int = 8,
) -> CancellationResult:
    """Move a WorkUnit to CANCELLING, stop what can be stopped, then settle it.

    The order is the invariant. `CANCELLING` is recorded *before* anything is
    stopped, so a crash midway leaves a WorkUnit that visibly has not finished
    cancelling rather than one that claims it has. Re-running the cascade from
    `CANCELLING` is safe and is the intended recovery.

    The stops run concurrently because they are independent and each is a
    round trip; a WorkUnit with several live milestones would otherwise pay for
    them in series while an agent keeps working.
    """

    unit = repo.get_work_unit(work_unit_id)
    if unit.status in TERMINAL_WORK_UNIT_STATUSES:
        return CancellationResult(
            work_unit_id=work_unit_id,
            status=unit.status,
            cancelled=False,
            reason=f"already {unit.status.value}",
        )

    live = [
        execution
        for execution in repo.list_milestone_executions(work_unit_id)
        if execution.status not in TERMINAL_MILESTONE_STATUSES
    ]

    if unit.status is not WorkUnitStatus.CANCELLING:
        repo.record_fact(
            work_unit_id,
            WorkUnitTransition(
                status=WorkUnitStatus.CANCELLING,
                reason=reason,
                epoch=repo.execution_epoch(work_unit_id),
            ),
        )

    # Deduplicated because two milestone attempts can name one intent, and asking
    # twice would report a spurious `not_pending` for the second.
    intent_ids = {
        execution.dispatch_intent_id for execution in live if execution.dispatch_intent_id
    }
    workflow_ids = {
        execution.child_workflow_id for execution in live if execution.child_workflow_id
    }
    workflow_ids.add(unit.root_workflow_id)

    # Leases are resolved before anything is stopped. A claimed intent is about
    # to be refused, and the lease is the only handle on the process that intent
    # started, so losing it would leave an agent running with nothing naming it.
    lease_ids = sorted(
        {
            str(lease["lease_id"])
            for intent_id in sorted(intent_ids)
            for lease in live_execution_leases_for_intent(intent_id)
            if lease.get("lease_id")
        }
    )

    jobs: list[Any] = [
        (lambda item=item: _stop_dispatch_intent(item, reason)) for item in sorted(intent_ids)
    ]
    jobs.extend((lambda item=item: _stop_execution_lease(item, reason)) for item in lease_ids)
    jobs.extend((lambda item=item: _stop_dbos_workflow(item)) for item in sorted(workflow_ids))

    if jobs:
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(jobs)))) as pool:
            attempts = tuple(pool.map(lambda job: job(), jobs))
    else:
        attempts = ()

    cancelled_milestones: list[str] = []
    for execution in live:
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=execution.phase,
                milestone_key=execution.stable_key,
                status=MilestoneExecutionStatus.CANCELLED,
                attempt=max(1, execution.attempt),
                failure_code="work_unit_cancelled",
                failure_summary=reason,
            ),
        )
        cancelled_milestones.append(execution.stable_key)

    repo.record_fact(
        work_unit_id,
        WorkUnitTransition(
            status=WorkUnitStatus.CANCELLED,
            reason=reason,
            epoch=repo.execution_epoch(work_unit_id),
        ),
    )

    return CancellationResult(
        work_unit_id=work_unit_id,
        status=WorkUnitStatus.CANCELLED,
        cancelled=True,
        reason=reason,
        cancelled_milestones=tuple(cancelled_milestones),
        attempts=attempts,
    )


__all__ = [
    "SCHEMA_VERSION_WORK_UNIT_CANCELLATION",
    "CancellationResult",
    "StopAttempt",
    "StopTargetKind",
    "StopVerdict",
    "run_cancellation_cascade",
]
