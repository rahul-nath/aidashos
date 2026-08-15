# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The row-locking guarantees the production claim path depends on.

These are the tests the removed SQLite adapter structurally could not run: its
lock clauses degraded to the empty string, so every assertion about concurrent
claiming was checking a query with no locking in it. That reads as coverage while
testing nothing, which is worse than having no test.

The failure this lane exists to catch is not hypothetical: a Postgres-only
deadlock, `AccessExclusiveLock` during concurrent execution-event persistence,
is what stopped the Pest project at milestone six.

These tests create and drop whole databases, which is why they take their own
rather than running in the per-test schema the rest of the suite uses.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from postgres_support import SKIP_UNLESS_INTEGRATION
from work_unit_support import compile_acceptance_doc

from local_first_agent_os.coordination.dispatch import (
    claim_next_dispatch_intent,
    submit_dispatch_intent,
)
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.events import MilestoneTransition, PhaseTransition
from local_first_agent_os.work_units.lifecycle import (
    LifecyclePhase,
    MilestoneExecutionStatus,
    PhaseStatus,
)

pytestmark = [pytest.mark.integration, SKIP_UNLESS_INTEGRATION]

_CONCURRENT_CLAIMERS = 8


def _submit(count: int) -> list[str]:
    return [
        str(
            submit_dispatch_intent(
                "senior",
                f"work item {index}",
                "advisory",
                None,
                f"locking-test:{index}",
            )["intent_id"]
        )
        for index in range(count)
    ]


def test_concurrent_claimers_never_run_one_intent_twice(postgres_ledger: str) -> None:
    """Eight claimers against eight intents: each intent goes to exactly one.

    This is the guarantee `FOR UPDATE SKIP LOCKED` exists to provide. On SQLite the
    same code runs a bare SELECT and the guarantee comes from the database being
    serialized by a file lock, which is not the property production relies on.
    """

    submitted = _submit(_CONCURRENT_CLAIMERS)

    def claim(worker: int) -> str | None:
        intent = claim_next_dispatch_intent(f"worker-{worker}").get("intent")
        return str(intent["intent_id"]) if intent else None

    with ThreadPoolExecutor(max_workers=_CONCURRENT_CLAIMERS) as pool:
        claimed = list(pool.map(claim, range(_CONCURRENT_CLAIMERS)))

    won = [item for item in claimed if item is not None]
    assert sorted(won) == sorted(submitted), "every intent must be claimed exactly once"
    assert len(set(won)) == len(won), "no intent may be claimed twice"


def test_a_locked_row_is_skipped_rather_than_waited_on(postgres_ledger: str) -> None:
    """A claimer steps over a row another transaction holds, instead of blocking.

    `SKIP LOCKED` is what keeps one slow claim from stalling every other dispatcher.
    Without it this call would block until the holding transaction commits, so the
    claim runs on a timer: it has to come back with the other intent while the lock
    is still held.
    """

    first, second = _submit(2)
    released = threading.Event()

    with psycopg.connect(postgres_ledger) as holder, holder.transaction():
        holder.execute(
            "SELECT intent_id FROM dispatch_intents WHERE intent_id = %s FOR UPDATE",
            (first,),
        ).fetchone()

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(lambda: claim_next_dispatch_intent("skipper").get("intent"))
            intent = future.result(timeout=15)
        elapsed = time.monotonic() - started
        released.set()

    assert intent is not None, "the claimer must find the unlocked intent"
    assert str(intent["intent_id"]) == second, "it must skip the held row, not wait for it"
    assert elapsed < 10, "a held row must be skipped, not waited on"
    assert released.is_set()


def test_the_work_unit_row_lock_serializes_conflicting_transitions(
    postgres_ledger: str,
) -> None:
    """Two facts for one WorkUnit at once both land, because the row lock orders them.

    `record_fact` reads the WorkUnit under `FOR UPDATE` and then writes with an
    optimistic version check. The lock is what makes those two steps atomic: without
    it both readers see the same version, and the second write finds zero rows and
    raises. This asserts the pair works together under real contention.
    """

    compiled = compile_acceptance_doc(design_doc_id="locking_serialization")
    assert compiled.compiled_plan_revision_id is not None
    unit = repo.start_work_unit(compiled.compiled_plan_revision_id).work_unit

    def record(phase: LifecyclePhase) -> bool:
        return repo.record_fact(
            unit.work_unit_id,
            PhaseTransition(phase=phase, status=PhaseStatus.SKIPPED),
        ).applied

    with ThreadPoolExecutor(max_workers=2) as pool:
        applied = list(pool.map(record, [LifecyclePhase.CLARIFY, LifecyclePhase.VALIDATE]))

    assert applied == [True, True], "both distinct facts must land"
    events = repo.list_work_unit_events(unit.work_unit_id, limit=100)
    sequences = [item.sequence_number for item in events]
    # The sequence is assigned under the lock, so contention cannot produce a gap
    # or a duplicate.
    assert sequences == sorted(set(sequences)) == list(range(1, len(sequences) + 1))


def test_concurrent_identical_facts_collapse_onto_one_event(postgres_ledger: str) -> None:
    """The idempotency key is enforced by the database, not by a check-then-write.

    Several callers submitting the same fact at the same moment is exactly what a
    re-delivered dispatch outcome looks like. Only one may apply.
    """

    compiled = compile_acceptance_doc(design_doc_id="locking_idempotency")
    assert compiled.compiled_plan_revision_id is not None
    unit = repo.start_work_unit(compiled.compiled_plan_revision_id).work_unit
    repo.record_fact(
        unit.work_unit_id,
        MilestoneTransition(
            phase=LifecyclePhase.PLAN,
            milestone_key="a",
            status=MilestoneExecutionStatus.READY,
            attempt=1,
        ),
    )

    def record(_: int) -> bool:
        return repo.record_fact(
            unit.work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase.PLAN,
                milestone_key="a",
                status=MilestoneExecutionStatus.RUNNING,
                attempt=1,
            ),
        ).applied

    with ThreadPoolExecutor(max_workers=_CONCURRENT_CLAIMERS) as pool:
        applied = list(pool.map(record, range(_CONCURRENT_CLAIMERS)))

    assert sum(applied) == 1, "exactly one caller may apply the fact"
    events = repo.list_work_unit_events(unit.work_unit_id, limit=100)
    started = [item for item in events if item.event_type.value == "MILESTONE_STARTED"]
    assert len(started) == 1


def test_a_named_schema_isolates_the_ledger_within_one_database(
    postgres_ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two schemas in one database are two independent ledgers.

    This is what makes a per-test schema viable instead of a per-test database.
    The three things that had to be fixed for it are all exercised here: the
    version probe is qualified by the current schema, the readiness cache is keyed
    by schema, and the search path is a connection parameter rather than a
    statement that a rollback would revert.
    """

    from local_first_agent_os.coordination import store
    from local_first_agent_os.coordination.projects import create_saga

    for schema in ("ledger_one", "ledger_two"):
        with psycopg.connect(postgres_ledger, autocommit=True) as connection:
            connection.execute(f"CREATE SCHEMA {schema}")

    monkeypatch.setenv("AGENT_COORDINATION_SCHEMA", "ledger_one")
    store.reset_connections()
    store._SCHEMA_READY.clear()
    first = str(create_saga("saga in the first schema")["saga_id"])

    monkeypatch.setenv("AGENT_COORDINATION_SCHEMA", "ledger_two")
    store.reset_connections()
    store._SCHEMA_READY.clear()
    second = str(create_saga("saga in the second schema")["saga_id"])

    with psycopg.connect(postgres_ledger) as connection:
        one = connection.execute("SELECT saga_id FROM ledger_one.sagas").fetchall()
        two = connection.execute("SELECT saga_id FROM ledger_two.sagas").fetchall()
        public = connection.execute("SELECT to_regclass('public.sagas') AS relation").fetchone()

    assert [row[0] for row in one] == [first]
    assert [row[0] for row in two] == [second]
    # The default schema stayed empty, which is the failure the qualified version
    # probe prevents: an unqualified to_regclass would have found public's row and
    # skipped the DDL entirely.
    assert public is not None and public[0] is None


def test_each_schema_migrates_under_its_own_advisory_key() -> None:
    """Migration serializes within a schema, not across the whole cluster."""

    from local_first_agent_os.coordination.store import (
        POSTGRES_SCHEMA_ADVISORY_LOCK_KEY,
        _advisory_lock_key,
    )

    assert _advisory_lock_key(None) == POSTGRES_SCHEMA_ADVISORY_LOCK_KEY
    assert _advisory_lock_key("ledger_one") != _advisory_lock_key("ledger_two")
    # Stable across processes, because it is derived rather than allocated.
    assert _advisory_lock_key("ledger_one") == _advisory_lock_key("ledger_one")
    assert -(2**63) <= _advisory_lock_key("ledger_one") < 2**63


def test_a_nested_transaction_does_not_join_its_caller(postgres_ledger: str) -> None:
    """`emit` writes a best-effort ledger row from inside an open transaction.

    Folding that write into the caller's transaction would let a failed audit
    insert abort a command that had already succeeded, so a nested call gets its
    own connection even though the outer one is cached.

    This lives in the Postgres lane because it cannot run anywhere else: on SQLite
    a second connection cannot BEGIN IMMEDIATE while the first holds the write
    lock, so the nested call blocks for the full busy timeout and then fails. One
    more production behavior the adapter cannot express.
    """

    from local_first_agent_os.coordination import store

    try:
        with store.tx() as outer, store.tx() as inner:
            assert inner is not outer
            inner.execute("SELECT 1")
            outer.execute("SELECT 1")
    finally:
        store.reset_connections()
