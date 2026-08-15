# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A disposable Postgres *database* for tests that manage schemas themselves.

The suite as a whole gets a private schema per test, which is cheaper and
sufficient for everything that only needs isolation. This exists for the few
tests that create and inspect schemas as their subject matter, and so cannot run
inside one: proving that two schemas in one database are independent ledgers is
not something a test confined to a single schema can do.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import postgres_server
import psycopg
import pytest
from conftest import suite_postgres_url
from psycopg import sql

from local_first_agent_os.coordination import store

SKIP_UNLESS_INTEGRATION = pytest.mark.skipif(
    os.environ.get("LOCAL_AGENT_RUN_POSTGRES_INTEGRATION") != "1",
    reason="set LOCAL_AGENT_RUN_POSTGRES_INTEGRATION=1 to run real Postgres tests",
)


def postgres_admin_url() -> str:
    """A server this test may create and drop databases on.

    An explicit override wins; otherwise this is the same server the rest of the
    suite already started, so there is nothing extra to configure or to bring up.

    The one name for this, deliberately. Three integration files used to read
    `LOCAL_AGENT_POSTGRES_TEST_ADMIN_URL` and fall back to a hardcoded
    `127.0.0.1:5432/postgres`, which is the durable server: running the
    integration lane created and dropped real databases beside the coordination
    ledger. A default that reaches production is worse than no default, and two
    names for one server is how the two got to disagree.
    """

    override = os.environ.get("LOCAL_AGENT_POSTGRES_ADMIN_URL")
    if override is None:
        return suite_postgres_url()
    resolved = postgres_server.normalized_postgres_url(override)
    # An operator named this server, so this repository does not start it.
    postgres_server.ensure_running(postgres_server.ExternalPostgres(url=resolved))
    return resolved


def point_store_at_database(monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    """Send the coordination store to `database_url` and that database's own schema.

    The autouse fixture in conftest gives every test a named schema on the suite's
    shared server. A test that creates a whole disposable database wants the
    default schema inside it, and leaving `AGENT_COORDINATION_SCHEMA` pointing at
    the other server's schema name fails as `InvalidSchemaName` on the first
    write - which reads like a connection problem rather than the configuration
    one it is.

    Clearing it lives here rather than in each fixture because all three
    disposable-database fixtures forgot it in the same way.
    """

    monkeypatch.setenv("AGENT_COORDINATION_BACKEND", "postgres")
    monkeypatch.setenv("AGENT_COORDINATION_DATABASE_URL", database_url)
    monkeypatch.delenv("AGENT_COORDINATION_SCHEMA", raising=False)


@contextmanager
def fresh_postgres_ledger(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Point the coordination store at a brand new database, then drop it.

    The process-level schema cache is cleared on both sides of the test, because it
    is keyed by database URL and would otherwise report a fresh database as already
    migrated.
    """

    admin_url = postgres_admin_url()
    database = f"coordination_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    target = admin_url.rsplit("/", 1)[0] + f"/{database}"
    point_store_at_database(monkeypatch, target)
    store._SCHEMA_READY.clear()
    store.reset_connections()
    try:
        yield target
    finally:
        # The cached connection points at a database that is about to not exist.
        store.reset_connections()
        store._SCHEMA_READY.clear()
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database))
            )
