# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""How many Postgres connections one process may hold, and for how long.

The ledger used to cache one connection per thread and release it only when the
process changed which database it talked to. Threads outlive their work - a
request handler returns and its thread goes back to a pool of its own - so a
server held a connection for every thread that had ever touched the ledger,
whether it was doing anything or not.

Measured on the live runtime: one API server held 74 connections after 33
minutes of ordinary cockpit polling, and the sum across the API server,
dispatcher, drainer, and daemon reached Postgres's `max_connections` of 100.
Every process lost the database at once and even `psql` was refused.

The earlier design was itself a fix - for a test file that opened 2591
connections - and it swapped unbounded creation for unbounded retention without
bounding either. These tests exist because neither failure mode was observable
from inside the suite: nothing counted connections, so nothing could notice.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from local_first_agent_os.coordination import store


def _open_connection_count() -> int:
    """How many connections this process currently holds against the ledger."""

    return sum(pool.get_stats().get("pool_size", 0) for pool in store._pools.values())


def test_a_transaction_returns_its_connection_when_it_ends(work_unit_ledger: Path) -> None:
    """The property the old thread cache did not have.

    Sequential transactions on one thread must not accumulate connections. Under
    the previous design this was already true *per thread*, which is exactly why
    the leak was invisible: a single-threaded test could never show it.
    """

    with store.tx() as c:
        c.execute("SELECT 1")
    after_first = _open_connection_count()

    for _ in range(20):
        with store.tx() as c:
            c.execute("SELECT 1")

    assert _open_connection_count() == after_first
    assert after_first <= 1


def test_many_threads_do_not_each_keep_a_connection(work_unit_ledger: Path) -> None:
    """The leak, reproduced as the suite could not before.

    Forty threads run one transaction each and finish. The old design would leave
    forty connections held for the life of the process; a pool leaves at most as
    many as were ever concurrently in use, and these barely overlap.
    """

    barrier_free_workers = 40

    def unit_of_work() -> None:
        with store.tx() as c:
            c.execute("SELECT 1")

    threads = [threading.Thread(target=unit_of_work) for _ in range(barrier_free_workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    held = _open_connection_count()
    assert held <= store.postgres_pool_max_size(), (
        f"{held} connections held after {barrier_free_workers} threads finished; "
        "a finished thread must not keep one"
    )


def test_concurrent_transactions_never_exceed_the_pool_bound(work_unit_ledger: Path) -> None:
    """The bound holds under real contention, not just after the fact.

    Every worker holds its transaction open until the last one has started, so the
    pool is asked for more connections than it may create. It must make them wait
    rather than exceed the bound.
    """

    max_size = 4
    workers = 12
    store.reset_connections()

    with pytest.MonkeyPatch.context() as patch:
        patch.setenv(store.POOL_MAX_SIZE_ENV, str(max_size))
        store.reset_connections()

        peak = 0
        peak_lock = threading.Lock()
        in_flight = 0
        errors: list[BaseException] = []

        def unit_of_work() -> None:
            nonlocal in_flight, peak
            try:
                with store.tx() as c:
                    c.execute("SELECT 1")
                    with peak_lock:
                        in_flight += 1
                        peak = max(peak, in_flight)
                    # Held briefly so the transactions genuinely overlap; without
                    # this they would serialize and the bound would be untested.
                    threading.Event().wait(0.05)
                    with peak_lock:
                        in_flight -= 1
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                errors.append(exc)

        threads = [threading.Thread(target=unit_of_work) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors, f"workers failed: {errors[:3]}"
        assert peak <= max_size, f"{peak} concurrent transactions exceeded the bound of {max_size}"
        assert _open_connection_count() <= max_size

    store.reset_connections()


def test_a_nested_transaction_gets_a_different_connection(work_unit_ledger: Path) -> None:
    """Nesting must not join the caller's transaction, and never did.

    `emit` writes a best-effort audit row from inside two callers' open
    transactions. Sharing the caller's connection would fold that write into the
    caller, so a failed audit insert would abort a command that had already
    succeeded.

    This is asserted because it is easy to "simplify" into sharing while pooling -
    the reasoning that a nested call is "the same transaction" is wrong here and
    sounds right. A pool serves the real requirement for free: a checkout is by
    definition a connection nobody else holds.
    """

    with store.tx() as outer, store.tx() as inner:
        assert inner is not outer
        # Distinct sessions, not merely distinct wrappers around one socket.
        outer_pid = outer.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        inner_pid = inner.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        assert inner_pid != outer_pid


def test_a_failed_transaction_returns_its_connection(work_unit_ledger: Path) -> None:
    """A rollback must not cost a connection.

    The old code dropped a failed connection from the cache and closed it, which
    was right when it owned it. Under a pool the same instinct would leak a slot
    per failure, and a workload that fails steadily would exhaust the pool.
    """

    before = _open_connection_count()

    for _ in range(10):
        with pytest.raises(RuntimeError), store.tx() as c:
            c.execute("SELECT 1")
            raise RuntimeError("caller failed mid-transaction")

    assert _open_connection_count() <= max(before, 1)
    # Still usable afterwards; a poisoned pool would fail here.
    with store.tx() as c:
        assert c.execute("SELECT 1 AS ok").fetchone()["ok"] == 1


def test_closing_a_connection_twice_does_not_return_it_twice(work_unit_ledger: Path) -> None:
    """Double return would hand one socket to two borrowers.

    `__exit__` closes, and several callers close as well. The corruption that
    causes surfaces far from its cause, so it is refused here.
    """

    connection = store.connect()
    connection.close()
    connection.close()

    with store.tx() as c:
        assert c.execute("SELECT 1 AS ok").fetchone()["ok"] == 1


def test_an_unreachable_database_says_so_rather_than_timing_out(
    work_unit_ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two ways a checkout fails need opposite responses, so they need names.

    `PoolTimeout` says "couldn't get a connection after 30.00 sec" whether every
    connection is checked out or the database is not reachable at all, and it puts
    the real cause in a warning log rather than the exception. Before the pool, an
    unreachable database raised its own error immediately; keeping that is the
    difference between "fix the URL" and "raise the bound" being distinguishable.
    """

    store.reset_connections()
    monkeypatch.setattr(
        store, "postgres_database_url", lambda: "postgresql://nope@unit-test:5432/x"
    )
    # Long enough to be a real wait if it were not short-circuited, short enough
    # that a regression here shows up as a failure rather than a hung suite.
    monkeypatch.setattr(store, "_new_pool", _pool_that_times_out_fast)

    with pytest.raises(RuntimeError, match="not reachable"), store.tx() as c:
        c.execute("SELECT 1")

    store.reset_connections()


def _pool_that_times_out_fast(database_url: str, schema: str | None) -> object:
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    return ConnectionPool(
        database_url,
        kwargs={"row_factory": dict_row},
        min_size=0,
        max_size=1,
        timeout=1.0,
        open=True,
    )


def test_the_pool_bound_is_configurable_and_validated() -> None:
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv(store.POOL_MAX_SIZE_ENV, raising=False)
        assert store.postgres_pool_max_size() == store.DEFAULT_POOL_MAX_SIZE

        patch.setenv(store.POOL_MAX_SIZE_ENV, "7")
        assert store.postgres_pool_max_size() == 7

        patch.setenv(store.POOL_MAX_SIZE_ENV, "0")
        with pytest.raises(ValueError):
            store.postgres_pool_max_size()

        patch.setenv(store.POOL_MAX_SIZE_ENV, "not-a-number")
        with pytest.raises(ValueError):
            store.postgres_pool_max_size()


def test_a_reset_between_the_pool_lookup_and_the_checkout_is_survivable(
    work_unit_ledger: Path,
) -> None:
    """The cockpit's 500 on its first request, reproduced at the seam.

    `reset_connections` closes pools across threads, and its own docstring says
    that is safe only between units of work. The API is the caller that breaks
    that: it serves on a threadpool and `applied_ledger_selection` resets
    whenever the ledger root moves, so a request that had read the pool out of
    `_pools` resumed to find it closed. The first `GET /work-units` after startup
    answered 500 with `PoolClosed` and every later one answered 200, which is the
    signature of a race rather than a broken database.

    Reproduced by handing `connect` a pool that is already closed, which is what
    the descheduled thread was holding.
    """

    store.reset_connections()
    doomed = store._pool()
    doomed.close()

    handed: list[object] = []
    real_pool = store._pool

    def one_dead_pool_then_the_real_one() -> object:
        if not handed:
            handed.append(doomed)
            return doomed
        return real_pool()

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "_pool", one_dead_pool_then_the_real_one)
        with store.connect() as connection:
            assert connection.execute("SELECT 1").fetchone() is not None

    assert handed, "the test never exercised the closed pool"
    store.reset_connections()


def test_a_checkout_failure_that_is_not_a_closed_pool_is_not_retried(
    work_unit_ledger: Path,
) -> None:
    """Only a closed pool says nothing about the database.

    Retrying anything else would turn one honest diagnosis into two attempts and
    a slower, vaguer error, which is what `_diagnosed_checkout_failure` exists to
    prevent.
    """

    store.reset_connections()
    calls: list[int] = []

    class _RefusingPool:
        def getconn(self, timeout: float | None = None) -> object:
            calls.append(1)
            raise RuntimeError("connection refused")

        def putconn(self, conn: object) -> None:  # pragma: no cover - never reached
            raise AssertionError("nothing was checked out")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "_pool", _RefusingPool)
        with pytest.raises(RuntimeError, match="connection refused"):
            store.connect()

    assert calls == [1], "a failure that is not a closed pool must not be retried"
    store.reset_connections()


def test_a_bounded_checkout_does_not_start_a_second_reachability_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicit timeout is the complete bound for latency-sensitive reads."""

    from psycopg_pool import PoolTimeout

    class _TimedOutPool:
        def getconn(self, timeout: float | None = None) -> object:
            assert timeout == 1.0
            raise PoolTimeout("bounded checkout elapsed")

    monkeypatch.setattr(store, "_pool", _TimedOutPool)
    monkeypatch.setattr(
        store,
        "_diagnosed_checkout_failure",
        lambda exc: pytest.fail(f"unexpected reachability diagnosis: {exc}"),
    )

    with pytest.raises(PoolTimeout, match="bounded checkout elapsed"):
        store.connect(checkout_timeout_seconds=1.0)
