# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The resident process that hands new WorkUnits to DBOS.

`start_work_unit` writes the WorkUnit and its enqueue row in one coordination
transaction, because the coordination ledger and the DBOS system database are
two databases and cannot share one. The outbox is the bridge, and a bridge needs
something walking across it: DBOS has no idea `work_unit_enqueue_outbox` exists,
and its own recovery only resumes workflows it already started, so it cannot
bootstrap the first execution of anything.

This is that something, and it is deliberately the same shape as
`LedgerDispatcher`: ask the database for work, do it, drain before sleeping. The
database is the queue, so the loop holds no state and several may run at once.

An undeliverable row is left pending rather than failed. "No DBOS runtime is up"
is a statement about this process, not about the WorkUnit, and the row must
survive until a process that can deliver it comes along.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..coordination.availability import ledger_unavailable
from .auto_resume import (
    DEFAULT_MAX_TRANSIENT_RESUMES,
    AutoResumeEnqueued,
    sweep_transient_blocked,
)
from .integration_settlement import settle_landed_integrations
from .root_workflow import (
    EnqueueDelivery,
    EnqueueFailed,
    EnqueueSettled,
    drain_enqueue_outbox,
)

logger = logging.getLogger(__name__)

DEFAULT_IDLE_INTERVAL_SECONDS = 5.0
DEFAULT_BATCH_LIMIT = 20

# How long to wait after a pass that could not reach the ledger at all.
#
# Longer than the idle interval on purpose. A restarting database is measured in
# seconds to minutes, not in poll intervals, and every attempt against a down one
# pays a connection timeout and writes a log line. This is short enough that work
# resumes promptly once the database is back and long enough that a ten-minute
# outage does not produce three hundred identical errors.
DEFAULT_UNAVAILABLE_INTERVAL_SECONDS = 15.0

# How many consecutive passes may find work and fail to deliver all of it before
# the loop treats that as broken rather than as weather.
#
# One undeliverable pass is ordinary: a runtime restarts, a pass lands in the gap.
# A run of them means the rows will never move, which is what a drainer that never
# launched DBOS looked like - a warning per pass, forever, identical to an idle
# queue at a glance. An empty outbox is deliberately NOT this condition: nothing
# pending is the normal steady state, and a loop that stopped on it would stop
# almost immediately.
DEFAULT_UNDELIVERABLE_PASSES_BEFORE_STALLED = 3


@dataclass(frozen=True)
class Delivered:
    """One pass handed at least one WorkUnit to a durable runtime."""

    work_unit_ids: tuple[str, ...]


@dataclass(frozen=True)
class Idle:
    """One pass found nothing it could deliver.

    Nothing pending and nothing deliverable are the same thing to the loop: both
    mean sleep. They differ to an operator, which is why `undeliverable` is
    carried rather than collapsed.
    """

    undeliverable: tuple[str, ...] = ()


@dataclass(frozen=True)
class Stalled:
    """Work is pending and this loop has repeatedly failed to move it.

    Distinct from `Idle` because the operator action differs: idle needs nothing,
    stalled needs someone to look at why delivery is refused. Collapsing the two
    is what let a drainer that could never deliver anything look like a quiet
    queue for an entire session.
    """

    undeliverable: tuple[str, ...]
    consecutive_passes: int
    failed: tuple[EnqueueFailed, ...] = ()
    """Rows whose start raised, with the failure each one raised.

    Empty is the ordinary shape: a stall is usually a stopped runtime refusing
    every row. A non-empty one is a different problem with a different answer,
    and it used to be indistinguishable from an empty queue because
    `drain_enqueue_outbox` returned nothing at all for a row that crashed.
    """


@dataclass(frozen=True)
class Unavailable:
    """The ledger could not be reached on this pass.

    Distinct from `Stalled`, which means the rows are readable and something
    refuses to move them. Here nothing was read at all, so there is nothing to
    say about the outbox, and the only honest report is about the database.
    """

    error: str


DrainOutcome = Delivered | Idle | Stalled | Unavailable


class EnqueueDrainer:
    """Deliver pending root-workflow enqueues, once or on a loop."""

    def __init__(
        self,
        *,
        name: str = "work-unit-enqueue-drainer",
        limit: int = DEFAULT_BATCH_LIMIT,
        delivery: EnqueueDelivery = EnqueueDelivery.DURABLE,
        stalled_after_passes: int = DEFAULT_UNDELIVERABLE_PASSES_BEFORE_STALLED,
        unavailable_interval_seconds: float = DEFAULT_UNAVAILABLE_INTERVAL_SECONDS,
        max_transient_resumes: int = DEFAULT_MAX_TRANSIENT_RESUMES,
    ) -> None:
        self.name = name
        self.limit = limit
        self.delivery = delivery
        self.stalled_after_passes = stalled_after_passes
        self.unavailable_interval_seconds = unavailable_interval_seconds
        self.max_transient_resumes = max_transient_resumes
        self.consecutive_undeliverable_passes = 0

    def ensure_delivery_target(self) -> None:
        """Start the runtime this drainer delivers to.

        Importing the DBOS modules constructs the singleton and registers the
        decorated workflows, but `DBOS.launch()` is what makes `is_dbos_active()`
        true, and `start_root_workflow` refuses a durable delivery until it is.
        Without this the loop polls forever and reports every row undeliverable,
        which reads exactly like an idle queue.

        Launching belongs to the drainer rather than to its caller: a process
        whose whole job is handing work to DBOS is the process that should start
        DBOS. An INLINE drainer executes in-process and needs none of it.
        """

        if self.delivery is not EnqueueDelivery.DURABLE:
            return
        from ..dbos_app import launch_dbos

        launch_dbos()

    def poll_once(self) -> DrainOutcome:
        try:
            # Settlement runs first: a landed commit may complete a milestone
            # and enqueue the RESUME the drain below then delivers, all in one
            # pass. The sweep runs next for the same reason. Both share the
            # drain's unavailability handling because all three are one
            # conversation with the same database.
            settle_landed_integrations()
            for swept in sweep_transient_blocked(self.max_transient_resumes):
                if isinstance(swept, AutoResumeEnqueued):
                    logger.info(
                        "%s queued an automatic resume for work unit %s: %s",
                        self.name,
                        swept.work_unit_id,
                        swept.reason,
                    )
            outcomes = drain_enqueue_outbox(self.limit, self.delivery)
        except Exception as exc:
            # Only an unreachable ledger. Anything else is a defect, and a loop
            # that swallowed defects would spin on one forever while reporting
            # that the database was down.
            if not ledger_unavailable(exc):
                raise
            logger.error(
                "%s could not reach the coordination ledger: %s",
                self.name,
                exc,
            )
            return Unavailable(error=f"{type(exc).__name__}: {exc}")
        failed = tuple(item for item in outcomes if isinstance(item, EnqueueFailed))
        delivered = tuple(
            item.work_unit_id
            for item in outcomes
            if isinstance(item, EnqueueSettled) and item.delivered
        )
        # A row that raised counts as undeliverable, which is the whole point of
        # the change: it used to be in neither list, so a pass in which every row
        # crashed reset the stall counter and reported `Idle`. A drainer that can
        # never deliver anything must not look like a quiet queue.
        undeliverable = tuple(
            item.work_unit_id
            for item in outcomes
            if isinstance(item, EnqueueFailed)
            or (isinstance(item, EnqueueSettled) and not item.delivered)
        )
        for item in failed:
            logger.error(
                "%s could not start work unit %s: %s",
                self.name,
                item.work_unit_id,
                item.failure.message,
                extra=item.failure.observability_fields(),
            )
        if undeliverable:
            self.consecutive_undeliverable_passes += 1
            # Loud once per pass rather than per row: a stopped DBOS runtime makes
            # every row undeliverable at the same moment, and one line is enough
            # to say so.
            logger.warning(
                "%s could not deliver %d enqueue(s); they stay pending (pass %d)",
                self.name,
                len(undeliverable),
                self.consecutive_undeliverable_passes,
            )
        else:
            self.consecutive_undeliverable_passes = 0
        if delivered:
            return Delivered(work_unit_ids=delivered)
        if undeliverable and self.consecutive_undeliverable_passes >= self.stalled_after_passes:
            logger.error(
                "%s has failed to deliver for %d consecutive passes; "
                "work is queued and this loop cannot move it",
                self.name,
                self.consecutive_undeliverable_passes,
            )
            return Stalled(
                undeliverable=undeliverable,
                consecutive_passes=self.consecutive_undeliverable_passes,
                failed=failed,
            )
        return Idle(undeliverable=undeliverable)

    def run(
        self,
        *,
        interval_seconds: float = DEFAULT_IDLE_INTERVAL_SECONDS,
        max_polls: int | None = None,
        sleeper: object = None,
    ) -> int:
        """Poll until `max_polls` is reached, or forever when it is None.

        Returns the number of WorkUnits delivered. A pass that delivered
        something goes straight round again, so a backlog drains at full speed
        and only an idle loop pays the interval.

        A stalled loop keeps polling rather than exiting. The rows are durable and
        the condition is usually a runtime that will come back, so stopping would
        turn a recoverable outage into one needing a human to restart a process.
        `Stalled` is how it says so at ERROR; `poll_once` is the seam a supervisor
        watches if it wants to act on that.

        An unreachable ledger is that same bargain, and it used to be the one
        outage this loop did not survive: the exception left `poll_once`, left
        `run`, and ended the process. A database restart is precisely the
        recoverable outage the paragraph above describes, so it waits that out
        too, on a longer interval because a database does not come back within a
        poll and every attempt costs a connection timeout and a log line.
        """

        self.ensure_delivery_target()
        sleep = sleeper if callable(sleeper) else time.sleep
        delivered = 0
        polls = 0
        while max_polls is None or polls < max_polls:
            polls += 1
            outcome = self.poll_once()
            if isinstance(outcome, Delivered):
                delivered += len(outcome.work_unit_ids)
                continue
            if isinstance(outcome, Unavailable):
                sleep(max(interval_seconds, self.unavailable_interval_seconds))
                continue
            sleep(interval_seconds)
        return delivered


__all__ = [
    "DEFAULT_BATCH_LIMIT",
    "DEFAULT_IDLE_INTERVAL_SECONDS",
    "DEFAULT_UNAVAILABLE_INTERVAL_SECONDS",
    "DEFAULT_UNDELIVERABLE_PASSES_BEFORE_STALLED",
    "Delivered",
    "DrainOutcome",
    "EnqueueDrainer",
    "Idle",
    "Stalled",
    "Unavailable",
]
