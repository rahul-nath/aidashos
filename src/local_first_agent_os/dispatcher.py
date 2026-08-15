# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Ledger-driven dispatcher — the reactor that fuses the event-driven and
imperative worlds.

Producers (ASR triggers, gemma keyword scans, sagas, agents) write *dispatch
intents* to the coordination ledger; this reactor claims PENDING intents,
runs them through an injected `runner`, and records the outcome. `/saga` is the
imperative door (push a goal); this is the autonomous door (an event pulls a
task) — same ledger, same executors, two triggers.

Design notes (per the team's advanced-software-design principles):
- Keep-your-secrets: the dispatcher does NOT know how to run an agent. It takes
  a `runner: IntentRunner` callback (injected by the daemon/workflow layer,
  which owns the runtime + executor). Swap the runner without touching the loop.
- Representable/valid: `DispatchOutcome` is a sum type — a poll either dispatched
  one intent (with a terminal status) or was idle. No ambiguous in-between.
- The PENDING->CLAIMED claim is atomic in the ledger, so N concurrent reactors
  never double-run one intent (see claim_next_dispatch_intent).
- Concurrency is seats, not knowledge: every PENDING intent is runnable by
  construction (milestones submit intents only once their dependencies are
  satisfied), so the loop needs no DAG. It claims up to the free seats each
  tier has and runs every claimed pipeline on a bounded worker pool. The seat
  counts belong to staffing (`staffing.dispatch_seat_counts`); this module only
  spends them.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import json
import logging
import time
import traceback
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

from .coordination import (
    ClaimNextDispatchIntent,
    CompleteDispatchIntent,
    CoordinationCommand,
    DispatchTerminalStatus,
    DispatchTier,
)
from .coordination.availability import ledger_unavailable
from .lifecycle_failure_harness import (
    LifecycleTransitionPoint,
    reach_lifecycle_transition,
)
from .pow_wow import run_coordination_command
from .progress_events import emit_progress
from .settings import Settings

logger = logging.getLogger(__name__)

# A runner turns a claimed intent (dict from the ledger) into a terminal result.
IntentResult = tuple[Literal["DONE", "FAILED"], str | None, str | None]  # status, result, error
IntentRunner = Callable[[Mapping[str, Any]], IntentResult]


@dataclass(frozen=True)
class Dispatched:
    """One intent was claimed and run to a terminal status."""

    intent_id: str
    tier: str
    status: Literal["DONE", "FAILED"]
    source: str | None = None
    target_project_id: str | None = None
    milestone_id: str | None = None


@dataclass(frozen=True)
class Idle:
    """No PENDING intent was available to claim."""


@dataclass(frozen=True)
class Unavailable:
    """The ledger could not be reached on this pass.

    Distinct from `Idle`, which is a fact about the queue. This is a fact about
    the database, and the two used to be indistinguishable in the other
    direction: an unreachable ledger ended the process rather than reporting
    anything at all.
    """

    error: str


DispatchOutcome = Dispatched | Idle | Unavailable


@dataclass(frozen=True)
class _ClaimLane:
    """One tier's claim lane: what to claim as, and how many may run at once.

    `tier=None` is the unscoped lane - it claims the oldest PENDING intent of
    any tier, which is the historical single-seat loop's behavior and remains
    the shape a dispatcher constructed without seats gets.
    """

    tier: str | None
    seats: int


# Longer than the poll interval, for the reasons `DEFAULT_UNAVAILABLE_INTERVAL_SECONDS`
# gives in `work_units.enqueue_drainer`. The two resident loops wait out the same
# outage and there is no reason for them to disagree about how long a database
# takes to come back.
UNAVAILABLE_INTERVAL_SECONDS = 15.0


_TRACEBACK_LIMIT: Final = 8000


def _runner_crash_payload(intent_id: str, exc: BaseException) -> str:
    """The traceback for a runner that died, in the shape readers already parse.

    A crash here used to write `result=None`, so the only surviving trace of a
    defect in our own code was one exception line in the `error` column. Every
    reader of a settled intent expects `dispatch_runner_result.v1`, so writing
    the traceback in any other shape would be evidence nothing knows how to
    open. It carries `result_origin: runner_crash` rather than impersonating a
    run: no agent produced this, and a reader must not count it as one.

    Bounded, because a traceback is diagnostic text in a durable row and a deep
    recursion would otherwise write a megabyte of frames into the ledger.
    """

    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    if len(trace) > _TRACEBACK_LIMIT:
        trace = f"{trace[:_TRACEBACK_LIMIT]}..."
    return json.dumps(
        {
            "schema_version": "dispatch_runner_result.v1",
            "result_origin": "runner_crash",
            "intent_id": intent_id,
            "run_result": {
                "status": "FAILED",
                "output_summary": f"{type(exc).__name__}: {exc}",
                "risks": [f"dispatcher runner raised {type(exc).__name__}: {exc}"],
                "tasks": [],
                "traceback": trace,
            },
        },
        sort_keys=True,
    )


class LedgerDispatcher:
    def __init__(
        self,
        runner: IntentRunner,
        *,
        name: str = "dispatcher",
        tier: str | None = None,
        settings: Settings | None = None,
        seats: Mapping[str, int] | None = None,
        tier_claimable: Callable[[str | None], bool] | None = None,
    ) -> None:
        self.runner = runner
        self.name = name
        self.tier = tier  # None = claim any tier; set to fan out per-tier reactors
        self.settings = settings
        # Whether the bench can staff a tier *right now*, asked once per sweep.
        #
        # Injected rather than computed, for the reason at the top of this
        # module: the dispatcher spends seats and does not know what a harness
        # is. "Is there anyone to run this" is a staffing question, and a loop
        # that answered it itself would have to learn about quotas, providers,
        # and cooldowns to do it.
        #
        # None means always claimable, which is every caller that predates this
        # and the whole historical behaviour.
        self.tier_claimable = tier_claimable
        # Per-tier concurrent seat counts, keyed by tier value. None keeps the
        # historical loop: one unscoped seat, strictly serial across all tiers.
        # The mapping's keys are the claimable tiers - a tier absent from it has
        # no seats, which is truthful for an unstaffed tier: claiming work the
        # bench cannot staff would fail it rather than leave it for a dispatcher
        # that can.
        self.seats = dict(seats) if seats is not None else None
        self.last_outcomes: list[Dispatched] = []

    def _coord(self, command: CoordinationCommand) -> dict[str, Any]:
        return run_coordination_command(command, settings=self.settings)

    def poll_once(self) -> DispatchOutcome:
        """Claim the next PENDING intent (if any), run it, record the outcome.

        An unreachable ledger is reported rather than raised, so that the loop
        around this survives a database restart. Every other failure propagates:
        a loop that swallowed a defect would spin on it forever while saying the
        database was down.

        An outage between running an intent and recording its outcome loses the
        record, not the work: the claim's lease expires, the intent returns to
        PENDING, and it is claimed again. That is the same at-least-once bargain
        every other interruption here makes, and it is why the ledger's writes
        carry idempotency keys.
        """

        try:
            return self._poll_once()
        except Exception as exc:
            if not ledger_unavailable(exc):
                raise
            logger.error("%s could not reach the coordination ledger: %s", self.name, exc)
            return Unavailable(error=f"{type(exc).__name__}: {exc}")

    def _poll_once(self) -> DispatchOutcome:
        intent = self._claim_next(self.tier)
        if intent is None:
            return Idle()
        return self._settle(intent)

    def _claim_next(self, tier: str | None) -> Mapping[str, Any] | None:
        """Atomically claim the oldest PENDING intent, or None when the queue is idle."""

        claim_command = ClaimNextDispatchIntent(
            claimed_by=self.name,
            tier=DispatchTier(tier) if tier is not None else None,
        )
        intent = self._coord(claim_command).get("intent")
        if intent is None:
            return None

        reach_lifecycle_transition(
            LifecycleTransitionPoint.AFTER_INTENT_CLAIMED,
            intent_id=str(intent["intent_id"]),
            tier=str(intent["tier"]),
            target_project_id=(
                str(intent["target_project_id"]) if intent.get("target_project_id") else None
            ),
        )
        emit_progress(
            (
                f"claimed intent {intent['intent_id']} for {intent['tier']} execution"
                + (
                    f" against {intent['target_project_id']}"
                    if intent.get("target_project_id")
                    else ""
                )
            ),
            phase="intent_claimed",
            intent_id=intent["intent_id"],
            tier=intent["tier"],
            target_project_id=intent.get("target_project_id"),
        )
        return intent

    def _settle(self, intent: Mapping[str, Any]) -> Dispatched:
        """Run one claimed intent's pipeline and record its terminal outcome."""

        try:
            status, result, error = self.runner(intent)
        except Exception as exc:  # noqa: BLE001 - a runner crash fails the intent, not the reactor
            status, result, error = (
                "FAILED",
                _runner_crash_payload(str(intent["intent_id"]), exc),
                f"{type(exc).__name__}: {exc}",
            )

        self._coord(
            CompleteDispatchIntent(
                intent_id=intent["intent_id"],
                status=DispatchTerminalStatus(status),
                result=result,
                error=error,
            )
        )
        emit_progress(
            f"intent {intent['intent_id']} reached terminal status {status}",
            phase="intent_completed",
            intent_id=intent["intent_id"],
            status=status,
            tier=intent["tier"],
        )
        raw_payload = intent.get("payload")
        if isinstance(raw_payload, str):
            try:
                raw_payload = json.loads(raw_payload)
            except json.JSONDecodeError:
                raw_payload = {}
        payload = raw_payload if isinstance(raw_payload, Mapping) else {}
        return Dispatched(
            intent_id=intent["intent_id"],
            tier=intent["tier"],
            status=status,
            source=str(intent.get("source") or "") or None,
            target_project_id=(
                str(intent.get("target_project_id") or payload.get("target_project_id") or "")
                or None
            ),
            milestone_id=(
                str(intent.get("milestone_id") or payload.get("milestone_id") or "") or None
            ),
        )

    def _claim_lanes(self) -> tuple[_ClaimLane, ...]:
        """The tiers this dispatcher claims for, each with its seat count.

        No seats mapping is the historical loop: one unscoped seat. A scoped
        dispatcher keeps its scope and takes its own tier's seat count. An
        unscoped dispatcher with seats gets one lane per staffed tier, sorted
        for a deterministic sweep order.
        """

        if self.seats is None:
            return (_ClaimLane(tier=self.tier, seats=1),)
        if self.tier is not None:
            lanes = (_ClaimLane(tier=self.tier, seats=int(self.seats.get(self.tier, 0))),)
        else:
            lanes = tuple(
                _ClaimLane(tier=tier, seats=int(count))
                for tier, count in sorted(self.seats.items())
            )
        for lane in lanes:
            if lane.tier is not None:
                DispatchTier(lane.tier)  # an unknown tier fails here, not on first claim
            if lane.seats < 0:
                raise ValueError(f"tier {lane.tier!r} has a negative seat count: {lane.seats}")
        return lanes

    def _free_seats(
        self,
        lanes: tuple[_ClaimLane, ...],
        in_flight: Mapping[concurrent.futures.Future[Dispatched], str | None],
    ) -> dict[str | None, int]:
        busy = Counter(in_flight.values())
        return {lane.tier: lane.seats - busy[lane.tier] for lane in lanes}

    def _claim_free_seats(
        self,
        pool: concurrent.futures.ThreadPoolExecutor,
        lanes: tuple[_ClaimLane, ...],
        free: dict[str | None, int],
        in_flight: dict[concurrent.futures.Future[Dispatched], str | None],
    ) -> tuple[int, str | None]:
        """Claim intents into free seats; (claims made, unavailable error or None).

        Claims happen here on the loop's own thread - the atomic PENDING->CLAIMED
        token in the ledger is the only ordering that matters - and only the
        claimed pipeline runs on a worker. The submit captures the current
        context so a lifecycle-failure harness or progress projection installed
        around the loop reaches the worker too.
        """

        claimed = 0
        for lane in lanes:
            if self.tier_claimable is not None and not self.tier_claimable(lane.tier):
                # Leave the intent PENDING rather than claiming work nothing can
                # run. This is the whole retry mechanism for a spent quota: the
                # row keeps its place, this loop is already re-sweeping on its
                # interval, and the intent is claimed on the first sweep after
                # the bench reports a harness back. No reaper, no scheduler, and
                # no wake-up to miss.
                #
                # The alternative, which this replaces, was to claim and dispatch
                # into a harness known to be spent - "rather than stranding a run
                # the operator door already admitted". A queued intent is not
                # stranded; what stranded work was spending its three attempts
                # rediscovering the same refusal and then needing an override.
                #
                # Skipping the lane rather than zeroing its seats is deliberate:
                # `_free_seats` stays an honest statement of capacity, and the
                # caller falls through to its normal idle wait instead of
                # spinning on a pass where every seat looked occupied.
                continue
            while free[lane.tier] > 0:
                try:
                    intent = self._claim_next(lane.tier)
                except Exception as exc:
                    if not ledger_unavailable(exc):
                        raise
                    logger.error("%s could not reach the coordination ledger: %s", self.name, exc)
                    return claimed, f"{type(exc).__name__}: {exc}"
                if intent is None:
                    break  # this lane's queue is idle
                context = contextvars.copy_context()
                future = pool.submit(context.run, self._settle, intent)
                in_flight[future] = lane.tier
                free[lane.tier] -= 1
                claimed += 1
        return claimed, None

    def _collect(
        self,
        in_flight: dict[concurrent.futures.Future[Dispatched], str | None],
        done: set[concurrent.futures.Future[Dispatched]],
    ) -> int:
        """Fold finished pipelines into `last_outcomes`; their seats free up here.

        A pipeline that lost the ledger mid-settle is logged and dropped rather
        than counted: its claim lease lapses, the intent returns to PENDING, and
        it is claimed again - the same at-least-once bargain `poll_once`
        documents. Any other exception is a defect and propagates, exactly as a
        defect in the serial loop always has.
        """

        collected = 0
        for future in done:
            in_flight.pop(future)
            try:
                outcome = future.result()
            except Exception as exc:
                if not ledger_unavailable(exc):
                    raise
                logger.error(
                    "%s lost the coordination ledger while settling an intent; "
                    "its claim lease will lapse and the intent will be re-claimed: %s",
                    self.name,
                    exc,
                )
                continue
            self.last_outcomes.append(outcome)
            collected += 1
        return collected

    def _idle_wait(
        self,
        in_flight: dict[concurrent.futures.Future[Dispatched], str | None],
        seconds: float,
    ) -> None:
        """Wait out an idle or unavailable pass without sleeping through a finish.

        With pipelines in flight this waits on them, so a completion wakes the
        loop to refill the freed seat immediately. With nothing in flight it is
        a plain sleep, which is also what keeps the outage tests' patch of
        `time.sleep` honest.
        """

        if in_flight:
            concurrent.futures.wait(list(in_flight), timeout=seconds)
        else:
            time.sleep(seconds)

    def dispatch_pending_intents(
        self,
        *,
        interval_seconds: float = 2.0,
        max_polls: int | None = None,
    ) -> int:
        """Claim up to the free seats per tier and run each claimed intent's
        pipeline on a bounded worker pool. Returns the number of intents
        dispatched to a terminal status.

        A poll is one sweep over the claim lanes. `max_polls` bounds the sweeps
        (None = forever); an idle sweep waits `interval_seconds`. A pass in
        which every seat is occupied is not a poll - nothing could have been
        claimed, so the loop waits for a pipeline to finish without spending
        the budget. On exit the loop drains: every claimed intent settles
        before the count is returned.

        Without a `seats` mapping this degenerates to one unscoped seat, which
        is the strictly serial loop this method has always been.
        """

        dispatched = 0
        self.last_outcomes = []
        lanes = self._claim_lanes()
        total_seats = sum(lane.seats for lane in lanes)
        if total_seats < 1:
            raise ValueError(f"{self.name} has no seats to dispatch with: seats={self.seats!r}")
        polls = 0
        in_flight: dict[concurrent.futures.Future[Dispatched], str | None] = {}
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=total_seats,
            thread_name_prefix=f"{self.name}-pipeline",
        )
        try:
            while max_polls is None or polls < max_polls:
                free = self._free_seats(lanes, in_flight)
                if not any(count > 0 for count in free.values()):
                    done, _ = concurrent.futures.wait(
                        list(in_flight),
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    dispatched += self._collect(in_flight, done)
                    continue
                polls += 1
                claimed, unavailable = self._claim_free_seats(pool, lanes, free, in_flight)
                done, _ = concurrent.futures.wait(list(in_flight), timeout=0)
                dispatched += self._collect(in_flight, done)
                if unavailable is not None:
                    self._idle_wait(in_flight, max(interval_seconds, UNAVAILABLE_INTERVAL_SECONDS))
                    continue
                if claimed:
                    continue  # drain the queue before sleeping
                self._idle_wait(in_flight, interval_seconds)
            while in_flight:
                done, _ = concurrent.futures.wait(
                    list(in_flight),
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                dispatched += self._collect(in_flight, done)
            return dispatched
        finally:
            # On a defect this blocks until running pipelines finish rather than
            # abandoning their subprocesses; queued-but-unstarted work cannot
            # exist because claims never exceed free seats.
            pool.shutdown(wait=True)
