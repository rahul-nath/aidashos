# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Whether a failure is the ledger being unreachable, or the code being wrong.

A resident loop has to survive the first and must not survive the second. Those
are opposite responses to failures that arrive through the same call, so
something has to tell them apart, and this is the one place that does.

The distinction is drawn by exception *type* and nothing else. The repository
already classifies agent process failures by matching their text, because for a
subprocess's output text is genuinely all there is. Here it is not: a terminated
backend arrives as `psycopg.errors.AdminShutdown`, which is an
`OperationalError`, and reading its message instead would throw away the one
piece of evidence that cannot be phrased two ways.

What counts as unreachable is deliberately narrow. `OperationalError` is
psycopg's category for "the connection or the server, not your statement":
refused connections, a server that closed the socket, an administrator
terminating the backend. A malformed query is a `ProgrammingError` and an
integrity violation is an `IntegrityError`; neither is here, and neither should
be, because a loop that swallowed those would spin forever on a defect while
reporting that the database was down.
"""

from __future__ import annotations

from functools import cache

__all__ = ["LedgerUnavailable", "ledger_budget_exhausted", "ledger_unavailable"]


class LedgerUnavailable(RuntimeError):
    """The coordination ledger could not be reached.

    Raised where this repository detects the condition itself and would
    otherwise have to describe it in a message. A caller that means to keep
    working through an outage catches this and the driver's own errors together,
    through `ledger_unavailable`, rather than knowing which of the two it got.
    """


@cache
def _unavailable_types() -> tuple[type[BaseException], ...]:
    """The driver-level types that mean the database, not the statement.

    Resolved once and lazily. Both drivers are declared dependencies, so a miss
    here is not an expected condition; it is tolerated only so that importing
    this module can never be the thing that breaks a process, and a miss narrows
    what is recognised rather than widening it.
    """

    types: list[type[BaseException]] = [LedgerUnavailable]
    try:
        import psycopg
    except ImportError:  # pragma: no cover - dependency is declared
        pass
    else:
        types.append(psycopg.OperationalError)
    try:
        from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError
    except ImportError:  # pragma: no cover - dependency is declared
        pass
    else:
        # DBOS reaches the same Postgres through SQLAlchemy, so a resident loop
        # that launched DBOS can be handed either driver's spelling of the same
        # outage depending on which call noticed first.
        types.append(SQLAlchemyOperationalError)
    return tuple(types)


@cache
def _budget_types() -> tuple[type[BaseException], ...]:
    """Failures that are a bound somebody has to change, not an outage to wait out.

    `TooManyConnections` (SQLSTATE 53300) subclasses `OperationalError`, so it
    arrives through exactly the door this module opens for a server that closed
    the socket. It is not that. The ledger is up and answering; it is refusing
    one more client because the connection budget is spent.

    Treating it as unavailable is worse than merely wrong, because the response
    to unavailable is to wait and retry, and a retrying loop is itself a
    consumer of the budget it is waiting on. The condition never clears on its
    own, and the loop that would have reported a bound instead reports weather,
    forever.

    Recognised by type, like everything else here. That is the whole reason this
    is expressible: the driver already draws the distinction, and only the
    breadth of the `OperationalError` catch was hiding it.
    """

    try:
        from psycopg.errors import TooManyConnections
    except ImportError:  # pragma: no cover - dependency is declared
        return ()
    return (TooManyConnections,)


def ledger_budget_exhausted(error: BaseException) -> bool:
    """Whether `error` means the server's connection budget is spent."""

    return isinstance(error, _budget_types())


def ledger_unavailable(error: BaseException) -> bool:
    """Whether `error` means the ledger is unreachable rather than defective."""

    if ledger_budget_exhausted(error):
        return False
    return isinstance(error, _unavailable_types())
