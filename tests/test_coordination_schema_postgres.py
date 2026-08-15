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
    with psycopg.connect(disposable_postgres_url, autocommit=True) as connection:
        connection.execute(
            "UPDATE coordination_schema_versions SET version=%s WHERE component=%s",
            (store.SCHEMA_VERSION + 1, store.POSTGRES_SCHEMA_COMPONENT),
        )

    store._SCHEMA_READY.clear()
    with pytest.raises(RuntimeError, match="newer than this runtime"):
        store.connect()
