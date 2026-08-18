# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Postgres schema ownership under real concurrency.

The coordination CLI runs as many short-lived subprocesses. Each one starts with
an empty process cache, so before the durable version marker existed every one
of them re-ran the whole DDL script and they deadlocked against each other on
relation locks. That failure only reproduces with a real server and real
concurrent connections, so it gets its own integration file.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest
from postgres_support import point_store_at_database, postgres_admin_url
from psycopg import sql

from local_first_agent_os.coordination import store

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("LOCAL_AGENT_RUN_POSTGRES_INTEGRATION") != "1",
        reason="set LOCAL_AGENT_RUN_POSTGRES_INTEGRATION=1 to run real Postgres tests",
    ),
]

_CONCURRENT_MIGRATORS = 8
# Well above a healthy cold start; far below the point where a lock wait would
# have to be called a hang.
_MIGRATION_TIMEOUT_SECONDS = 120

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def disposable_postgres_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    admin_url = postgres_admin_url()
    database_name = f"local_agent_schema_{uuid.uuid4().hex}"
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
    database_url = admin_url.rsplit("/", 1)[0] + f"/{database_name}"
    point_store_at_database(monkeypatch, database_url)
    store._SCHEMA_READY.clear()
    try:
        yield database_url
    finally:
        store._SCHEMA_READY.clear()
        # Pools are keyed by URL, so a pool aimed at a dropped database would
        # otherwise sit in `_pools` for the rest of the session holding sockets
        # to a server object that no longer exists.
        store.reset_connections()
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=%s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name)))


_MIGRATING_CHILD = textwrap.dedent(
    """
    from local_first_agent_os.coordination import store

    connection = store.connect()
    try:
        connection.execute("SELECT 1 FROM sagas LIMIT 1").fetchall()
    finally:
        connection.close()
    """
)


def _run_concurrent_migrators(database_url: str, count: int) -> list[subprocess.CompletedProcess]:
    child_env = {
        **os.environ,
        "AGENT_COORDINATION_BACKEND": "postgres",
        "AGENT_COORDINATION_DATABASE_URL": database_url,
    }
    children = [
        subprocess.Popen(  # noqa: S603 - fixed argv, interpreter from sys.executable
            [sys.executable, "-c", _MIGRATING_CHILD],
            cwd=_REPO_ROOT,
            env=child_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(count)
    ]
    results = []
    for child in children:
        stdout, stderr = child.communicate(timeout=_MIGRATION_TIMEOUT_SECONDS)
        results.append(subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr))
    return results


def _durable_versions(database_url: str) -> list[tuple[str, int]]:
    with psycopg.connect(database_url) as connection:
        return [
            (row[0], row[1])
            for row in connection.execute(
                "SELECT component, version FROM coordination_schema_versions ORDER BY component"
            ).fetchall()
        ]


def test_simultaneous_first_connections_migrate_without_deadlocking(
    disposable_postgres_url: str,
) -> None:
    results = _run_concurrent_migrators(disposable_postgres_url, _CONCURRENT_MIGRATORS)

    failures = [result for result in results if result.returncode != 0]
    assert not failures, failures[0].stderr[-2000:]
    assert _durable_versions(disposable_postgres_url) == [
        (store.POSTGRES_SCHEMA_COMPONENT, store.SCHEMA_VERSION)
    ]


def test_a_migrated_database_runs_no_ddl_on_the_next_process(
    disposable_postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = store.connect()
    first.close()

    # A fresh process starts with an empty cache; only the durable version can
    # keep it away from the DDL.
    store._SCHEMA_READY.clear()
    monkeypatch.setattr(
        store,
        "load_postgres_schema_sql",
        lambda: pytest.fail("a database already at the current version must not re-run the DDL"),
    )

    second = store.connect()
    try:
        assert second.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
    finally:
        second.close()


def test_a_database_newer_than_the_runtime_refuses_to_connect(
    disposable_postgres_url: str,
) -> None:
    first = store.connect()
    first.close()
    _set_durable_version(disposable_postgres_url, store.SCHEMA_VERSION + 1)

    store._SCHEMA_READY.clear()
    with pytest.raises(RuntimeError, match="newer than this runtime"):
        store.connect()


def _set_durable_version(database_url: str, version: int) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute(
            "UPDATE coordination_schema_versions SET version=%s WHERE component=%s",
            (version, store.POSTGRES_SCHEMA_COMPONENT),
        )


def test_connecting_to_a_stale_database_performs_no_ddl(disposable_postgres_url: str) -> None:
    """The incident, reproduced against a real server and refused.

    The unit-level version of this guard runs against a fake connection and
    proves the protocol; this one proves the database. A stale marker in front of
    a schema that is physically current is exactly the shape a runtime ahead of
    its ledger presents, and the old code answered it by running the whole DDL
    script against a database several other processes were working in.

    The read after the refusal is the part that matters. It is not enough that
    `connect` raised - a migration that happened and then reported an error would
    still have reshaped production - so this asks the catalog whether anything
    moved.
    """

    first = store.connect()
    first.close()
    _set_durable_version(disposable_postgres_url, store.SCHEMA_VERSION - 1)
    store._SCHEMA_READY.clear()

    with pytest.raises(RuntimeError, match="needs migration"):
        store.connect()

    with psycopg.connect(disposable_postgres_url) as connection:
        version = connection.execute(
            "SELECT version FROM coordination_schema_versions WHERE component=%s",
            (store.POSTGRES_SCHEMA_COMPONENT,),
        ).fetchone()
    assert version == (store.SCHEMA_VERSION - 1,), (
        "connecting migrated the ledger it was only supposed to read"
    )


def test_a_refused_connection_does_not_consume_the_pool(disposable_postgres_url: str) -> None:
    """Sixteen refusals must not become the outage's own misdiagnosis.

    The refusal used to live in the pool's `configure`, where psycopg_pool
    swallowed it, retried in the background, and handed the caller `PoolTimeout`
    after thirty seconds; `_diagnosed_checkout_failure` then probed a healthy
    server and reported that all sixteen connections were checked out. Two agents
    chased that phantom for an afternoon.

    So the properties are both: the operator reads the real reason, and reading it
    repeatedly costs nothing. A connection returned to the pool on the way out is
    what makes the second true.
    """

    first = store.connect()
    first.close()
    _set_durable_version(disposable_postgres_url, store.SCHEMA_VERSION - 1)
    store._SCHEMA_READY.clear()

    for _ in range(store.postgres_pool_max_size() + 4):
        with pytest.raises(RuntimeError, match="needs migration"):
            store.connect()

    pool = store._pools[store._pool_key(disposable_postgres_url, None)]
    assert pool.get_stats().get("pool_size", 0) <= 1


def test_an_explicit_migration_restores_a_stale_database(disposable_postgres_url: str) -> None:
    """The command an operator runs after reading the refusal, end to end.

    Also the reason the refusal is not simply a wall: the schema has to be
    reachable again, in one typed step, by the person who decided it should move.
    """

    first = store.connect()
    first.close()
    _set_durable_version(disposable_postgres_url, store.SCHEMA_VERSION - 1)
    store._SCHEMA_READY.clear()

    result = store.migrate_postgres_schema()

    assert result["previous_version"] == store.SCHEMA_VERSION - 1
    assert result["version"] == store.SCHEMA_VERSION
    assert result["migrated"] is True
    assert result["created"] is False

    reconnected = store.connect()
    try:
        assert reconnected.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
    finally:
        reconnected.close()


def test_an_explicit_migration_reports_a_database_that_needed_nothing(
    disposable_postgres_url: str,
) -> None:
    """Already current and just-moved-your-shared-ledger must read differently."""

    first = store.connect()
    first.close()
    store._SCHEMA_READY.clear()

    result = store.migrate_postgres_schema()

    assert result["previous_version"] == store.SCHEMA_VERSION
    assert result["migrated"] is False


def test_the_schema_state_report_touches_nothing(disposable_postgres_url: str) -> None:
    """`first-run-check.sh` asks this about a database it must not change."""

    first = store.connect()
    first.close()
    _set_durable_version(disposable_postgres_url, store.SCHEMA_VERSION - 1)
    store._SCHEMA_READY.clear()

    state = store.coordination_schema_state()

    assert state["state"] == "MIGRATION_REQUIRED"
    assert state["applied_version"] == store.SCHEMA_VERSION - 1
    assert state["runtime_version"] == store.SCHEMA_VERSION
    with psycopg.connect(disposable_postgres_url) as connection:
        assert connection.execute(
            "SELECT version FROM coordination_schema_versions WHERE component=%s",
            (store.POSTGRES_SCHEMA_COMPONENT,),
        ).fetchone() == (store.SCHEMA_VERSION - 1,)


def test_the_schema_state_report_names_an_empty_database(disposable_postgres_url: str) -> None:
    state = store.coordination_schema_state()

    assert state["state"] == "ABSENT"
    assert state["applied_version"] is None


def test_a_credential_never_reaches_a_schema_message(disposable_postgres_url: str) -> None:
    """These messages get pasted into handoffs and now travel to a public repo.

    The URL is `postgresql://user:password@host/db`, and the question the outage
    turned on was *which database*, not which password. So the description keeps
    the host and the database name and drops the userinfo.
    """

    first = store.connect()
    first.close()
    _set_durable_version(disposable_postgres_url, store.SCHEMA_VERSION - 1)
    store._SCHEMA_READY.clear()

    with pytest.raises(RuntimeError) as refusal:
        store.connect()

    parsed = urlparse(disposable_postgres_url)
    assert parsed.password
    assert parsed.password not in str(refusal.value)
    assert (parsed.hostname or "") in str(refusal.value)
    assert str(parsed.path).lstrip("/") in str(refusal.value)
