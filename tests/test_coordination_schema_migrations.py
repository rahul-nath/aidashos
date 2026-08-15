# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest


def _load_coordination_module() -> Any:
    from local_first_agent_os.coordination import store

    return store


def test_the_pinned_schema_hash_matches_the_ddl_on_disk() -> None:
    """The one check that stands between a DDL edit and a silent bad migration.

    Every other test in this file builds its schema from the whole `.sql` file,
    so all of them pass whatever `SCHEMA_VERSION` says. That is what let version
    12's DDL grow two columns without the number moving: `_ensure_postgres_schema`
    returns early when applied == expected, so a database that already existed
    never ran the new statements, and the first real WorkUnit died on a column
    1133 green tests had proved was present.

    Read the failure as a question, not a defect. If the edit changed DDL, bump
    `SCHEMA_VERSION` and write the statements so they apply to an existing
    database; if it changed only comments, update the hash. Either way somebody
    looked, which is the whole job.
    """

    module = _load_coordination_module()

    assert module.postgres_schema_content_hash() == module.SCHEMA_CONTENT_HASH, (
        "agent_coordination_postgres_schema.sql changed without its pin being updated. "
        f"If the change was DDL, bump SCHEMA_VERSION (now {module.SCHEMA_VERSION}) and make "
        "the new statements safe to apply to an existing database. Then set "
        f"SCHEMA_CONTENT_HASH to {module.postgres_schema_content_hash()!r}."
    )


def test_a_migration_to_the_current_version_is_reachable_from_the_previous_one() -> None:
    """The schema file has to be applicable to a database, not only creatable.

    `_ensure_postgres_schema` runs the same file against an empty database and
    against one already at an older version, so every statement in it has to be
    idempotent. A bare `CREATE TABLE` or `ADD COLUMN` without its guard passes
    every fresh-schema test here and fails only on the upgrade nobody runs.
    """

    module = _load_coordination_module()
    ddl = module.load_postgres_schema_sql().lower()

    creates = ddl.count("create table")
    guarded_creates = ddl.count("create table if not exists")
    assert creates == guarded_creates, (
        f"{creates - guarded_creates} CREATE TABLE statement(s) lack IF NOT EXISTS, so "
        "applying this schema to an existing database would fail"
    )

    adds = ddl.count("add column")
    guarded_adds = ddl.count("add column if not exists")
    assert adds == guarded_adds, (
        f"{adds - guarded_adds} ADD COLUMN statement(s) lack IF NOT EXISTS, so applying "
        "this schema to an existing database would fail"
    )


def test_fresh_schema_contains_execution_checkpoint_tables(tmp_path: Path, monkeypatch) -> None:
    module = _load_coordination_module()
    module.set_root(str(tmp_path))

    connection = module.connect()
    try:
        tables = {
            row["table_name"]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            ).fetchall()
        }
    finally:
        connection.close()

    assert {
        "agent_execution_events",
        "agent_execution_artifacts",
        "agent_execution_checkpoints",
    } <= tables


def test_fresh_schema_carries_every_column_the_runtime_writes(tmp_path: Path, monkeypatch) -> None:
    """The schema script is the only source of truth for a column's existence.

    SQLite kept a repair pass that added missing columns to an existing file,
    because a file could predate a column with no version bump to notice. The
    versioned Postgres migration makes that impossible, so this asserts the
    property the repair pass used to guarantee.
    """

    module = _load_coordination_module()
    module.set_root(str(tmp_path))

    connection = module.connect()
    try:
        columns = {
            row["column_name"]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'dispatch_intents'"
            ).fetchall()
        }
    finally:
        connection.close()

    assert {
        "fanout",
        "allow_tiers",
        "reduce",
        "reducer_tier",
        "parent_intent_id",
        "intent_role",
        "checkpoint_id",
    } <= columns


def test_postgres_driver_owns_schema_statement_boundaries() -> None:
    module = _load_coordination_module()
    calls: list[tuple[str, bool]] = []

    class RecordingDriverConnection:
        def execute(self, script: str, *, prepare: bool) -> None:
            calls.append((script, prepare))

    connection: Any = module.PostgresConnection.__new__(module.PostgresConnection)
    connection._conn = RecordingDriverConnection()
    script = (
        "CREATE TABLE example (value TEXT);\n"
        "-- This semicolon; belongs to the comment.\n"
        "ALTER TABLE example ADD COLUMN other TEXT;"
    )

    connection.executescript(script)

    assert calls == [(script, False)]


# ---------------------------------------------------------------------------
# Postgres schema-version ownership
# ---------------------------------------------------------------------------

_PROBE_RELATION = "probe_relation"
_READ_VERSION = "read_version"
_TAKE_MIGRATION_LOCK = "take_migration_lock"
_RECORD_VERSION = "record_version"
_OTHER = "other"


def _statement_kind(sql: str) -> str:
    """Name the migration-protocol statements so traces read as a protocol."""

    normalized = " ".join(sql.split()).lower()
    if "to_regclass" in normalized:
        return _PROBE_RELATION
    if normalized.startswith("select version from coordination_schema_versions"):
        return _READ_VERSION
    if "pg_advisory_xact_lock" in normalized:
        return _TAKE_MIGRATION_LOCK
    if normalized.startswith("insert into coordination_schema_versions"):
        return _RECORD_VERSION
    return _OTHER


class _FakePostgresConnection:
    """A ConnectionLike that answers the version probes and records the rest.

    The database-side state is just the applied version, so a waiter that finds
    a different version after taking the lock is expressed by handing the fake
    the version the migration owner would have committed.
    """

    backend = "postgres"

    def __init__(
        self,
        applied_version: int | None,
        *,
        version_after_lock: int | None = None,
        fail_on: str | None = None,
    ) -> None:
        self._applied_version = applied_version
        self._version_after_lock = version_after_lock
        self._fail_on = fail_on
        self.trace: list[str] = []
        self.scripts: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, sql: str, params: Any = None) -> Any:
        kind = _statement_kind(sql)
        self.trace.append(kind)
        if kind == self._fail_on:
            raise RuntimeError(f"simulated failure during {kind}")
        if kind == _TAKE_MIGRATION_LOCK and self._version_after_lock is not None:
            self._applied_version = self._version_after_lock
        if kind == _RECORD_VERSION:
            self._applied_version = int(list(params)[1])
        return _FakeCursor(self._result_for(kind))

    def _result_for(self, kind: str) -> Any:
        if kind == _PROBE_RELATION:
            exists = self._applied_version is not None
            return {"relation": "coordination_schema_versions" if exists else None}
        if kind == _READ_VERSION:
            return {"version": self._applied_version}
        return None

    def executescript(self, script: str) -> None:
        self.trace.append("run_schema_script")
        if self._fail_on == "run_schema_script":
            raise RuntimeError("simulated failure during run_schema_script")
        self.scripts.append(script)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


class _FakeCursor:
    def __init__(self, row: Any) -> None:
        self._row = row

    def fetchone(self) -> Any:
        return self._row

    def fetchall(self) -> list[Any]:
        return [self._row] if self._row is not None else []


@pytest.fixture()
def postgres_schema_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the store at a Postgres URL without connecting to one."""

    module = _load_coordination_module()
    monkeypatch.setenv("AGENT_COORDINATION_BACKEND", "postgres")
    monkeypatch.setenv(
        "AGENT_COORDINATION_DATABASE_URL",
        "postgresql://unit-test/not-connected",
    )
    module._SCHEMA_READY.clear()
    yield module
    module._SCHEMA_READY.clear()


def test_current_durable_version_skips_the_migration_entirely(postgres_schema_env: Any) -> None:
    module = postgres_schema_env
    connection = _FakePostgresConnection(module.SCHEMA_VERSION)

    module.ensure_schema(connection)

    assert connection.trace == [_PROBE_RELATION, _READ_VERSION]
    assert connection.scripts == []
    assert connection.commits == 1


def test_stale_database_migrates_under_the_advisory_lock(postgres_schema_env: Any) -> None:
    module = postgres_schema_env
    connection = _FakePostgresConnection(module.SCHEMA_VERSION - 1)

    module.ensure_schema(connection)

    assert connection.trace[:3] == [_PROBE_RELATION, _READ_VERSION, _TAKE_MIGRATION_LOCK]
    assert connection.trace.index("run_schema_script") > connection.trace.index(
        _TAKE_MIGRATION_LOCK
    )
    assert connection.trace[-1] == _RECORD_VERSION
    assert connection.scripts == [module.load_postgres_schema_sql()]
    assert connection.commits == 1


def test_empty_database_migrates_and_records_the_version(postgres_schema_env: Any) -> None:
    module = postgres_schema_env
    connection = _FakePostgresConnection(None)

    module.ensure_schema(connection)

    assert connection.scripts == [module.load_postgres_schema_sql()]
    assert connection.trace.count(_RECORD_VERSION) == 1


def test_waiter_that_loses_the_race_does_not_repeat_the_migration(
    postgres_schema_env: Any,
) -> None:
    module = postgres_schema_env
    connection = _FakePostgresConnection(None, version_after_lock=module.SCHEMA_VERSION)

    module.ensure_schema(connection)

    assert "run_schema_script" not in connection.trace
    assert _RECORD_VERSION not in connection.trace
    assert connection.commits == 1


def test_database_newer_than_the_runtime_is_refused(postgres_schema_env: Any) -> None:
    module = postgres_schema_env
    connection = _FakePostgresConnection(module.SCHEMA_VERSION + 1)

    with pytest.raises(RuntimeError, match="newer than this runtime"):
        module.ensure_schema(connection)

    assert connection.scripts == []
    assert not module._SCHEMA_READY


def test_failed_migration_leaves_no_ready_marker(postgres_schema_env: Any) -> None:
    module = postgres_schema_env
    connection = _FakePostgresConnection(None, fail_on="run_schema_script")

    with pytest.raises(RuntimeError, match="run_schema_script"):
        module.ensure_schema(connection)

    assert not module._SCHEMA_READY


def test_transactions_do_not_open_a_connection_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reuse across transactions, which is now the pool's job rather than a thread's.

    Opening a connection per transaction is affordable against a local file and is
    not against a server: a single WorkUnit run performs about two hundred
    transactions, which used to mean two hundred connections.

    The old design bought that reuse with a per-thread cache held for the life of
    the process, which is what later exhausted `max_connections` on a server that
    simply stayed up. A pool keeps the reuse and drops the retention.
    """

    module = _load_coordination_module()
    module.set_root(str(tmp_path))
    opened = {"count": 0}
    real = module.PostgresConnection

    class Counting(real):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            opened["count"] += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(module, "PostgresConnection", Counting)
    try:
        for _ in range(5):
            with module.tx() as connection:
                connection.execute("SELECT 1")
        # Deliberately not asserting how many wrappers were made. That is an
        # implementation detail - `configure` makes one too - and pinning it is
        # what made the previous version of this test break on a change that kept
        # every property it existed to protect. Sockets are the scarce thing.
        assert sum(pool.get_stats().get("pool_size", 0) for pool in module._pools.values()) == 1
    finally:
        module.reset_connections()


def test_reset_closes_connections_opened_on_other_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker thread's connection is reachable for teardown.

    A thread-local was invisible from every other thread, so without a registry a
    connection opened by a pooled worker would outlive the database it points at
    with nothing able to close it. The pool is now that registry, and it is also
    what stops the worker from keeping the connection once its work is done.
    """

    import threading

    module = _load_coordination_module()
    module.set_root(str(tmp_path))

    def work() -> None:
        with module.tx() as connection:
            connection.execute("SELECT 1")

    worker = threading.Thread(target=work)
    worker.start()
    worker.join()

    def held() -> int:
        return sum(pool.get_stats().get("pool_size", 0) for pool in module._pools.values())

    assert held() == 1
    module.reset_connections()
    assert module._pools == {}
    assert held() == 0
