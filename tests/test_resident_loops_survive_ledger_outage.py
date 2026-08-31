# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What the two resident loops do when the coordination database goes away.

Both loops died at 11:55:31 on 2026-08-05, six minutes after they started,
because `docker compose stop` took Postgres out from under them. Neither came
back, so a WorkUnit started afterwards would have sat at QUEUED with no failure
recorded anywhere. The drainer's own docstring already said a recoverable outage
must not become one that needs a human to restart a process; this is the outage
it did not survive.

The condition is recognised by exception type. `AdminShutdown` here is not a
stand-in: it is the class a terminated backend actually raises, confirmed
against a live database by `test_a_terminated_backend_is_recognised_as_an_outage`
below, which is why the injected tests can use it and mean something.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import psycopg
import pytest

from local_first_agent_os import dispatcher as dispatcher_module
from local_first_agent_os.coordination import ClaimNextDispatchIntent, DispatchTerminalStatus, store
from local_first_agent_os.coordination.availability import (
    LedgerUnavailable,
    ledger_unavailable,
)
from local_first_agent_os.coordination.cli import execute_argv
from local_first_agent_os.dispatcher import LedgerDispatcher
from local_first_agent_os.work_units import enqueue_drainer as drainer_module
from local_first_agent_os.work_units.enqueue_drainer import (
    EnqueueDrainer,
    Idle,
    Unavailable,
)
from local_first_agent_os.work_units.root_workflow import EnqueueDelivery

REPO_ROOT = Path(__file__).resolve().parents[1]


def _terminated_backend() -> psycopg.OperationalError:
    return psycopg.errors.AdminShutdown("terminating connection due to administrator command")


# --- what counts as an outage -------------------------------------------------


def test_a_terminated_backend_is_recognised_as_an_outage(work_unit_ledger: Any) -> None:
    """The anchor for every injected test below: a real backend, really killed.

    `docker compose stop` terminates every backend at once, and this terminates
    one. If the driver ever changes which class it raises, this fails and the
    injected tests stop being fiction that happens to pass.
    """

    url = store.postgres_database_url()
    try:
        # The whole unit of work, because a pass may notice the termination on
        # its next statement or on the commit that ends it, and a loop has to
        # survive either one.
        with (
            pytest.raises(Exception) as raised,  # noqa: PT011 - the type is the assertion
            store.connect() as held,
        ):
            row = held.execute("SELECT pg_backend_pid() AS pid").fetchone()
            assert row is not None
            pid = row["pid"] if isinstance(row, dict) else row[0]
            with psycopg.connect(url, autocommit=True) as killer:
                killer.execute("SELECT pg_terminate_backend(%s)", (pid,))
            held.execute("SELECT 1").fetchone()
        assert ledger_unavailable(raised.value), (
            f"a terminated backend raised {type(raised.value).__name__}, "
            "which no resident loop will recognise as an outage"
        )
    finally:
        # The pool is now holding a connection to a backend that no longer
        # exists, and every later test in this process would inherit it.
        store.reset_connections()


def test_a_defect_is_not_an_outage() -> None:
    """The half that matters more.

    A loop that treated these as weather would spin on a defect forever while
    reporting that the database was down, which is a worse failure than the one
    being fixed: it never stops and it never says anything true.
    """

    assert not ledger_unavailable(ValueError("bad argument"))
    assert not ledger_unavailable(psycopg.errors.UndefinedColumn("no such column"))
    assert not ledger_unavailable(psycopg.errors.UniqueViolation("duplicate key"))
    assert not ledger_unavailable(KeyError("intent_id"))


def test_this_repositorys_own_diagnosis_is_recognised() -> None:
    assert ledger_unavailable(LedgerUnavailable("ledger at ... is not reachable"))


# --- the enqueue drainer ------------------------------------------------------


def test_the_drainer_reports_an_outage_instead_of_ending_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*_args: object, **_kwargs: object) -> object:
        raise _terminated_backend()

    monkeypatch.setattr(drainer_module, "drain_enqueue_outbox", _raise)
    outcome = EnqueueDrainer(delivery=EnqueueDelivery.INLINE).poll_once()

    assert isinstance(outcome, Unavailable)
    assert "AdminShutdown" in outcome.error


def test_the_drainer_keeps_polling_across_an_outage_and_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point: the loop is still running when the database comes back."""

    passes: list[str] = []

    def _flaky(*_args: object, **_kwargs: object) -> list[object]:
        if len(passes) < 2:
            passes.append("down")
            raise _terminated_backend()
        passes.append("up")
        return []

    slept: list[float] = []
    monkeypatch.setattr(drainer_module, "drain_enqueue_outbox", _flaky)
    drainer = EnqueueDrainer(delivery=EnqueueDelivery.INLINE)
    drainer.run(interval_seconds=0.0, max_polls=4, sleeper=slept.append)

    assert passes == ["down", "down", "up", "up"]
    # An outage waits longer than an idle pass, so a ten-minute one does not
    # produce hundreds of identical connection attempts and log lines.
    assert slept[:2] == [drainer.unavailable_interval_seconds] * 2
    assert slept[2:] == [0.0, 0.0]


def test_the_drainer_still_dies_on_a_defect(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_args: object, **_kwargs: object) -> object:
        raise ValueError("the outbox row is malformed")

    monkeypatch.setattr(drainer_module, "drain_enqueue_outbox", _raise)

    with pytest.raises(ValueError, match="malformed"):
        EnqueueDrainer(delivery=EnqueueDelivery.INLINE).run(interval_seconds=0.0, max_polls=1)


def test_an_outage_is_not_an_idle_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """`Idle` says the outbox was read and held nothing. An outage read nothing."""

    monkeypatch.setattr(drainer_module, "drain_enqueue_outbox", lambda *_a, **_k: [])
    assert isinstance(EnqueueDrainer(delivery=EnqueueDelivery.INLINE).poll_once(), Idle)


# --- the ledger dispatcher ----------------------------------------------------


def _dispatcher(coord: Any) -> LedgerDispatcher:
    dispatcher = LedgerDispatcher(lambda _intent: (DispatchTerminalStatus.DONE, None, None))
    dispatcher._coord = coord  # type: ignore[method-assign]
    return dispatcher


def test_the_dispatcher_reports_an_outage_instead_of_ending_the_process() -> None:
    def _raise(_command: object) -> dict[str, Any]:
        raise _terminated_backend()

    outcome = _dispatcher(_raise).poll_once()

    assert isinstance(outcome, dispatcher_module.Unavailable)
    assert "AdminShutdown" in outcome.error


def test_the_dispatcher_keeps_polling_across_an_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _flaky(_command: object) -> dict[str, Any]:
        if len(calls) < 2:
            calls.append("down")
            raise _terminated_backend()
        calls.append("up")
        return {"ok": True, "intent": None}

    monkeypatch.setattr(dispatcher_module.time, "sleep", lambda _seconds: None)
    _dispatcher(_flaky).dispatch_pending_intents(interval_seconds=0.0, max_polls=3)

    assert calls == ["down", "down", "up"]


def test_the_dispatcher_still_dies_on_a_defect() -> None:
    def _raise(_command: object) -> dict[str, Any]:
        raise KeyError("intent_id")

    with pytest.raises(KeyError):
        _dispatcher(_raise).dispatch_pending_intents(interval_seconds=0.0, max_polls=1)


# --- the transport boundary ---------------------------------------------------


def test_an_outage_is_not_reported_as_a_rejected_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`{"ok": false}` means the ledger considered this and declined it.

    An unreachable database considered nothing, and saying otherwise both states
    something false and destroys the exception type, which is the only evidence
    a loop has for telling an outage apart from a defect.
    """

    from local_first_agent_os.coordination import cli

    def _raise(_args: object) -> dict[str, Any]:
        raise _terminated_backend()

    monkeypatch.setattr(cli, "dispatch", _raise)

    with pytest.raises(psycopg.OperationalError):
        execute_argv(["list_sagas"])


def test_the_outage_survives_the_whole_transport_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam that was actually broken, end to end through the real path.

    A dispatcher's claim goes `run_coordination_command` -> transport ->
    `execute_argv`. The envelope used to flatten the outage there, and
    `_require_ok` then raised a `RuntimeError` whose only trace of the original
    was the class name inside a formatted payload. Nothing downstream could tell
    that apart from a rejected command without reading prose.
    """

    from local_first_agent_os.coordination import cli
    from local_first_agent_os.pow_wow import run_coordination_command

    def _raise(_args: object) -> dict[str, Any]:
        raise _terminated_backend()

    monkeypatch.setattr(cli, "dispatch", _raise)

    with pytest.raises(psycopg.OperationalError) as raised:
        run_coordination_command(ClaimNextDispatchIntent(claimed_by="dispatcher", tier=None))
    assert ledger_unavailable(raised.value)


def test_an_ordinary_failure_is_still_a_rejected_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from local_first_agent_os.coordination import cli

    def _raise(_args: object) -> dict[str, Any]:
        raise ValueError("unknown saga")

    monkeypatch.setattr(cli, "dispatch", _raise)
    payload = execute_argv(["list_sagas"])

    assert payload == {"ok": False, "error": "ValueError", "message": "unknown saga"}


# --- the loops the runtime actually starts -------------------------------------


def test_both_started_loops_are_the_ones_covered_here() -> None:
    """A guard against this file testing loops the runtime no longer starts.

    `start-agent-runtime.sh` is the operator's entry point, so it decides which
    loops matter. If a third resident loop is added there, this fails rather than
    leaving it silently uncovered.
    """

    script = (REPO_ROOT / "scripts" / "start-agent-runtime.sh").read_text(encoding="utf-8")
    started = set(re.findall(r"agent_coordination_mcp\.py[^\n]*?\s(run_[a-z_]+)", script))

    assert started == {"run_enqueue_drainer", "run_ledger_dispatcher"}, (
        f"the runtime starts resident loops this file does not cover: {started}"
    )
