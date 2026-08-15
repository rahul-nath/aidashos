# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The arithmetic nobody performed, and the failure that hid because of it.

`postgres_pool_max_size` bounds what one process draws. Its own comment names
the outage it exists to prevent: the API server, the dispatcher, the drainer,
and the daemon reaching Postgres's `max_connections` between them. Each sized
itself correctly; the sum was checked by the database, at the moment it ran out,
inside whatever call was unlucky. It happened on 2026-08-04 and surfaced as
`FATAL: sorry, too many clients already` inside a streaming supervisor.
"""

from __future__ import annotations

import pytest

from local_first_agent_os.coordination.availability import (
    LedgerUnavailable,
    ledger_budget_exhausted,
    ledger_unavailable,
)
from local_first_agent_os.coordination.store import (
    BudgetExceeded,
    BudgetFits,
    BudgetUnknown,
    check_connection_budget,
)


def test_a_spent_budget_is_not_an_outage_to_wait_out() -> None:
    """The misclassification that let a bound look like weather.

    `TooManyConnections` subclasses `OperationalError`, so it arrived through
    the door this module opens for a server that closed the socket. The ledger
    is up and answering; it is refusing one more client. Calling that
    unavailable tells a resident loop to wait and retry, and a retrying loop is
    itself a consumer of the budget it is waiting on, so the condition never
    clears and the loop reports weather forever.
    """

    psycopg_errors = pytest.importorskip("psycopg.errors")
    spent = psycopg_errors.TooManyConnections("sorry, too many clients already")

    assert ledger_budget_exhausted(spent) is True
    assert ledger_unavailable(spent) is False, "a bound is not an outage"


def test_an_ordinary_outage_is_still_an_outage() -> None:
    """Narrowing the budget case must not cost the case that was already right."""

    psycopg = pytest.importorskip("psycopg")
    closed = psycopg.OperationalError("server closed the connection unexpectedly")

    assert ledger_unavailable(closed) is True
    assert ledger_budget_exhausted(closed) is False
    assert ledger_unavailable(LedgerUnavailable("down")) is True


def test_a_defect_is_never_either_of_them() -> None:
    """A loop that swallowed these would spin on a bug while blaming the database."""

    psycopg = pytest.importorskip("psycopg")

    for defect in (psycopg.ProgrammingError("syntax error"), ValueError("nope")):
        assert ledger_unavailable(defect) is False
        assert ledger_budget_exhausted(defect) is False


def test_an_unreachable_server_is_unknown_rather_than_exceeded() -> None:
    """Not knowing is reported, never acted on.

    Refusing a start because the budget could not be measured would stop setups
    this check never anticipated, which is the same rule the harness probe
    already follows for a CLI it could not ask.
    """

    budget = check_connection_budget("postgresql://127.0.0.1:1/does-not-exist")

    assert isinstance(budget, BudgetUnknown)
    assert budget.sufficient is True, "ignorance must not block a run"


@pytest.mark.parametrize(
    ("server_max", "in_use", "pool_max", "expected"),
    [
        (300, 40, 16, BudgetFits),
        (100, 90, 16, BudgetExceeded),
        (100, 84, 16, BudgetFits),
        (100, 85, 16, BudgetExceeded),
    ],
    ids=["room to spare", "the 2026-08-04 shape", "exactly fits", "one over"],
)
def test_the_arithmetic_is_in_one_place(
    monkeypatch: pytest.MonkeyPatch,
    server_max: int,
    in_use: int,
    pool_max: int,
    expected: type,
) -> None:
    """Stated as a table, because the boundary is the whole point of the check.

    Patches the measurement seam rather than `psycopg.connect`, which is shared
    with every other thing that opens a connection - including this suite's own
    teardown, which a global patch quietly broke.
    """

    from local_first_agent_os.coordination import store

    monkeypatch.setattr(store, "_server_connection_stats", lambda _t: (server_max, in_use))
    monkeypatch.setattr(store, "postgres_pool_max_size", lambda: pool_max)

    budget = check_connection_budget("postgresql://example/ledger")

    assert isinstance(budget, expected)
    assert budget.sufficient is (expected is BudgetFits)


def test_the_refusal_names_the_arithmetic_rather_than_the_symptom() -> None:
    """`too many clients already` names the moment, never the cause.

    An operator reading it cannot tell which of the several pooled processes to
    change, or whether to change the server instead. The refusal has to carry
    all three numbers or it is the same message with a nicer stack.
    """

    message = BudgetExceeded(server_max=100, in_use=90, pool_max=16).describe()

    assert "100" in message and "90" in message and "16" in message
    assert "106 > 100" in message
    assert "AGENT_COORDINATION_POOL_MAX_SIZE" in message


def test_the_door_refuses_before_it_writes_a_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stated at the door, because that is where a run stops being cheap to stop."""

    from local_first_agent_os.work_units import commands

    started: list[str] = []
    monkeypatch.setattr(
        commands,
        "check_connection_budget",
        lambda: BudgetExceeded(server_max=100, in_use=95, pool_max=16),
    )
    monkeypatch.setattr(
        commands.service,
        "start_work_unit",
        lambda *a, **k: started.append("started") or {},
    )

    result = commands.start_work_unit("cpr_1")

    assert result["ok"] is False
    assert result["error"] == "connection_budget_exceeded"
    assert started == [], "no row may be written when the budget cannot hold the run"


def test_a_sufficient_budget_does_not_stop_the_door(monkeypatch: pytest.MonkeyPatch) -> None:
    from local_first_agent_os.work_units import commands

    monkeypatch.setattr(
        commands,
        "check_connection_budget",
        lambda: BudgetFits(server_max=300, in_use=40, pool_max=16),
    )
    monkeypatch.setattr(commands, "_harness_refusal", lambda: None)
    monkeypatch.setattr(commands.service, "start_work_unit", lambda *a, **k: {"work_unit_id": "w1"})

    result = commands.start_work_unit("cpr_1")

    assert result["ok"] is True
    assert result["work_unit_id"] == "w1"
