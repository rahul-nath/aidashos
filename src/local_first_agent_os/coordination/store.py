# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Database, schema, path, and serialization primitives for coordination."""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import logging
import os
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from .availability import LedgerUnavailable
from .contracts import CoordinationCommandName
from .failures import DurableFailureError, expected_failure
from .outcomes import FailureCategory

ROOT_OVERRIDE: Path | None = None

# ---------------------------------------------------------------------------
# Ambiguity gate thresholds (Ouroboros-inspired)
# ---------------------------------------------------------------------------
AMBIGUITY_THRESHOLDS = {
    "goal_clarity": 0.85,
    "constraints_clarity": 0.80,
    "success_criteria_clarity": 0.80,
    "max_unresolved_critical": 0,
}

# Stagnation: if two consecutive pow-wow cycles produce < this fraction of new
# artifact bytes relative to the previous cycle total, declare stagnation.
STAGNATION_MIN_DELTA_RATIO = 0.10


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def set_root(path: str | None) -> None:
    """Point the ledger at a different repository root.

    This changes which database subsequent calls talk to, so any connection this
    thread had cached is now to the wrong one and is dropped.
    """

    global ROOT_OVERRIDE
    ROOT_OVERRIDE = Path(path).expanduser().resolve() if path else None
    reset_connections()


def repo_root(start: str | None = None) -> Path:
    if ROOT_OVERRIDE:
        return ROOT_OVERRIDE
    env = os.environ.get("AGENT_COORDINATION_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    p = Path(start or os.getcwd()).resolve()
    for x in (p, *p.parents):
        if (x / ".git").exists():
            return x
    return p


def coord_dir() -> Path:
    d = repo_root() / ".agent_coordination"
    d.mkdir(parents=True, exist_ok=True)
    return d


def events_path() -> Path:
    return coord_dir() / "events.jsonl"


def now() -> float:
    return time.time()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def normalize_path(path: str) -> str:
    root = repo_root()
    p = Path(path).expanduser()
    p = p.resolve() if p.is_absolute() else (root / p).resolve()
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def normalize_paths(paths: Iterable[str]) -> list[str]:
    out, seen = [], set()
    for p in paths:
        n = normalize_path(p)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


# ---------------------------------------------------------------------------
# Database bootstrap
# ---------------------------------------------------------------------------

# Schema lives in a sibling .sql file (single source of truth) and is applied
# once per database, not on every connection. Bump SCHEMA_VERSION when the DDL
# changes. The applied version is persisted in coordination_schema_versions,
# and migrations serialize under one transaction-scoped advisory lock.
#
# 13: `dispatch_intents.idempotency_key` and `.notify_workflow_id`. The DDL for
# both was added at 12 without moving this number, so `_ensure_postgres_schema`
# saw applied == expected and returned before running it. Every test builds a
# fresh schema from the whole file and therefore had the columns; only a
# database that already existed was missing them, which is why a green suite sat
# on top of a coordination ledger that could not record a dispatch intent.
# The tell was `UndefinedColumn: column "idempotency_key" ... does not exist`
# raised inside `execute_milestone_workflow`, on the first real WorkUnit run.
#
# 14: `milestone_executions.failure_class`. A BLOCKED milestone had no durable
# answer to "did this spend an attempt?": `_status_for_failure` knew the
# `FailureClass` and the write threw it away, keeping only a free-text
# `failure_code` written in four places. So a retry budget could only guess from
# a string, and guessing wrong on an approval gate fails a milestone that never
# ran. The class is now persisted beside the code.
#
# 15: `dispatch_intents.permitted_capabilities`. The compiled plan computes what
# each milestone's agent may do, and it stopped at the agent's prompt: the spawn
# decision was made from `is_review`, a boolean derived partly from the task's
# name, and every task it called not-a-review was launched with the sandbox off.
#
# 16: `tool_permission_requests.expires_at`. A GRANTED row was forever. A grant
# is a statement about a piece of work and work ends, so an authorization that
# outlives its pow-wow is one nobody remembers making and nobody will remove.
#
# 17: the `claims` table leaves the schema. Nothing reached it - dispatched
# agents write with their own harness tools and never touch a coordination
# surface - so the lock it offered was never taken and two comments in the tree
# claimed an enforcement that did not exist. Archived with its reasoning in
# potential_directions/file_claims/. Existing databases keep the table: it is
# harmless, and dropping rows is not what a version bump should do behind an
# operator's back.
#
# 18: `integration_requests`, the refinery's queue. An approved agent branch had
# nowhere durable to wait: `DispatchPromotionState.MERGE_APPROVED -> MERGED` is
# a transition nothing performs, and integration was a `git merge --ff-only`
# string printed for a human, so N milestones running concurrently produced N
# branches from one base with no order and no combination ever tested. This is
# the row that makes the queue a query rather than an inspection of git, which
# is invariant 11 of docs/completed/refinery_integration_queue_design.md.
# `integration_batches` is deliberately not here yet: its columns are decided by
# the driver that writes them, which is milestone 3, and a table shaped before
# its writer exists is the unconsulted-mechanism defect this design was written
# to avoid.
#
# 19: monotonic `consumed_tokens` counters on sagas and pow-wows. These are the
# authoritative budget-accounting counters; the legacy `tokens_used` field is
# not. A budget is a limit, not mutable usage state, so extending it cannot
# erase spend history. The aggregate deliberately combines measured and
# estimated usage; source and estimation provenance remain ledger-event detail.
#
# The columns reached the shared ledger ahead of this constant, from a dispatched
# agent's worktree, and every checkout on disk then refused to connect because
# `_assert_supported_schema_version` saw database=19 against runtime=18. Only the
# DDL and this number are taken here. The feature that reads these columns is
# still on `agent/4ce002fa-...-dispatch_58b7b6b9_code-3e607727` and merges when
# its staff review passes, not as part of restoring the runtime.
#
# 20: normalized frontier usage and Codex continuation projections. The event
# ledger remains the immutable evidence, while these tables make budget reads
# and compatible thread lookup indexed operations on the execution hot path.
SCHEMA_VERSION = 20

# The DDL that `SCHEMA_VERSION` names, pinned by content.
#
# Version 13 exists because version 12's DDL was edited and this number was not,
# and nothing could see the difference: every test builds a fresh schema from the
# whole file, so the file and the number only have to agree on a database that
# already exists. That is not a testable surface, it is production.
#
# So the agreement is pinned here instead. Editing the `.sql` changes this hash
# and fails `test_the_pinned_schema_hash_matches_the_ddl_on_disk`, which is a
# tripwire rather than a checksum: it does not know whether an edit needs a
# migration, it only refuses to let the edit pass unnoticed. Two answers are
# correct when it fires, and the point is that someone chooses between them:
#
# - The edit changed DDL. Bump `SCHEMA_VERSION`, make sure the statements are
#   written so they apply to an existing database (`ADD COLUMN IF NOT EXISTS`),
#   then update this hash.
# - The edit changed only comments or whitespace. Update this hash alone.
#
# Recomputed with `shasum -a 256 agent_coordination_postgres_schema.sql`.
SCHEMA_CONTENT_HASH = "563e6e76c614f1e5116f68ad9e4c1857a9efbc72e9a64a4c7c228aeaddbf1651"

# The two ways a runtime and a database can disagree about the schema, as stable
# `failure.v1` codes. Both are operator conditions: the ledger is reachable and
# answering, and what is wrong is that the two of them are at different versions.
SCHEMA_MIGRATION_REQUIRED = "COORDINATION_SCHEMA_MIGRATION_REQUIRED"
SCHEMA_NEWER_THAN_RUNTIME = "COORDINATION_SCHEMA_NEWER_THAN_RUNTIME"

POSTGRES_SCHEMA_COMPONENT = "agent_coordination"
# Stable, signed 64-bit key reserved for this component's schema migration.
POSTGRES_SCHEMA_ADVISORY_LOCK_KEY = 5_861_874_679_801_903_697
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
POSTGRES_SCHEMA_PATH = _REPOSITORY_ROOT / "agent_coordination_postgres_schema.sql"
_POSTGRES_SCHEMA_SQL: str | None = None
_SCHEMA_READY: set[str] = set()

CoordinationBackend = Literal["postgres"]


def load_postgres_schema_sql() -> str:
    """Read (and cache) the Postgres coordination schema DDL from disk."""
    global _POSTGRES_SCHEMA_SQL
    if _POSTGRES_SCHEMA_SQL is None:
        _POSTGRES_SCHEMA_SQL = POSTGRES_SCHEMA_PATH.read_text(encoding="utf-8")
    return _POSTGRES_SCHEMA_SQL


def postgres_schema_content_hash() -> str:
    """The hash of the DDL on disk, to compare against `SCHEMA_CONTENT_HASH`.

    Hashes the file's bytes rather than a normalized form of them. Normalizing
    would mean deciding which edits are allowed to go unnoticed, and the whole
    reason this exists is that such a decision was made implicitly once already.
    """

    return hashlib.sha256(POSTGRES_SCHEMA_PATH.read_bytes()).hexdigest()


def coordination_backend() -> CoordinationBackend:
    raw = os.environ.get("AGENT_COORDINATION_BACKEND") or os.environ.get(
        "LOCAL_AGENT_COORDINATION_BACKEND"
    )
    if raw is None:
        # Direct operator invocations of this script do not inherit the
        # runtime shell's exports. Reuse the project's Settings loader so the
        # checked-in/local `.env` selects the same ledger as pi-daemon.
        with contextlib.suppress(Exception):
            from local_first_agent_os.settings import get_settings

            raw = get_settings().coordination_backend
    backend = (raw or "postgres").strip().lower()
    if backend != "postgres":
        raise ValueError(
            f"AGENT_COORDINATION_BACKEND must be 'postgres', got {raw!r}. The SQLite "
            "adapter was removed: it could not express FOR UPDATE, SKIP LOCKED, or "
            "advisory locks, so a ledger on it had none of the guarantees the "
            "coordination code is written against."
        )
    return backend  # type: ignore[return-value]


def postgres_schema() -> str | None:
    """The Postgres schema holding the coordination tables, when it is not the default.

    Set by a test harness that wants an isolated namespace on a shared server.
    Production leaves it unset and uses the connection's default search path.
    """

    raw = os.environ.get("AGENT_COORDINATION_SCHEMA")
    if raw is None:
        return None
    schema = raw.strip()
    if not schema:
        return None
    if not schema.replace("_", "").isalnum():
        raise ValueError(f"AGENT_COORDINATION_SCHEMA must be a bare identifier, got {raw!r}")
    return schema


def postgres_database_url() -> str:
    url = (
        os.environ.get("AGENT_COORDINATION_DATABASE_URL")
        or os.environ.get("LOCAL_AGENT_COORDINATION_DATABASE_URL")
        or os.environ.get("LOCAL_AGENT_DATABASE_URL")
    )
    if not url:
        with contextlib.suppress(Exception):
            from local_first_agent_os.settings import get_settings

            settings = get_settings()
            url = settings.coordination_database_url or settings.database_url
    if not url:
        raise ValueError(
            "Postgres coordination backend requires AGENT_COORDINATION_DATABASE_URL "
            "or LOCAL_AGENT_DATABASE_URL"
        )
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def postgres_target_description() -> str:
    """Name the ledger this process points at, without its credentials.

    Which database is being talked to is the question the 2026-08-17 outage
    turned on, so every schema refusal answers it. The URL carries a password,
    though, and these messages are printed to terminals, pasted into handoffs,
    and now travel to a public repository, so the host, port, and database name
    are taken and the userinfo is left behind.
    """

    with contextlib.suppress(Exception):
        parsed = urlparse(postgres_database_url())
        host = parsed.hostname or "localhost"
        port = f":{parsed.port}" if parsed.port else ""
        database = (parsed.path or "").lstrip("/") or "?"
        return f"{host}{port}/{database} (schema {postgres_schema() or 'default'})"
    return "the configured coordination ledger"


class CursorLike(Protocol):
    rowcount: int
    lastrowid: int | None

    def fetchone(self) -> Any: ...
    def fetchall(self) -> list[Any]: ...


class ConnectionLike(Protocol):
    backend: CoordinationBackend

    def __enter__(self) -> ConnectionLike: ...
    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...
    def execute(
        self,
        sql: str,
        params: Iterable[Any] | None = None,
    ) -> Any: ...
    def executescript(self, script: str) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


class PostgresCursor:
    def __init__(self, cursor: Any, *, lastrowid: int | None = None) -> None:
        self._cursor = cursor
        self.lastrowid = lastrowid

    @property
    def rowcount(self) -> int:
        return int(self._cursor.rowcount)

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return list(self._cursor.fetchall())


class PostgresConnection:
    """One borrowed connection, wrapped in the interface the ledger is written to.

    It no longer opens anything. A pool owns the underlying connections and this
    wraps whichever one was handed out, because "who may open a connection" and
    "who may use one" became different questions the moment the count was bounded.

    ``release`` is how the connection goes back. It is a callable rather than a
    reference to the pool so that a connection created outside a pool - the schema
    check on a brand new one - closes for real instead of being returned to a pool
    that never lent it.
    """

    backend: CoordinationBackend = "postgres"

    def __init__(
        self,
        connection: Any,
        *,
        release: Callable[[Any], None] | None = None,
    ) -> None:
        self._conn: Any = connection
        self._release = release
        self._returned = False

    def __enter__(self) -> PostgresConnection:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        self.close()

    def execute(
        self,
        sql: str,
        params: Iterable[Any] | None = None,
    ) -> PostgresCursor:
        statement = _postgres_sql(sql)
        wants_lastrowid = _insert_needs_lastrowid(statement)
        if wants_lastrowid and " returning " not in statement.lower():
            statement = statement.rstrip().rstrip(";") + " RETURNING id"
        cursor = self._conn.execute(statement, tuple(params or ()))
        lastrowid: int | None = None
        if wants_lastrowid:
            row = cursor.fetchone()
            if row is not None:
                lastrowid = int(row["id"])
        return PostgresCursor(cursor, lastrowid=lastrowid)

    def executescript(self, script: str) -> None:
        # Postgres must own SQL statement boundaries. Splitting on punctuation
        # here would misread semicolons in comments, strings, or function bodies.
        self._conn.execute(script, prepare=False)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        """Give the connection back, or close it if nobody lent it.

        Idempotent, because `__exit__` closes and several callers close as well,
        and returning one connection to the pool twice would hand the same socket
        to two borrowers at once - a corruption that would surface far from here.
        """

        if self._returned:
            return
        self._returned = True
        if self._release is None:
            self._conn.close()
            return
        self._release(self._conn)


def sql_status_list(*statuses: StrEnum) -> str:
    """Render enum members as a SQL IN-list.

    These values are our own constants, never user input, so interpolating them
    is safe and keeps the enum the single source of truth for text that would
    otherwise be copied into every query. A renamed member follows the query
    instead of leaving it silently matching nothing.
    """

    return ", ".join(f"'{status.value}'" for status in statuses)


def _postgres_sql(sql: str) -> str:
    return sql.replace("?", "%s")


def _insert_needs_lastrowid(statement: str) -> bool:
    normalized = " ".join(statement.lower().split())
    return normalized.startswith("insert into notes") or normalized.startswith(
        "insert into handoffs"
    )


def _backfill_structured_outcomes(c: ConnectionLike) -> None:
    c.execute(
        """
        UPDATE dispatch_intents
        SET outcome = CASE
            WHEN status='DONE' THEN 'AUTOMATED_COMPLETION'
            WHEN status='CANCELED' THEN 'OPERATOR_CANCELED'
            WHEN LOWER(COALESCE(error, '')) LIKE '%%argument list too long%%'
                THEN 'ARGUMENT_LIST_TOO_LONG'
            WHEN LOWER(COALESCE(error, '')) LIKE '%%400 bad request%%'
                THEN 'DELEGATE_REQUEST_REJECTED'
            WHEN LOWER(COALESCE(error, '')) LIKE '%%verification failed%%'
                THEN 'VERIFICATION_FAILED'
            WHEN LOWER(COALESCE(error, '')) LIKE '%%assertionerror%%'
                THEN 'INTERNAL_ASSERTION'
            WHEN LOWER(COALESCE(error, '')) LIKE '%%dependencies did not complete%%'
                THEN 'DEPENDENCY_FAILED'
            WHEN LOWER(COALESCE(error, '')) LIKE '%%timed out%%'
                THEN 'DEADLINE_EXCEEDED'
            ELSE 'UNKNOWN_FAILURE'
        END
        WHERE outcome IS NULL AND status IN ('DONE', 'FAILED', 'CANCELED')
        """
    )
    c.execute(
        """
        UPDATE agent_execution_leases
        SET outcome = CASE
            WHEN status='COMPLETED' THEN 'AUTOMATED_COMPLETION'
            WHEN status='CANCELED' THEN 'OPERATOR_CANCELED'
            WHEN status='COMPENSATED' THEN 'COMPENSATED'
            WHEN status='TIMED_OUT' AND LOWER(COALESCE(error, '')) LIKE '%%expired%%'
                THEN 'ORPHANED_LEASE_EXPIRED'
            WHEN status='TIMED_OUT' THEN 'DEADLINE_EXCEEDED'
            WHEN LOWER(COALESCE(error, '')) LIKE '%%supervisor%%'
                THEN 'SUPERVISOR_FAILED'
            ELSE 'UNKNOWN_FAILURE'
        END
        WHERE outcome IS NULL
          AND status IN ('COMPLETED', 'FAILED', 'TIMED_OUT', 'CANCELED', 'COMPENSATED')
        """
    )
    c.execute(
        """
        UPDATE agent_execution_leases
        SET agent_status = CASE
                WHEN status='COMPLETED' THEN 'COMPLETED'
                WHEN status='CANCELED' THEN 'CANCELED'
                ELSE 'FAILED'
            END,
            agent_failure = CASE WHEN status='COMPLETED' THEN NULL ELSE outcome END,
            agent_failure_category = CASE
                WHEN outcome IN ('VERIFICATION_FAILED', 'DEPENDENCY_FAILED',
                                 'DELEGATE_REQUEST_REJECTED') THEN 'BUSINESS'
                WHEN status <> 'COMPLETED' THEN 'INFRASTRUCTURE'
                ELSE NULL
            END,
            supervisor_status = CASE
                WHEN outcome='SUPERVISOR_FAILED' THEN 'FAILED'
                ELSE 'COMPLETED'
            END,
            supervisor_failure = CASE
                WHEN outcome='SUPERVISOR_FAILED' THEN error ELSE NULL
            END
        WHERE agent_status='PENDING'
          AND status IN ('COMPLETED', 'FAILED', 'TIMED_OUT', 'CANCELED', 'COMPENSATED')
        """
    )


def _postgres_applied_schema_version(c: ConnectionLike) -> int | None:
    # Qualified by the current schema on purpose. A bare to_regclass resolves
    # through the whole search path, so a connection pointed at an empty test
    # schema would find the default schema's version row, conclude it is already
    # migrated, and leave itself with no tables at all.
    relation = c.execute(
        "SELECT to_regclass(quote_ident(current_schema()) || "
        "'.coordination_schema_versions') AS relation"
    ).fetchone()
    if not relation or relation["relation"] is None:
        return None
    row = c.execute(
        "SELECT version FROM coordination_schema_versions WHERE component=?",
        (POSTGRES_SCHEMA_COMPONENT,),
    ).fetchone()
    return int(row["version"]) if row else None


def _schema_refusal(error_code: str, message: str) -> DurableFailureError:
    """Refuse to touch the schema, as a classified condition rather than a defect.

    The runtime and the database disagree about the schema, and no code can
    settle that: either the database should move forward or the checkout should.
    Only an operator knows which. So this is an expected rejection with a stable
    code, the same shape every other operator-facing refusal in this package
    carries, rather than the bare `RuntimeError` it used to be - which a caller
    could only tell apart from a bug by reading its prose.

    Never retryable. The next connection asks the identical question and gets the
    identical answer, so a loop that retried would spin until somebody typed the
    command the message already names.
    """

    return DurableFailureError(
        expected_failure(
            error_code,
            operation="ensure_coordination_schema",
            message=message,
            category=FailureCategory.BUSINESS,
            retryable=False,
        )
    )


def _assert_supported_schema_version(applied_version: int | None) -> None:
    if applied_version is not None and applied_version > SCHEMA_VERSION:
        raise _schema_refusal(
            SCHEMA_NEWER_THAN_RUNTIME,
            f"coordination schema at {postgres_target_description()} is newer than "
            f"this runtime: database={applied_version}, runtime={SCHEMA_VERSION}. "
            "This checkout is behind the ledger; pull, or point at another database.",
        )


def _assert_migration_was_asked_for(applied_version: int | None, *, allow_migration: bool) -> None:
    """Never *upgrade* implicitly. Creating is still allowed.

    Opening a connection used to run whatever DDL the file on disk described, so
    a dispatched agent's worktree - which is isolated in files and not in
    databases - migrated the shared production ledger on 2026-08-17 simply by
    reading from it, twice in one day. Every other checkout then refused to
    connect, because they were now the ones behind.

    The distinction that keeps the fix small is between creating and upgrading. A
    database with no schema at all belongs to whoever is booting it: a fresh
    clone must still come up, and `tests/conftest.py` builds a schema per test
    through exactly this path. A database that already has a schema belongs to
    every process using it, and the decision to reshape it under them is an
    operator's to make out loud.
    """

    if allow_migration or applied_version is None or applied_version >= SCHEMA_VERSION:
        return
    raise _schema_refusal(
        SCHEMA_MIGRATION_REQUIRED,
        f"coordination schema at {postgres_target_description()} needs migration: "
        f"database={applied_version}, runtime={SCHEMA_VERSION}. Migrating a shared "
        "ledger is an operator action, not a side effect of connecting. "
        f"Run: agent-ledger {CoordinationCommandName.MIGRATE_COORDINATION_SCHEMA.value}",
    )


def _advisory_lock_key(schema: str | None) -> int:
    """The migration lock key for one schema.

    Advisory lock keys are cluster-wide, so a single constant would make every
    schema's migration queue behind every other schema's. Deriving the key from the
    schema name keeps the serialization where it belongs: within one namespace.
    """

    if schema is None:
        return POSTGRES_SCHEMA_ADVISORY_LOCK_KEY
    digest = hashlib.sha256(schema.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, "big", signed=True)


def _ensure_postgres_schema(c: ConnectionLike, *, allow_migration: bool) -> None:
    applied_version = _postgres_applied_schema_version(c)
    _assert_supported_schema_version(applied_version)
    if applied_version == SCHEMA_VERSION:
        c.commit()
        return
    # Before the lock, so a refused connection costs one read and takes nothing
    # out on the database other processes are using.
    _assert_migration_was_asked_for(applied_version, allow_migration=allow_migration)

    # Every process checks the durable version before taking this lock. Only
    # the migration owner executes DDL; waiters recheck after the owner commits.
    c.execute("SELECT pg_advisory_xact_lock(?)", (_advisory_lock_key(postgres_schema()),))
    applied_version = _postgres_applied_schema_version(c)
    _assert_supported_schema_version(applied_version)
    # Both checks again, on the version the lock made authoritative. The waiter
    # that lost the race reads the owner's committed version here, and the two
    # answers a fresh read can newly give - migrated past us, or migrated to a
    # version we would still have to upgrade - are the two these refuse.
    _assert_migration_was_asked_for(applied_version, allow_migration=allow_migration)
    if applied_version != SCHEMA_VERSION:
        c.executescript(load_postgres_schema_sql())
        _backfill_structured_outcomes(c)
        c.execute(
            """
            INSERT INTO coordination_schema_versions(component, version, applied_at)
            VALUES (?, ?, ?)
            ON CONFLICT(component) DO UPDATE
            SET version=excluded.version, applied_at=excluded.applied_at
            """,
            (POSTGRES_SCHEMA_COMPONENT, SCHEMA_VERSION, now()),
        )
    c.commit()


def _schema_ready_key() -> str:
    return f"postgres:{postgres_database_url()}:{postgres_schema() or 'default'}"


def ensure_schema(c: ConnectionLike, *, allow_migration: bool = False) -> None:
    """Agree with the database about the schema, or refuse to proceed.

    A process cache skips repeat connects. The database-persisted version skips
    DDL across processes. The rare migration path is serialized so concurrent
    short-lived coordination commands cannot deadlock while each tries to
    acquire relation locks in the schema script.

    `allow_migration` is false for every caller except the operator command that
    exists to say otherwise. Creating a schema where there is none is not a
    migration and does not need it; see `_assert_migration_was_asked_for`.

    A refusal deliberately leaves the cache untouched. The marker means "this
    process has agreed with this database", and a refusal is the opposite of
    agreement: caching it would make the first failed connection skip the check
    forever, so the migration an operator then ran would never be noticed.
    """
    key = _schema_ready_key()
    if key in _SCHEMA_READY:
        return
    _ensure_postgres_schema(c, allow_migration=allow_migration)
    _SCHEMA_READY.add(key)


def migrate_postgres_schema() -> dict[str, Any]:
    """Apply pending coordination DDL, because an operator asked for it.

    The one caller allowed to migrate, and the whole reason connecting no longer
    can. It reports the version it found as well as the one it left, because
    "already current" and "just moved your shared ledger" are answers an operator
    needs to tell apart after the fact.
    """

    connection = _borrow(None)
    try:
        previous_version = _postgres_applied_schema_version(connection)
        ensure_schema(connection, allow_migration=True)
    finally:
        # Harmless after the migration's own commit, and necessary without it:
        # `ensure_schema` returns early on a database this process already
        # agreed with, leaving the probe's read transaction open.
        with contextlib.suppress(Exception):
            connection.rollback()
        connection.close()
    return {
        "component": POSTGRES_SCHEMA_COMPONENT,
        "target": postgres_target_description(),
        "previous_version": previous_version,
        "version": SCHEMA_VERSION,
        "migrated": previous_version != SCHEMA_VERSION,
        "created": previous_version is None,
    }


def coordination_schema_state() -> dict[str, Any]:
    """What the database says its schema is, without changing it.

    Read-only by construction: it neither creates nor upgrades, so a reporting
    surface can ask about a database it must not touch. `first-run-check.sh` is
    the caller this exists for, and a readiness check that migrated in order to
    report on migration would be its own worst finding.
    """

    connection = _borrow(None)
    try:
        applied_version = _postgres_applied_schema_version(connection)
    finally:
        # A read still opens a transaction, and this one is deliberately not
        # committed: the point of the function is that it changed nothing.
        with contextlib.suppress(Exception):
            connection.rollback()
        connection.close()
    if applied_version is None:
        state = "ABSENT"
    elif applied_version == SCHEMA_VERSION:
        state = "CURRENT"
    elif applied_version < SCHEMA_VERSION:
        state = "MIGRATION_REQUIRED"
    else:
        state = "NEWER_THAN_RUNTIME"
    return {
        "state": state,
        "target": postgres_target_description(),
        "applied_version": applied_version,
        "runtime_version": SCHEMA_VERSION,
    }


# How many connections one process may hold against one ledger.
#
# The number that matters is concurrent *transactions*, not threads. Sibling
# milestones run concurrently and a transaction is connection-scoped state, so
# they genuinely cannot share one - but a thread that finished its transaction
# needs nothing, and the previous design kept a connection for it anyway.
#
# The default covers the engine's own concurrency (senior 3, staff 1, junior 4)
# with room for the API's request handlers on top. It is deliberately far below
# Postgres's `max_connections`, because several processes share that budget: the
# API server, the dispatcher, the drainer, and the daemon all draw on it, and the
# outage this bound exists to prevent was their sum reaching it, not any one of
# them misbehaving.
POOL_MAX_SIZE_ENV = "AGENT_COORDINATION_POOL_MAX_SIZE"
DEFAULT_POOL_MAX_SIZE = 16


def postgres_pool_max_size() -> int:
    raw = os.environ.get(POOL_MAX_SIZE_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_POOL_MAX_SIZE
    try:
        size = int(raw)
    except ValueError:
        raise ValueError(f"{POOL_MAX_SIZE_ENV} must be an integer, got {raw!r}") from None
    if size < 1:
        raise ValueError(f"{POOL_MAX_SIZE_ENV} must be at least 1, got {size}")
    return size


# One pool per (database, schema). Keyed rather than global because a test points
# the store at its own schema and a process that moved its root must not hand back
# connections aimed at where it used to be.
_pools: dict[str, Any] = {}
_pools_lock = threading.Lock()

# Transaction nesting is still per-thread. It is the only thing that has to be:
# the depth decides whether a caller is inside another caller's transaction, and
# that question is about the call stack, not about which connection served it.
_connections = threading.local()


def _transaction_depth() -> int:
    return getattr(_connections, "depth", 0)


@dataclass(frozen=True, slots=True)
class BudgetFits:
    """This process's pool can be filled without reaching the server's ceiling."""

    server_max: int
    in_use: int
    pool_max: int

    @property
    def sufficient(self) -> bool:
        return True

    def describe(self) -> str:
        return (
            f"connection budget fits: {self.in_use} of {self.server_max} in use, "
            f"this process may draw {self.pool_max}"
        )


@dataclass(frozen=True, slots=True)
class BudgetExceeded:
    """Filling this process's pool would reach the server's ceiling.

    The failure this predicts is `FATAL: sorry, too many clients already`, raised
    at whatever statement happens to be running when the last slot goes. That is
    a long way from the cause, which is arithmetic nobody performed: several
    processes each sized their own pool correctly and their sum was never
    checked against the one number they share.
    """

    server_max: int
    in_use: int
    pool_max: int

    @property
    def sufficient(self) -> bool:
        return False

    def describe(self) -> str:
        return (
            f"connection budget exceeded: the server allows {self.server_max}, "
            f"{self.in_use} are already in use, and this process may draw "
            f"{self.pool_max} more ({self.in_use + self.pool_max} > {self.server_max}). "
            f"Raise the server's max_connections, or lower {POOL_MAX_SIZE_ENV}, "
            "or run fewer pooled processes against this ledger."
        )


@dataclass(frozen=True, slots=True)
class BudgetUnknown:
    """The server could not be asked.

    Distinct from `BudgetExceeded` because the responses differ: a budget that
    could not be measured is not evidence of a budget that does not fit, and
    refusing on ignorance would stop setups this check never anticipated.
    """

    detail: str

    @property
    def sufficient(self) -> bool:
        return True

    def describe(self) -> str:
        return f"connection budget unknown ({self.detail})"


ConnectionBudget = BudgetFits | BudgetExceeded | BudgetUnknown


def _server_connection_stats(target: str) -> tuple[int, int]:
    """Ask the server for its ceiling and what is already drawn against it.

    Split from the arithmetic so the arithmetic can be tested without a
    database, and so a test never has to reach into `psycopg.connect` - which is
    shared with everything else that opens a connection, including test
    teardown.

    A direct connection rather than the pool, deliberately: the pool is the
    thing whose size is in question, and borrowing from it to ask whether it
    fits would be one of the connections being counted.
    """

    import psycopg

    with psycopg.connect(target, connect_timeout=5) as probe, probe.cursor() as cursor:
        cursor.execute("SHOW max_connections")
        row = cursor.fetchone()
        server_max = int(row[0]) if row else 0
        cursor.execute("SELECT count(*) FROM pg_stat_activity")
        row = cursor.fetchone()
        in_use = int(row[0]) if row else 0
    return server_max, in_use


def check_connection_budget(database_url: str | None = None) -> ConnectionBudget:
    """The one place that compares this process's pool against the shared ceiling.

    `postgres_pool_max_size` bounds what a single process draws, and its own
    comment names the outage that bound exists to prevent: the sum of the API
    server, the dispatcher, the drainer, and the daemon reaching Postgres's
    `max_connections`. Nothing performed that sum. Each process sized itself
    correctly and the total was checked by the database, at the moment it ran
    out, inside whatever call was unlucky.

    Measured rather than declared. A process cannot know how many peers exist,
    but the server knows how many are connected, so asking it answers the real
    question without anybody maintaining a list of who draws on the budget.

    Deliberately not called from `_new_pool`. Pools open lazily inside whatever
    is already running, and refusing there would take down a resident loop
    mid-life over a condition it did not create. This belongs at a door, next to
    the other things checked before work starts.
    """

    target = database_url or postgres_database_url()
    try:
        server_max, in_use = _server_connection_stats(target)
    except Exception as exc:
        return BudgetUnknown(detail=f"{type(exc).__name__}: {exc}")

    pool_max = postgres_pool_max_size()
    if in_use + pool_max > server_max:
        return BudgetExceeded(server_max=server_max, in_use=in_use, pool_max=pool_max)
    return BudgetFits(server_max=server_max, in_use=in_use, pool_max=pool_max)


def _pool_key(database_url: str, schema: str | None) -> str:
    return f"{database_url}:{schema or 'default'}"


def _new_pool(database_url: str, schema: str | None) -> Any:
    try:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
    except Exception as exc:  # pragma: no cover - dependency is declared
        raise RuntimeError("psycopg[pool] is required for Postgres coordination") from exc

    # The search path is a libpq startup option rather than a SET, because a SET is
    # transaction-scoped: the ROLLBACK on either error path would revert it and
    # leave the connection pointed at the default schema, which is the one case
    # where getting it wrong is silent. Under a pool it also has to be a property
    # of the connection, since the next borrower inherits whatever it carries.
    kwargs: dict[str, Any] = {"row_factory": dict_row}
    if schema:
        kwargs["options"] = f"-c search_path={schema}"

    # No `configure`, deliberately. The schema check used to live there, and
    # psycopg_pool treats a raising `configure` as a connection that failed to
    # open: it logs the exception, discards the socket, and has a background
    # worker try again, while the caller waits out the full 30 second checkout
    # timeout and is then handed `PoolTimeout`. `_diagnosed_checkout_failure`
    # probes the server, finds it perfectly healthy, and reports pool exhaustion.
    #
    # That is not a hypothetical. It is consequence 3 of the 2026-08-17 outage:
    # two agents spent an afternoon on a phantom "all 16 connections are checked
    # out" that was really a schema disagreement, because the one message that
    # said so had been swallowed. A refusal nobody can read is not a refusal.
    #
    # So the check moved to the checkout in `connect`, where the exception is on
    # the caller's own stack. It also runs there once per process rather than
    # once per socket, which is what `_SCHEMA_READY` always claimed and what
    # `configure` could not deliver: a pool that already held open connections
    # never re-ran it, so a cleared cache checked nothing.
    return ConnectionPool(
        database_url,
        kwargs=kwargs,
        # Nothing is opened until something asks. A test creates a schema per case
        # and would otherwise pay for a handful of eager connections it never uses.
        min_size=0,
        max_size=postgres_pool_max_size(),
        # Wait for a free connection, then give up loudly. Blocking forever would
        # turn pool exhaustion into a hang with no stack pointing at the cause,
        # which is strictly worse than the error it replaces.
        timeout=30.0,
        open=True,
    )


def _pool() -> Any:
    key = _pool_key(postgres_database_url(), postgres_schema())
    with _pools_lock:
        pool = _pools.get(key)
        if pool is None:
            pool = _new_pool(postgres_database_url(), postgres_schema())
            _pools[key] = pool
        return pool


def connect(*, checkout_timeout_seconds: float | None = None) -> ConnectionLike:
    """Borrow a connection from this ledger's pool.

    Named `connect` still, because every caller means "give me a connection to
    work on" and none of them ever meant "open a socket". Closing it returns it.

    `checkout_timeout_seconds` overrides the pool's 30 second wait for this one
    checkout. It exists for best-effort reads on latency-sensitive paths: a
    caller that answers "empty" on failure anyway has no business queueing half
    a minute behind a saturated pool to earn that answer. Correctness paths
    should leave it unset and inherit the pool's own patience.

    The schema check happens here, on the caller's stack, and costs a set lookup
    after the first checkout in the process. `_new_pool` says why it cannot live
    in the pool's `configure` instead.
    """

    connection = _borrow(checkout_timeout_seconds)
    try:
        ensure_schema(connection)
    except BaseException:
        # Back to the pool rather than leaked. The connection itself is healthy;
        # it is the database's shape this process declined, and holding a socket
        # hostage to that would turn one refusal into pool exhaustion - the very
        # misdiagnosis this arrangement exists to stop.
        #
        # Rolled back first because the version probe opened a transaction and
        # the refusal left it open. psycopg_pool would clean that up itself and
        # log a line about it to stderr for every refused connection, which puts
        # driver noise between the operator and the one message that says what to
        # do.
        with contextlib.suppress(Exception):
            connection.rollback()
        connection.close()
        raise
    return connection


def _borrow(checkout_timeout_seconds: float | None) -> ConnectionLike:
    """Take a connection out of this ledger's pool, with no opinion about schema."""

    # Two attempts, because `reset_connections` closes pools across threads and
    # this is the window it leaves. A caller reads the pool out of `_pools` and
    # is descheduled; a reset clears the dict and closes that pool; the caller
    # resumes and checks out of a closed one. `reset_connections` says
    # cross-thread closing is safe only between units of work, and an in-flight
    # HTTP request is not between units of work: the API serves on a threadpool
    # and `applied_ledger_selection` resets whenever the root moves, so the
    # cockpit's first `GET /work-units` after startup answered 500 while every
    # later one answered 200.
    #
    # A closed pool is the one checkout failure that says nothing about the
    # database, so it is the one worth retrying: the second `_pool()` builds a
    # fresh pool against the root the reset was pointing at. Anything else is
    # diagnosed and raised on the first attempt.
    for remaining in (1, 0):
        pool = _pool()
        try:
            raw = (
                pool.getconn()
                if checkout_timeout_seconds is None
                else pool.getconn(timeout=checkout_timeout_seconds)
            )
        except Exception as exc:
            if remaining and _is_pool_closed(exc):
                _discard_pool(pool)
                continue
            raise _diagnosed_checkout_failure(exc) from exc
        return PostgresConnection(raw, release=pool.putconn)
    raise AssertionError("unreachable: the last attempt returns or raises")


def _is_pool_closed(exc: Exception) -> bool:
    """Whether a checkout failed because the pool was closed underneath it."""

    try:
        from psycopg_pool import PoolClosed
    except Exception:  # pragma: no cover - dependency is declared
        return False
    return isinstance(exc, PoolClosed)


def _discard_pool(pool: Any) -> None:
    """Forget a pool that can no longer serve, if it is still the cached one.

    Usually a no-op: `reset_connections` clears the dict before closing, so the
    pool this caller held is already gone and the retry builds a new one. It
    matters for the order that leaves a closed pool cached, where retrying
    without discarding would fetch the same dead pool forever.
    """

    with _pools_lock:
        for key, cached in list(_pools.items()):
            if cached is pool:
                del _pools[key]


def _diagnosed_checkout_failure(exc: Exception) -> Exception:
    """Say which of the two problems a pool timeout actually is.

    `PoolTimeout` reports "couldn't get a connection after 30.00 sec" for both
    "every connection is checked out" and "the database is not reachable", and it
    puts the real cause in a warning log rather than in the exception. Those need
    opposite responses - raise the bound or lower concurrency, versus fix the URL
    or start the server - so collapsing them into one message costs an operator
    the whole diagnosis.

    Asking for a direct connection settles it. If that fails, the database is the
    problem and its error is the honest one to raise. If it succeeds, the pool
    genuinely had nothing free, and saturation is the honest answer.

    The two answers differ in type as well as in message, because a resident loop
    acts on them differently: an unreachable ledger is weather it should wait
    out, and an exhausted pool is a bound somebody has to change. Saying that
    only in the message would leave every caller re-deriving it from prose.
    """

    if type(exc).__name__ != "PoolTimeout":
        return exc
    try:
        import psycopg
    except Exception:  # pragma: no cover - dependency is declared
        return exc

    target = f"{postgres_database_url()} (schema {postgres_schema() or 'default'})"
    try:
        probe = psycopg.connect(postgres_database_url(), connect_timeout=5)
    except Exception as reachability:
        return LedgerUnavailable(
            f"coordination ledger at {target} is not reachable: {reachability}"
        )
    probe.close()
    return RuntimeError(
        f"coordination connection pool for {target} is exhausted: all "
        f"{postgres_pool_max_size()} connections are checked out. Either work is "
        f"more concurrent than {POOL_MAX_SIZE_ENV} permits, or a caller is holding "
        "a transaction open across something slow."
    )


def reset_connections() -> None:
    """Close every pooled connection, on this thread and on any other.

    Anything that changes which database the process talks to must call this, and
    so must a test that destroys its database. Closing is best-effort by design: a
    connection whose database has already gone is precisely the case this exists to
    clean up.

    Cross-thread closing is safe only between units of work, which is what this is
    for. It is not a way to cancel a connection another thread is mid-query on.
    """

    _connections.depth = 0
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        with contextlib.suppress(Exception):
            pool.close()


# A pool runs background workers and a scheduler, and psycopg_pool complains on
# stderr for each one still running at interpreter exit. Most invocations of this
# module are short-lived coordination commands that open a pool, do one thing, and
# exit, so without this every one of them would end in four warnings - into the
# same dispatcher log that just stopped dropping the field that says whether a
# task failed. Closing at exit is also simply correct: the pool owns sockets.
atexit.register(reset_connections)


@contextmanager
def tx():
    """One transaction, on a connection borrowed for exactly its duration.

    A nested call gets its own connection rather than joining the caller's
    transaction, which preserves the isolation callers already have. That matters
    concretely: `emit` writes a best-effort ledger row from inside two callers'
    open transactions, and folding that write into the caller would let a failed
    audit insert abort a command that had already succeeded. The pool serves that
    naturally - a checkout is by definition a connection nobody else holds - so
    nesting needs no special case beyond having somewhere to borrow from.

    The borrow is per transaction and not per thread. Threads outlive their work:
    a request handler returns, its thread goes back to a pool of its own, and the
    old per-thread cache kept its connection alive for the life of the process. A
    server with a forty-thread pool therefore held forty connections whether it was
    doing anything or not, and hit Postgres's ceiling by simply staying up.
    """

    connection = connect()
    _connections.depth = _transaction_depth() + 1
    try:
        yield connection
        connection.commit()
    except Exception:
        with contextlib.suppress(Exception):
            connection.rollback()
        raise
    finally:
        _connections.depth = max(0, _transaction_depth() - 1)
        connection.close()


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _event_aggregate(event_type: str, payload: dict[str, Any]) -> tuple[str, str]:
    for aggregate_type, key in (
        ("dispatch_intent", "intent_id"),
        ("dispatch_intent", "old_intent_id"),
        ("agent_execution_lease", "lease_id"),
        ("agent_execution_event", "event_id"),
        ("agent_execution_checkpoint", "checkpoint_id"),
        ("ledger_event", "event_id"),
        ("approval_request", "approval_id"),
        ("artifact", "artifact_id"),
        ("task", "task_id"),
        ("pow_wow", "pow_wow_id"),
        ("saga", "saga_id"),
        ("gawd_doc", "gawd_doc_id"),
        ("session", "session_id"),
    ):
        value = payload.get(key)
        if value:
            return aggregate_type, str(value)
    return event_type, ""


# Draining the outbox emits these, so mirroring them would refill the table
# faster than a consumer could empty it.
_OUTBOX_RECURSIVE_EVENT_TYPES = frozenset({"claim_ledger_event", "complete_ledger_event"})

# Internal bookkeeping emitted per agent step, per heartbeat, and per GC pass.
# These are ~92 percent of all rows written and no consumer projects them, so
# mirroring them buys nothing and costs unbounded growth. events.jsonl still
# records every one, which is where an operator reads them anyway.
_OUTBOX_HIGH_VOLUME_EVENT_TYPES = frozenset(
    {"append_execution_event", "heartbeat_execution_lease", "gc_ledger"}
)

_UNMIRRORED_EVENT_TYPES = _OUTBOX_RECURSIVE_EVENT_TYPES | _OUTBOX_HIGH_VOLUME_EVENT_TYPES


def _record_ledger_event(event_type: str, payload: dict[str, Any], t: float) -> None:
    """Best-effort durable outbox mirror for the human-readable JSONL event log."""

    from local_first_agent_os.settings import get_settings

    if get_settings().ledger_outbox_destination is None:
        return
    if event_type in _UNMIRRORED_EVENT_TYPES:
        return
    try:
        event_id = str(uuid.uuid4())
        aggregate_type, aggregate_id = _event_aggregate(event_type, payload)
        with tx() as c:
            c.execute(
                """
                INSERT INTO ledger_events(
                    event_id, event_type, aggregate_type, aggregate_id,
                    payload_json, status, attempts, created_at
                ) VALUES (?, ?, ?, ?, ?, 'PENDING', 0, ?)
                """,
                (
                    event_id,
                    event_type,
                    aggregate_type,
                    aggregate_id,
                    json.dumps(payload, sort_keys=True),
                    t,
                ),
            )
    except Exception:
        # Event recording must not make the command fail after its primary state
        # transition has already committed. The JSONL mirror below still carries
        # the operator-visible audit trail.
        return


def emit(event_type: str, payload: dict[str, Any]) -> None:
    t = now()
    rec = {"ts": iso(t), "event_type": event_type, "payload": payload}
    _record_ledger_event(event_type, payload, t)
    with events_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, sort_keys=True) + "\n")


def rowdict(r: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a row into a plain dict.

    Rows arrive as a psycopg `dict_row`, which is already a mapping; the copy is
    what makes a returned row safe to mutate and to serialize.
    """

    return dict(r)


def ok(**data: Any) -> dict[str, Any]:
    return {"ok": True, **data}


def err(error: str, **data: Any) -> dict[str, Any]:
    operation = str(data.pop("_operation", "") or sys._getframe(1).f_code.co_name)
    message = str(data.get("message") or "")
    failure = expected_failure(error, operation=operation, message=message)
    logging.getLogger(__name__).info(
        "coordination_command_rejected",
        extra=failure.observability_fields(),
    )
    return {"ok": False, "error": error, "failure": failure.to_dict(), **data}


def require_session(
    c: ConnectionLike,
    session_id: str | None = None,
) -> dict[str, Any]:
    sid = session_id or os.environ.get("AGENT_SESSION_ID")
    if not sid:
        raise ValueError("session_id required; pass session_id or set AGENT_SESSION_ID")
    r = c.execute("SELECT * FROM sessions WHERE session_id = ?", (sid,)).fetchone()
    if not r:
        raise ValueError(f"unknown session_id: {sid}")
    return r


def optional_session(
    c: ConnectionLike,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    sid = session_id or os.environ.get("AGENT_SESSION_ID")
    if not sid:
        return None
    return require_session(c, sid)


def claim_to_dict(r: dict[str, Any], t: float | None = None) -> dict[str, Any]:
    t = now() if t is None else t
    d = rowdict(r)
    d["created_at"] = iso(d["created_at"])
    d["expires_at"] = iso(d["expires_at"])
    d["expired"] = r["expires_at"] <= t
    d["ttl_remaining_seconds"] = max(0, round(r["expires_at"] - t, 3))
    return d


def session_to_dict(r: dict[str, Any]) -> dict[str, Any]:
    d = rowdict(r)
    d["created_at"] = iso(d["created_at"])
    d["last_heartbeat_at"] = iso(d["last_heartbeat_at"])
    return d


def decode_json_array(v: Any) -> list[Any]:
    if not v:
        return []
    if isinstance(v, list):
        return v
    try:
        return json.loads(v)
    except Exception:
        return []


def decode_json_object(v: Any) -> dict[str, Any]:
    if not v:
        return {}
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Layer 1: session + file-claim API (unchanged)
# ---------------------------------------------------------------------------
