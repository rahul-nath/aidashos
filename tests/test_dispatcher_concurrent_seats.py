# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The dispatcher's per-tier seats: overlap where staffing allows it, serial
where it does not, and a failed pipeline hands its seat back.

The defect these tests pin (observed 2026-08-10): milestones 1 and 2 of one
WorkUnit submitted their dispatch intents in the same second and ran strictly
serially, because `dispatch_pending_intents` ran each claimed pipeline to its
terminal status before claiming again. Every PENDING intent is runnable by
construction - milestones submit intents only once their dependencies are
satisfied - so the fix is seats, not DAG knowledge: the loop claims up to each
tier's free seats and runs the claimed pipelines on a bounded worker pool.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from local_first_agent_os.coordination import (
    ClaimNextDispatchIntent,
    CompleteDispatchIntent,
    DispatchTerminalStatus,
)
from local_first_agent_os.coordination.availability import LedgerUnavailable
from local_first_agent_os.dispatcher import UNAVAILABLE_INTERVAL_SECONDS, LedgerDispatcher
from local_first_agent_os.pow_wow import run_coordination_command
from local_first_agent_os.settings import Settings


def _coord(root: Path, args: list[str]) -> dict:
    return run_coordination_command(args, root=root)


def _submit_two_senior_siblings(root: Path) -> tuple[str, str]:
    first = _coord(root, ["submit_dispatch_intent", "senior", "first sibling milestone"])
    second = _coord(root, ["submit_dispatch_intent", "senior", "second sibling milestone"])
    return first["intent_id"], second["intent_id"]


def test_two_same_tier_intents_with_two_seats_run_overlapped(tmp_path: Path) -> None:
    """With two senior seats, sibling intents claimed together run at once.

    The barrier is the proof: each pipeline blocks until the other has started,
    so both runners being inside the barrier at the same time is exactly "both
    pipelines start before either finishes". A serial loop breaks the barrier
    by timeout and the intents surface as FAILED rather than hanging the test.
    """

    root = tmp_path / "coord"
    first_id, second_id = _submit_two_senior_siblings(root)
    both_started = threading.Barrier(2)

    def runner(intent):
        try:
            both_started.wait(timeout=15.0)
        except threading.BrokenBarrierError:
            return (
                DispatchTerminalStatus.FAILED,
                None,
                "the sibling pipeline never started while this one ran",
            )
        return (DispatchTerminalStatus.DONE, f"ran {intent['prompt']}", None)

    dispatcher = LedgerDispatcher(
        runner,
        name="overlap-dispatcher",
        settings=Settings(coordination_root=root),
        seats={"senior": 2},
    )

    dispatched = dispatcher.dispatch_pending_intents(interval_seconds=0.05, max_polls=1)

    # One poll is one sweep over the claim lanes: both free seats were filled
    # before the loop drained, so a single poll dispatched both siblings.
    assert dispatched == 2
    assert {outcome.status for outcome in dispatcher.last_outcomes} == {"DONE"}
    done = _coord(root, ["list_dispatch_intents", "--status", "DONE"])["intents"]
    assert {row["intent_id"] for row in done} == {first_id, second_id}


def test_one_seat_preserves_the_serial_loop(tmp_path: Path) -> None:
    """seats=1 is today's behavior: FIFO claims, one pipeline at a time."""

    root = tmp_path / "coord"
    first_id, second_id = _submit_two_senior_siblings(root)
    windows: list[tuple[str, float, float]] = []
    windows_lock = threading.Lock()

    def runner(intent):
        started = time.monotonic()
        time.sleep(0.05)
        finished = time.monotonic()
        with windows_lock:
            windows.append((intent["intent_id"], started, finished))
        return (DispatchTerminalStatus.DONE, None, None)

    dispatcher = LedgerDispatcher(
        runner,
        name="serial-dispatcher",
        settings=Settings(coordination_root=root),
        seats={"senior": 1},
    )

    dispatched = dispatcher.dispatch_pending_intents(interval_seconds=0.0, max_polls=3)

    assert dispatched == 2
    assert [entry[0] for entry in windows] == [first_id, second_id]
    first_window, second_window = windows
    assert first_window[2] <= second_window[1], (
        "with one seat the second pipeline must not start before the first finishes"
    )


def test_a_failed_pipeline_hands_its_seat_back(tmp_path: Path) -> None:
    """A pipeline crash fails its intent and frees the seat for the next claim."""

    root = tmp_path / "coord"
    first_id, second_id = _submit_two_senior_siblings(root)

    def runner(intent):
        if intent["intent_id"] == first_id:
            raise RuntimeError("pipeline died")
        return (DispatchTerminalStatus.DONE, None, None)

    dispatcher = LedgerDispatcher(
        runner,
        name="release-dispatcher",
        settings=Settings(coordination_root=root),
        seats={"senior": 1},
    )

    dispatched = dispatcher.dispatch_pending_intents(interval_seconds=0.0, max_polls=3)

    assert dispatched == 2
    statuses = {outcome.intent_id: outcome.status for outcome in dispatcher.last_outcomes}
    assert statuses == {first_id: "FAILED", second_id: "DONE"}
    failed = _coord(root, ["list_dispatch_intents", "--status", "FAILED"])["intents"]
    assert failed[0]["intent_id"] == first_id
    assert "pipeline died" in failed[0]["error"]
    done = _coord(root, ["list_dispatch_intents", "--status", "DONE"])["intents"]
    assert done[0]["intent_id"] == second_id


def test_explicit_seat_map_cannot_invent_a_seat_for_an_unstaffed_scoped_tier() -> None:
    dispatcher = LedgerDispatcher(
        lambda _intent: (DispatchTerminalStatus.DONE, None, None),
        name="unstaffed-dispatcher",
        tier="staff",
        seats={"senior": 2},
    )

    with pytest.raises(ValueError, match="has no seats"):
        dispatcher.dispatch_pending_intents(interval_seconds=0.0, max_polls=1)


def test_an_outage_after_a_partial_sweep_waits_before_retrying() -> None:
    intent = {
        "intent_id": "first-claim",
        "tier": "senior",
        "prompt": "first sibling",
    }
    claim_calls = 0
    waits: list[float] = []

    def coordination(command):
        nonlocal claim_calls
        if isinstance(command, ClaimNextDispatchIntent):
            claim_calls += 1
            if claim_calls == 1:
                return {"ok": True, "intent": intent}
            raise LedgerUnavailable("coordination database is unavailable")
        assert isinstance(command, CompleteDispatchIntent)
        return {
            "ok": True,
            "intent_id": command.intent_id,
            "status": command.status.value,
        }

    dispatcher = LedgerDispatcher(
        lambda _intent: (DispatchTerminalStatus.DONE, None, None),
        name="outage-dispatcher",
        seats={"senior": 2},
    )
    dispatcher._coord = coordination  # type: ignore[method-assign]
    dispatcher._idle_wait = (  # type: ignore[method-assign]
        lambda _in_flight, seconds: waits.append(seconds)
    )

    dispatched = dispatcher.dispatch_pending_intents(interval_seconds=0.0, max_polls=1)

    assert dispatched == 1
    assert dispatcher.last_deferred == []
    assert claim_calls == 2
    assert waits == [UNAVAILABLE_INTERVAL_SECONDS]
