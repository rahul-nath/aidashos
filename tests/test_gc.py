# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import time
from pathlib import Path

from local_first_agent_os.coordination.store import ConnectionLike, tx
from local_first_agent_os.pow_wow import (
    resolve_coordination_events_path,
    run_coordination_command,
)

_NOTE = "INSERT INTO notes(scope, message, created_at) VALUES ('x', ?, ?)"
_INTENT = (
    "INSERT INTO dispatch_intents(intent_id, tier, kind, prompt, status, created_at) "
    "VALUES (?, 'junior', 'advisory', 'p', ?, ?)"
)
_LEASE = """
    INSERT INTO agent_execution_leases(
        lease_id, idempotency_key, intent_id, worker_id, status, timeout_seconds,
        lease_expires_at, command_json, compensation_json, created_at,
        heartbeat_at, completed_at
    ) VALUES (?, ?, ?, 'worker', ?, 60, ?, '[]', '{}', ?, ?, ?)
"""
_LEDGER_EVENT = """
    INSERT INTO ledger_events(
        event_id, event_type, aggregate_type, aggregate_id, payload_json,
        status, attempts, created_at
    ) VALUES (?, 'smoke', 'dispatch_intent', ?, '{}', ?, 0, ?)
"""

_DAY = 86_400


def _statuses(connection: ConnectionLike, query: str, key: str) -> dict[str, str]:
    return {row[key]: row["status"] for row in connection.execute(query).fetchall()}


def _init_schema(root: Path) -> None:
    run_coordination_command(["list_dispatch_intents"], root=root)  # creates the schema


def test_gc_ledger_prunes_old_records(tmp_path: Path) -> None:
    root = tmp_path / "coord"
    _init_schema(root)
    t = time.time()
    with tx() as c:
        c.execute(
            "INSERT INTO sessions(session_id, agent_name, created_at, last_heartbeat_at) "
            "VALUES ('s', 'a', ?, ?)",
            (t, t),
        )
        c.execute(_NOTE, ("old", t - 9999))
        c.execute(_NOTE, ("new", t))
        c.execute(_INTENT, ("done", "DONE", t - 9999))  # terminal + old -> prunable
        c.execute(_INTENT, ("leased_terminal", "DONE", t - 9999))
        c.execute(_INTENT, ("leased_active", "DONE", t - 9999))
        c.execute(_INTENT, ("leased_expired", "DONE", t - 9999))
        c.execute(_INTENT, ("pend", "PENDING", t - 9999))  # old but PENDING -> must survive
        c.execute(
            _LEASE,
            (
                "lease_terminal",
                "key_terminal",
                "leased_terminal",
                "COMPLETED",
                t - 9999,
                t - 9999,
                t - 9999,
                t - 9999,
            ),
        )
        c.execute(
            _LEASE,
            (
                "lease_active",
                "key_active",
                "leased_active",
                "ACTIVE",
                t + 3600,
                t - 9999,
                t,
                None,
            ),
        )
        c.execute(
            _LEASE,
            (
                "lease_expired",
                "key_expired",
                "leased_expired",
                "ACTIVE",
                t - 10,
                t - 9999,
                t - 9999,
                None,
            ),
        )
        c.execute(_LEDGER_EVENT, ("event_processed", "done", "PROCESSED", t - 9999))
        c.execute(_LEDGER_EVENT, ("event_failed", "done", "FAILED", t - 9999))
        c.execute(_LEDGER_EVENT, ("event_pending", "pend", "PENDING", t - 9999))

    events = resolve_coordination_events_path(root=root)
    # The ledger lives in Postgres, so nothing has created the event log's
    # directory yet; production makes it on first emit.
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text(
        json.dumps({"ts": _iso(t - 9999), "event_type": "old"})
        + "\n"
        + json.dumps({"ts": _iso(t), "event_type": "new"})
        + "\n",
        encoding="utf-8",
    )

    res = run_coordination_command(["gc", "--retention-seconds", "3600"], root=root)
    deleted = res["deleted"]
    assert deleted["expired_execution_leases"] == 1
    assert deleted["notes"] == 1
    assert deleted["agent_execution_leases"] == 1
    assert deleted["dispatch_intents"] == 2  # terminal unleased + terminal lease pruned
    assert deleted["ledger_events"] == 2  # processed/failed only, not pending
    assert deleted["events"] == 1

    with tx() as c:
        assert {r["message"] for r in c.execute("SELECT message FROM notes").fetchall()} == {"new"}
        assert {
            r["intent_id"] for r in c.execute("SELECT intent_id FROM dispatch_intents").fetchall()
        } == {
            "leased_active",
            "leased_expired",
            "pend",
        }
        assert {
            r["lease_id"]
            for r in c.execute("SELECT lease_id FROM agent_execution_leases").fetchall()
        } == {
            "lease_active",
            "lease_expired",
        }
        assert (
            c.execute(
                "SELECT status FROM agent_execution_leases WHERE lease_id='lease_expired'"
            ).fetchone()["status"]
            == "TIMED_OUT"
        )
        assert "event_pending" in {
            r["event_id"] for r in c.execute("SELECT event_id FROM ledger_events").fetchall()
        }


def test_gc_ledger_abandons_pending_events_past_their_expiry(tmp_path: Path) -> None:
    """An unclaimed event past its deadline is dead, exactly like an expired lease.

    Terminalizing runs without a retention window, so periodic maintenance stops
    the outbox from accumulating rows no consumer will ever drain.
    """

    root = tmp_path / "coord"
    _init_schema(root)
    t = time.time()
    with tx() as c:
        c.execute(_LEDGER_EVENT, ("stale", "a", "PENDING", t - 16 * _DAY))
        c.execute(_LEDGER_EVENT, ("fresh", "b", "PENDING", t - 60))

    res = run_coordination_command(["gc"], root=root)
    assert res["deleted"]["expired_ledger_events"] == 1

    with tx() as c:
        statuses = _statuses(c, "SELECT event_id, status FROM ledger_events", "event_id")
        assert statuses == {"stale": "ABANDONED", "fresh": "PENDING"}
        abandoned = c.execute(
            "SELECT error, processed_at FROM ledger_events WHERE event_id='stale'"
        ).fetchone()
    assert abandoned["error"]  # the terminal fact records why, as the lease sweep does
    assert abandoned["processed_at"] is not None


def test_gc_ledger_abandons_events_a_consumer_claimed_and_never_resolved(
    tmp_path: Path,
) -> None:
    """A CLAIMED row is as stuck as a PENDING one when the consumer dies mid-drain."""

    root = tmp_path / "coord"
    _init_schema(root)
    t = time.time()
    with tx() as c:
        c.execute(_LEDGER_EVENT, ("stalled", "a", "CLAIMED", t - 16 * _DAY))
        c.execute(
            "UPDATE ledger_events SET claimed_at=? WHERE event_id='stalled'", (t - 16 * _DAY,)
        )
        c.execute(_LEDGER_EVENT, ("just_claimed", "b", "CLAIMED", t - 16 * _DAY))
        c.execute("UPDATE ledger_events SET claimed_at=? WHERE event_id='just_claimed'", (t - 30,))

    res = run_coordination_command(["gc"], root=root)
    assert res["deleted"]["stalled_ledger_events"] == 1

    with tx() as c:
        statuses = _statuses(c, "SELECT event_id, status FROM ledger_events", "event_id")
    # The fresh claim is old by created_at and young by claimed_at: the claim is
    # what stalled, so the claim is what the window measures.
    assert statuses == {"stalled": "ABANDONED", "just_claimed": "CLAIMED"}


def test_gc_ledger_abandons_claimed_intents_only_without_a_live_lease(tmp_path: Path) -> None:
    """A lease is stronger evidence than the intent's own age.

    An intent claimed long ago whose worker still holds a live lease is real
    work in progress, not debris.
    """

    root = tmp_path / "coord"
    _init_schema(root)
    t = time.time()
    old = t - 16 * _DAY
    with tx() as c:
        c.execute(_INTENT, ("orphaned", "CLAIMED", old))
        c.execute(_INTENT, ("working", "CLAIMED", old))
        c.execute(_INTENT, ("dead_worker", "CLAIMED", old))
        c.execute("UPDATE dispatch_intents SET claimed_at=?", (old,))
        c.execute(_LEASE, ("live", "k_live", "working", "ACTIVE", t + 3600, old, t, None))
        # Expired, so the lease sweep terminalizes it earlier in the same pass
        # and this intent then reads as unheld.
        c.execute(_LEASE, ("gone", "k_gone", "dead_worker", "ACTIVE", t - 10, old, old, None))

    res = run_coordination_command(["gc"], root=root)
    assert res["deleted"]["abandoned_dispatch_intents"] == 2

    with tx() as c:
        rows = _statuses(c, "SELECT intent_id, status FROM dispatch_intents", "intent_id")
        outcome = c.execute(
            "SELECT outcome FROM dispatch_intents WHERE intent_id='orphaned'"
        ).fetchone()["outcome"]
    assert rows == {"orphaned": "CANCELED", "working": "CLAIMED", "dead_worker": "CANCELED"}
    # CANCELED keeps quorum settlement working; the reason lives in outcome.
    assert outcome == "ORPHANED_CLAIM_EXPIRED"


def test_gc_ledger_abandons_tasks_whose_session_stopped_heartbeating(tmp_path: Path) -> None:
    """A task's holder is a session, and a heartbeating session means live work."""

    root = tmp_path / "coord"
    _init_schema(root)
    t = time.time()
    old = t - 16 * _DAY
    with tx() as c:
        for session_id, heartbeat in (("live", t), ("dead", old)):
            c.execute(
                "INSERT INTO sessions(session_id, agent_name, created_at, last_heartbeat_at) "
                "VALUES (?, 'a', ?, ?)",
                (session_id, old, heartbeat),
            )
        c.execute(
            "INSERT INTO sagas(saga_id, goal, created_at, updated_at) VALUES ('s1', 'g', ?, ?)",
            (old, old),
        )
        c.execute(
            "INSERT INTO pow_wows(pow_wow_id, saga_id, stage, goal, created_at, updated_at) "
            "VALUES ('p1', 's1', 'BUILD', 'g', ?, ?)",
            (old, old),
        )
        for task_id, session_id in (("held", "live"), ("dropped", "dead"), ("unowned", None)):
            c.execute(
                "INSERT INTO saga_tasks(task_id, pow_wow_id, saga_id, task_name, description, "
                "assigned_session_id, status, created_at, updated_at) "
                "VALUES (?, 'p1', 's1', 'n', 'd', ?, 'CLAIMED', ?, ?)",
                (task_id, session_id, old, old),
            )

    res = run_coordination_command(["gc"], root=root)
    assert res["deleted"]["abandoned_saga_tasks"] == 2

    with tx() as c:
        rows = _statuses(c, "SELECT task_id, status FROM saga_tasks", "task_id")
    assert rows == {"held": "CLAIMED", "dropped": "ABANDONED", "unowned": "ABANDONED"}


def test_gc_ledger_expiry_window_is_configurable(tmp_path: Path) -> None:
    root = tmp_path / "coord"
    _init_schema(root)
    t = time.time()
    with tx() as c:
        c.execute(_LEDGER_EVENT, ("hour_old", "a", "PENDING", t - 3600))

    run_coordination_command(["gc", "--abandoned-after-seconds", "60"], root=root)

    with tx() as c:
        status = c.execute("SELECT status FROM ledger_events WHERE event_id='hour_old'").fetchone()
    assert status["status"] == "ABANDONED"


def test_gc_ledger_retention_collects_abandoned_events(tmp_path: Path) -> None:
    """Abandoning makes the row terminal; the existing retention window removes it."""

    root = tmp_path / "coord"
    _init_schema(root)
    t = time.time()
    with tx() as c:
        c.execute(_LEDGER_EVENT, ("stale", "a", "PENDING", t - 16 * _DAY))
        c.execute(_LEDGER_EVENT, ("fresh", "b", "PENDING", t - 60))

    res = run_coordination_command(
        ["gc", "--retention-seconds", str(_DAY)],
        root=root,
    )
    assert res["deleted"]["expired_ledger_events"] == 1
    assert res["deleted"]["ledger_events"] == 1

    with tx() as c:
        remaining = {
            r["event_id"] for r in c.execute("SELECT event_id FROM ledger_events").fetchall()
        }
    assert remaining == {"fresh"}


def test_gc_ledger_without_retention_leaves_dated_rows_alone(tmp_path: Path) -> None:
    root = tmp_path / "coord"
    _init_schema(root)
    with tx() as c:
        c.execute(_NOTE, ("ancient", 0))
    res = run_coordination_command(["gc"], root=root)
    assert "notes" not in res["deleted"]  # retention window omitted -> notes untouched
    with tx() as c:
        assert c.execute("SELECT COUNT(*) AS count FROM notes").fetchone()["count"] == 1


def _iso(ts: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, UTC).isoformat()


def test_gc_ledger_keeps_terminal_children_of_live_quorums(tmp_path: Path) -> None:
    root = tmp_path / "coord"
    _init_schema(root)
    t = time.time()
    child_insert = (
        "INSERT INTO dispatch_intents("
        "  intent_id, tier, kind, prompt, status, created_at,"
        "  parent_intent_id, intent_role"
        ") VALUES (?, 'junior', 'advisory', 'p', ?, ?, ?, ?)"
    )
    with tx() as c:
        # A live quorum whose child already finished: the child is pending
        # evidence for the reducer, not garbage.
        c.execute(_INTENT, ("live_parent", "PENDING", t - 9999))
        c.execute(
            "UPDATE dispatch_intents SET intent_role='quorum', fanout=2 "
            "WHERE intent_id='live_parent'"
        )
        c.execute(child_insert, ("live_child", "DONE", t - 9999, "live_parent", "child"))
        # A settled quorum: its terminal children are prunable.
        c.execute(_INTENT, ("done_parent", "DONE", t - 9999))
        c.execute(
            "UPDATE dispatch_intents SET intent_role='quorum', fanout=2 "
            "WHERE intent_id='done_parent'"
        )
        c.execute(child_insert, ("done_child", "DONE", t - 9999, "done_parent", "child"))

    run_coordination_command(["gc", "--retention-seconds", "3600"], root=root)

    with tx() as c:
        remaining = {
            row["intent_id"]
            for row in c.execute("SELECT intent_id FROM dispatch_intents").fetchall()
        }
    assert "live_parent" in remaining  # PENDING never pruned
    assert "live_child" in remaining  # guarded by the live parent
    assert "done_parent" not in remaining
    assert "done_child" not in remaining
