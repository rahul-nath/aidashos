# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The resident process that drains one project's integration queue.

A stack builder with nothing calling it is the unconsulted-mechanism shape the
design document was written against, so the loop lands in the same milestone as
the builder rather than as a runner at the end. Every milestone from here on is
exercised through this.

The shape is the one `enqueue_drainer` and `dispatcher_loop` already have: hold a
single owner, ask the durable store for work, do it, drain before sleeping.

Batch formation is the loop, and there is no wait policy
========================================================

There is no linger timer, no sibling wait, and no batch cap. `select_next_batch`
takes every `Queued` request for the project in enqueue order, which means every
poll drains everything queued, which means a request cannot be passed over by a
later arrival.

What grows a batch is the previous run. An integration run allocates a worktree,
merges N commits, and (from milestone 4) runs the project's whole verification
command set, which for a real project is seconds to minutes. Every `CODE_MERGE`
an operator resolves *during* that run enqueues behind it and forms the next
batch. So batch size is a function of arrival rate against integration duration,
which is the correct thing for it to be a function of, and nobody had to choose a
number.

Starvation is impossible for the same reason: selection is FIFO over the whole
queue and the batch is the whole queue, so the only way to wait is behind a run
that is already executing, which is bounded by that run. A wait-for-siblings
policy could not have had that property, because a sibling may be approved much
later or never.

When it sleeps, and why that is not the same as "after doing work"
==================================================================

It sleeps on an empty queue, and it sleeps after a run that decided nothing.

The design says "do not sleep after doing work", and a run that abandoned did
not do any: a dirty working tree, an unallocatable worktree, or milestone 3's
missing fast-forward all return the whole batch to `Queued` unchanged. Polling
again immediately would rebuild the same stack against the same condition as fast
as the machine allows, which is a hot loop wearing a drain's clothing. The
condition that ends an abandonment is always outside the queue, so waiting for it
is the only thing that can help.

A run that parked or landed anything did do work, and it polls again at once.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from ..coordination.availability import ledger_unavailable
from ..coordination.integration_queue import read_integration_requests, recover_in_flight_requests
from ..coordination.resident_loop import ResidentLoop, ResidentLoopBusy, hold_resident_loop
from ..coordination.store import connect, err, ok
from ..project_center import LinkedProject, load_project_center
from ..settings import Settings, get_settings
from .driver import RefineryRun, integrate_batch
from .queue import NothingToIntegrate, select_next_batch
from .requests import IntegrationRequestId
from .stack import StackBuilder

logger = logging.getLogger(__name__)

DEFAULT_UNAVAILABLE_INTERVAL_SECONDS = 15.0
"""How long to wait after a pass that could not reach the ledger at all.

The same number and the same reason as the enqueue drainer's: a restarting
database is measured in seconds to minutes rather than in poll intervals, and
every attempt against a down one pays a connection timeout and writes a log line.
"""


@dataclass(frozen=True)
class Idle:
    """Nothing was queued for this project.

    No worktree is allocated, no command runs, no batch row is written. A batch
    of zero is not a batch: an empty stack is trivially green, and a durable
    record of an integration that integrated nothing cannot be told from one that
    did.
    """


@dataclass(frozen=True)
class Drained:
    """One batch was taken to a verdict."""

    run: RefineryRun

    @property
    def decided_anything(self) -> bool:
        return self.run.decided_anything


@dataclass(frozen=True)
class Unavailable:
    """The coordination ledger could not be reached on this pass."""

    error: str


type RefineryPoll = Idle | Drained | Unavailable


class Refinery:
    """Drain one target project's integration queue, once or on a loop."""

    def __init__(
        self,
        target_project_id: str,
        *,
        project: LinkedProject | None = None,
        builder: StackBuilder | None = None,
        settings: Settings | None = None,
        clock: Any = time.time,
    ) -> None:
        self.settings = settings or get_settings()
        self.target_project_id = target_project_id
        self.project = project or load_project_center(self.settings).project_by_id(
            target_project_id
        )
        self.builder = builder or StackBuilder(
            repository_path=self.project.expanded_path,
            worktree_root=self.settings.saga_worktree_root,
            timeout_seconds=self.settings.git_operation_timeout_seconds,
        )
        self.clock = clock

    def recover(self) -> tuple[IntegrationRequestId, ...]:
        """Return every outstanding attempt to the queue before selecting anything.

        `select_next_batch` crashes on an `InFlight` row rather than skipping it,
        so this is not optional cleanup: it is the precondition. Safe because
        nothing advances the integrated branch unless a gate went green, which
        makes an unfinished attempt redoable from scratch whatever state its
        worktree was left in.
        """

        with connect() as c:
            recovered = recover_in_flight_requests(
                c,
                target_project_id=self.target_project_id,
                recorded_at=self.clock(),
            )
        if recovered:
            logger.warning(
                "refinery for %s returned %d outstanding attempt(s) to the queue: %s",
                self.target_project_id,
                len(recovered),
                ", ".join(recovered),
            )
        return recovered

    def poll_once(self) -> RefineryPoll:
        try:
            with connect() as c:
                selection = select_next_batch(
                    read_integration_requests(c, target_project_id=self.target_project_id),
                    target_project_id=self.target_project_id,
                )
                if isinstance(selection, NothingToIntegrate):
                    return Idle()
                run = integrate_batch(
                    c,
                    selection,
                    project=self.project,
                    builder=self.builder,
                    now=self.clock(),
                )
        except Exception as exc:
            # Only an unreachable ledger is weather. Anything else is a defect,
            # and a loop that swallowed defects would spin on one forever while
            # reporting that the database was down.
            if not ledger_unavailable(exc):
                raise
            logger.error(
                "refinery for %s could not reach the coordination ledger: %s",
                self.target_project_id,
                exc,
            )
            return Unavailable(error=f"{type(exc).__name__}: {exc}")

        logger.info(
            "refinery for %s ran batch %s: %d integrated, %d parked",
            self.target_project_id,
            run.batch_id,
            len(run.outcome.integrated),
            len(run.outcome.isolated),
        )
        return Drained(run=run)

    def drain(
        self,
        *,
        interval_seconds: float | None = None,
        max_polls: int | None = None,
        sleep: Any = time.sleep,
    ) -> list[RefineryPoll]:
        """Poll until `max_polls`, or forever.

        `max_polls` bounds the run for an operator driving one batch by hand and
        for a test that must terminate; unset is the resident case.
        """

        idle_interval = (
            interval_seconds
            if interval_seconds is not None
            else self.settings.refinery_poll_seconds
        )
        self.recover()
        polls: list[RefineryPoll] = []
        while max_polls is None or len(polls) < max_polls:
            poll = self.poll_once()
            polls.append(poll)
            if isinstance(poll, Drained) and poll.decided_anything:
                # Work happened, so siblings may have arrived while it did.
                continue
            if max_polls is not None and len(polls) >= max_polls:
                break
            sleep(
                DEFAULT_UNAVAILABLE_INTERVAL_SECONDS
                if isinstance(poll, Unavailable)
                else idle_interval
            )
        return polls


def run_refinery(
    target_project_id: str,
    interval_seconds: float | None = None,
    max_polls: int | None = None,
) -> dict[str, Any]:
    """Hold one project's refinery and drain its queue. The coordination verb.

    Refuses rather than raising when another process holds the project, which is
    the normal outcome of starting the runtime twice rather than a failure
    anybody needs a traceback for.
    """

    with hold_resident_loop(ResidentLoop.REFINERY, scope=target_project_id) as lease:
        if isinstance(lease, ResidentLoopBusy):
            return err(
                "resident_loop_busy",
                message=lease.describe(),
                loop=lease.loop.value,
                target_project_id=target_project_id,
                owner=lease.owner.to_payload() if lease.owner else None,
            )
        refinery = Refinery(target_project_id)
        polls = refinery.drain(interval_seconds=interval_seconds, max_polls=max_polls)

    return ok(
        target_project_id=target_project_id,
        integrated_branch=refinery.project.integrated_branch,
        polls=[_describe_poll(poll) for poll in polls],
        advanced_the_integrated_branch=False,
        note=(
            "milestone 3 builds and verifies the stack and never advances the integrated "
            "branch; a stack that builds cleanly is abandoned as "
            "INTEGRATED_BRANCH_ADVANCE_UNIMPLEMENTED and its members return to the queue"
        ),
    )


def _describe_poll(poll: RefineryPoll) -> dict[str, Any]:
    match poll:
        case Idle():
            return {"outcome": "idle"}
        case Unavailable(error=error):
            return {"outcome": "unavailable", "error": error}
        case Drained(run=run):
            return {
                "outcome": "drained",
                "batch_id": run.batch_id,
                "integrated": list(run.outcome.integrated),
                "parked": [
                    {
                        "request_id": isolation.request_id,
                        "cause": type(isolation.cause).__name__,
                        "stack_beneath": list(isolation.stack_beneath),
                    }
                    for isolation in run.outcome.isolated
                ],
                "returned_to_queue": list(getattr(run.outcome, "returned_to_queue", ())),
                "abandoned_because": getattr(getattr(run.outcome, "reason", None), "value", None),
            }
    raise AssertionError(f"unhandled refinery poll {poll!r}")


__all__ = [
    "Drained",
    "Idle",
    "Refinery",
    "RefineryPoll",
    "Unavailable",
    "run_refinery",
]
