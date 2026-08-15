# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import time
import uuid
from collections.abc import Iterator
from functools import cache
from pathlib import Path

import postgres_server
import psycopg
import pytest
from psycopg import sql

# A live collector must not silently turn an ordinary unit-test run into a
# memory-profiling workload.  Targeted profiler tests can opt back in with an
# explicit environment override.
os.environ.setdefault("LOCAL_AGENT_MEMORY_PROFILING_ENABLED", "false")

# The unit suite is not a DBOS integration lane. `@dbos_step` and `@dbos_workflow`
# bind at import time and reach the system database as soon as a decorated
# function is called, so a developer whose `.env` enables DBOS would see the
# WorkUnit tests fail with "System database accessed before DBOS was launched"
# while the same tests pass on a machine with no `.env` at all. This has to be set
# before the package is imported, which is why it is here rather than in a
# fixture. `setdefault` leaves an explicit opt-in working.
os.environ.setdefault("LOCAL_AGENT_USE_DBOS", "false")

from local_first_agent_os.coordination import store
from local_first_agent_os.runtime import AppRuntime, build_runtime
from local_first_agent_os.settings import Settings, get_settings

REPO_CONFIGS = Path(__file__).resolve().parent.parent / "configs"

# The `postgres-test` service in docker-compose.yml, not the durable `postgres`
# one. Deliberately a different server rather than a different database on the
# same one: this container's data directory is a tmpfs, so nothing a test writes
# can outlive it, and nothing production wrote is in reach of a test.
DEFAULT_TEST_POSTGRES = postgres_server.ManagedPostgres(
    url="postgresql://postgres:postgres@127.0.0.1:5433/local_agent",
    compose_service="postgres-test",
)

# A schema is dropped by the fixture that made it, which a killed run never gets
# to do. Nothing in the catalog records when a schema was created, so the name
# carries the answer and the sweep below reads it back.
_SCHEMA_NAME_PATTERN = re.compile(r"^test_(?P<created_at>\d{10})_[0-9a-f]{12}$")
_ORPHAN_MAX_AGE_SECONDS = 3600


@cache
def suite_postgres_source() -> postgres_server.PostgresSource:
    """The server every test's schema is created on, started if it is not up.

    Running the suite is the request; a stopped database is an obstacle to it, not
    a question for the developer. So this starts the container the repository
    already declares rather than printing a command to paste. It runs once per
    session because the answer cannot change mid-run.

    When starting is not possible the run ends here, with the reason on screen,
    rather than as a psycopg traceback three test files later.
    """

    # Only LOCAL_AGENT_TEST_DATABASE_URL, deliberately. This used to fall back to
    # LOCAL_AGENT_COORDINATION_DATABASE_URL, which is the *production* ledger: on
    # any machine with a populated .env the suite ran inside the database holding
    # real sagas, and every schema a killed run failed to drop was left there.
    # A variable that names where production writes must not decide where tests
    # write.
    configured = os.environ.get("LOCAL_AGENT_TEST_DATABASE_URL")
    source: postgres_server.PostgresSource = (
        postgres_server.ExternalPostgres(url=configured) if configured else DEFAULT_TEST_POSTGRES
    )
    try:
        normalized = postgres_server.normalized_postgres_url(source.url)
        source = dataclasses.replace(source, url=normalized)
        postgres_server.ensure_running(source)
    except postgres_server.PostgresUnavailable as exc:
        pytest.exit(f"{exc}", returncode=1)
    _drop_orphaned_schemas(source.url)
    return source


def suite_postgres_url() -> str:
    return suite_postgres_source().url


def _drop_orphaned_schemas(admin_url: str) -> None:
    """Remove per-test schemas an earlier run was killed before dropping.

    Only schemas old enough that no live run could own them, so a second pytest
    running concurrently keeps its own. Names that predate the timestamped format
    are left alone for the same reason: an unreadable age is not an old one.
    """

    cutoff = time.time() - _ORPHAN_MAX_AGE_SECONDS
    with psycopg.connect(admin_url, autocommit=True) as connection:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name LIKE 'test\\_%'"
            ).fetchall()
        ]
        for name in names:
            match = _SCHEMA_NAME_PATTERN.match(name)
            if match is None or int(match.group("created_at")) > cutoff:
                continue
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


@pytest.fixture(autouse=True)
def _isolate_coordination_backend(monkeypatch: pytest.MonkeyPatch):
    """Give every test its own schema on the real Postgres server.

    The suite used to run on a SQLite adapter, which cannot express the primitives
    production depends on: `FOR UPDATE` became the empty string, `FOR UPDATE SKIP
    LOCKED` became no clause at all, and `pg_advisory_xact_lock` had no equivalent.
    Assertions about concurrent claiming were running against queries with no
    locking in them, which reads as coverage while testing nothing. The deadlock
    that stopped a production project was a Postgres-only failure mode no SQLite
    test could have produced.

    Isolation is a schema rather than a file. Creating one costs a few
    milliseconds, it is dropped on the way out, and a test that leaks state leaks
    it into a namespace nothing else can see.

    The schema lives on the tmpfs `postgres-test` server, so the 35-table
    coordination schema applies in about 24ms rather than the 81ms it costs on a
    disk-backed volume, and a leak that survives teardown dies with the container.
    """

    admin_url = suite_postgres_url()
    # The timestamp is what `_drop_orphaned_schemas` reads; keep the two in step.
    schema = f"test_{int(time.time())}_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    monkeypatch.setenv("LOCAL_AGENT_COORDINATION_BACKEND", "postgres")
    monkeypatch.setenv("AGENT_COORDINATION_BACKEND", "postgres")
    monkeypatch.setenv("AGENT_COORDINATION_DATABASE_URL", admin_url)
    monkeypatch.setenv("LOCAL_AGENT_COORDINATION_DATABASE_URL", admin_url)
    monkeypatch.setenv("AGENT_COORDINATION_SCHEMA", schema)
    get_settings.cache_clear()
    # The store keeps one connection per thread, so a test must not inherit the
    # previous test's connection to the previous test's schema.
    store.reset_connections()
    try:
        yield
    finally:
        store.reset_connections()
        get_settings.cache_clear()
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


@pytest.fixture()
def work_unit_ledger(tmp_path: Path) -> Iterator[Path]:
    """The coordination ledger for WorkUnit tests.

    Isolation now comes from the per-test schema the autouse fixture creates, so
    this only supplies the repository root that non-ledger paths still use. It used
    to point the store at a SQLite file, which is what made every locking assertion
    in these tests run against a query with no lock clause in it.
    """

    store.set_root(str(tmp_path))
    yield tmp_path
    store.set_root(None)


@pytest.fixture()
def postgres_ledger(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """A disposable Postgres coordination ledger, for the locking lane.

    Lives here rather than in a helper module so pytest resolves it by name;
    importing a fixture shadows it, which is what ruff objects to.
    """

    from postgres_support import fresh_postgres_ledger

    with fresh_postgres_ledger(monkeypatch) as target:
        yield target


@pytest.fixture()
def runtime(tmp_path: Path) -> AppRuntime:
    test_configs = tmp_path / "configs"
    test_configs.mkdir(parents=True, exist_ok=True)
    shutil.copy(REPO_CONFIGS / "pi_prompts.toml", test_configs / "pi_prompts.toml")
    # Build from explicit data so tests ignore the developer's repo-root .env.
    settings = Settings.model_validate(
        {
            "database_url": f"sqlite:///{tmp_path / 'test.sqlite3'}",
            "artifact_root": tmp_path / "artifacts",
            "spool_dir": tmp_path / "spool",
            "session_context_export_dir": tmp_path / "session-contexts",
            "config_dir": test_configs,
            "mock_models": True,
            "use_dbos": False,
        }
    )
    app_runtime = build_runtime(settings)
    return app_runtime


@pytest.fixture(autouse=True)
def _pin_access_posture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the suite enforcing, whatever posture this machine is left in.

    `LOCAL_AGENT_ACCESS_POSTURE=observing` in a developer's `.env` is a normal
    thing to set for a manual run, and `Settings` reads `.env`. Without this
    fixture that setting reaches the suite and every assertion that the gate
    refuses something passes for the wrong reason - the gate allowed it and
    logged that it would have refused.

    Seventeen tests demonstrated exactly that the first time the posture existed.
    A test suite whose security assertions turn green because of an untracked
    file on one machine is worse than not having them, so the posture is pinned
    here and a test that wants `OBSERVING` sets it explicitly.
    """

    monkeypatch.setenv("LOCAL_AGENT_ACCESS_POSTURE", "enforcing")
    get_settings.cache_clear()
