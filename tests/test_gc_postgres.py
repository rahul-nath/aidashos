# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The abandoned-work sweeps against real Postgres.

gc_ledger writes correlated-subquery UPDATEs that SQLite and Postgres do not
have to agree on, and the ledger these sweeps exist to repair is Postgres. The
rest of the GC suite runs on disposable SQLite, so without this file the only
Postgres execution of that SQL would be the first operator run against the live
ledger.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from postgres_support import point_store_at_database, postgres_admin_url
from psycopg import sql

from local_first_agent_os.coordination.execution import gc_ledger
from local_first_agent_os.coordination.store import ConnectionLike, tx

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("LOCAL_AGENT_RUN_POSTGRES_INTEGRATION") != "1",
        reason="set LOCAL_AGENT_RUN_POSTGRES_INTEGRATION=1 to run real Postgres tests",
    ),
]

_DAY = 86_400


def _statuses(c: ConnectionLike, table: str, key: str) -> dict[str, str]:
    """Read {id: status} from a table. Postgres rows are mappings, not tuples."""

    rows = c.execute(f"SELECT {key}, status FROM {table}").fetchall()  # noqa: S608 - fixed names
    return {row[key]: row["status"] for row in rows}


@pytest.fixture()
def postgres_ledger(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    admin_url = postgres_admin_url()
    database_name = f"local_agent_gc_{uuid.uuid4().hex}"
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    database_url = admin_url.rsplit("/", 1)[0] + f"/{database_name}"
    point_store_at_database(monkeypatch, database_url)
    try:
        yield
    finally:
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=%s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))


def test_every_abandoned_work_sweep_runs_on_postgres(postgres_ledger: None) -> None:
    """One row of each stuck kind, plus a live counterpart that must survive."""

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
        for task_id, session_id in (("held", "live"), ("dropped", "dead")):
            c.execute(
                "INSERT INTO saga_tasks(task_id, pow_wow_id, saga_id, task_name, description, "
                "assigned_session_id, status, created_at, updated_at) "
                "VALUES (?, 'p1', 's1', 'n', 'd', ?, 'CLAIMED', ?, ?)",
                (task_id, session_id, old, old),
            )
        for intent_id in ("orphaned", "working"):
            c.execute(
                "INSERT INTO dispatch_intents(intent_id, tier, kind, prompt, status, "
                "created_at, claimed_at) VALUES (?, 'junior', 'advisory', 'p', 'CLAIMED', ?, ?)",
                (intent_id, old, old),
            )
        c.execute(
            "INSERT INTO agent_execution_leases(lease_id, idempotency_key, intent_id, worker_id, "
            "status, timeout_seconds, lease_expires_at, command_json, compensation_json, "
            "created_at, heartbeat_at) "
            "VALUES ('live_lease', 'k', 'working', 'w', 'ACTIVE', 60, ?, '[]', '{}', ?, ?)",
            (t + 3600, old, t),
        )
        c.execute(
            "INSERT INTO agent_execution_leases(lease_id, idempotency_key, intent_id, worker_id, "
            "status, timeout_seconds, lease_expires_at, command_json, compensation_json, "
            "created_at, heartbeat_at) "
            "VALUES ('dead_lease', 'k2', NULL, 'w', 'ACTIVE', 60, ?, '[]', '{}', ?, ?)",
            (t - 10, old, old),
        )
        for event_id, status, claimed_at, created_at in (
            ("stale_pending", "PENDING", None, old),
            ("fresh_pending", "PENDING", None, t - 60),
            ("stalled_claim", "CLAIMED", old, old),
            ("fresh_claim", "CLAIMED", t - 30, old),
        ):
            c.execute(
                "INSERT INTO ledger_events(event_id, event_type, aggregate_type, aggregate_id, "
                "payload_json, status, attempts, created_at, claimed_at) "
                "VALUES (?, 'smoke', 'x', 'y', '{}', ?, 0, ?, ?)",
                (event_id, status, created_at, claimed_at),
            )

    swept = gc_ledger()["deleted"]

    assert swept["expired_execution_leases"] == 1
    assert swept["expired_ledger_events"] == 1
    assert swept["stalled_ledger_events"] == 1
    assert swept["abandoned_dispatch_intents"] == 1
    assert swept["abandoned_saga_tasks"] == 1

    with tx() as c:
        events = _statuses(c, "ledger_events", "event_id")
        intents = _statuses(c, "dispatch_intents", "intent_id")
        tasks = _statuses(c, "saga_tasks", "task_id")
    assert events == {
        "stale_pending": "ABANDONED",
        "fresh_pending": "PENDING",
        "stalled_claim": "ABANDONED",
        "fresh_claim": "CLAIMED",
    }
    assert intents == {"orphaned": "CANCELED", "working": "CLAIMED"}
    assert tasks == {"held": "CLAIMED", "dropped": "ABANDONED"}
