# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read-only tallies of what DBOS has actually executed.

``dbos.workflow_status`` and ``dbos.operation_outputs`` are the durable record of
every workflow and step this application has ever run. Any "has X ever executed?"
question is decidable from them, and reading them beats reasoning about code or
asking an agent, because they record what happened rather than what should.

The system database also holds ``inputs``, ``output``, ``error``, and ``request``.
For ``durable_workflow_entrypoint`` the inputs are ingress event payloads, which is
real content, so this module is built so that a caller cannot reach them:

* The projection is closed. :data:`WORKFLOW_TALLY_SQL` and :data:`STEP_TALLY_SQL`
  are the only statements here, they name their columns as literals, and nothing
  in this module interpolates a caller value into either. There is no parameter
  that widens what comes back, so "return a payload" is not a state this code can
  represent.
* The column grant is the enforcement. ``scripts/grant_execution_ledger_reader.sql``
  provisions a role holding ``SELECT (name, status)`` and ``SELECT (function_name)``
  and nothing else, so a reader connected as that role is refused by Postgres if a
  future edit here ever asks for more. Point
  ``LOCAL_AGENT_LEDGER_READER_DATABASE_URL`` at it to hold that privilege; the
  admin URL is the operator's fallback and still cannot widen the projection.

The two layers answer different questions. The closed projection means this code
does not leak. The column grant means a *process* holding only the reader URL
cannot leak, whatever code it runs.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from .settings import Settings, get_settings

# Every column these statements read. Both are literals with no interpolation,
# which is what makes "the projection is closed" a property of the module rather
# than a promise about its callers.
WORKFLOW_TALLY_SQL: Final = (
    "SELECT name, status, count(*) AS execution_count "
    "FROM dbos.workflow_status GROUP BY name, status "
    "ORDER BY execution_count DESC, name, status"
)
STEP_TALLY_SQL: Final = (
    "SELECT function_name, count(*) AS execution_count "
    "FROM dbos.operation_outputs GROUP BY function_name "
    "ORDER BY execution_count DESC, function_name"
)

# Columns that carry content rather than metadata. Named so the guard test can
# assert against the statements above by content instead of by eye.
PAYLOAD_BEARING_COLUMNS: Final = ("inputs", "output", "error", "request")


@dataclass(frozen=True)
class WorkflowTally:
    """How many times one workflow reached one status."""

    workflow_name: str
    status: str
    execution_count: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "workflow_name": self.workflow_name,
            "status": self.status,
            "execution_count": self.execution_count,
        }


@dataclass(frozen=True)
class StepTally:
    """How many step checkpoints one function has recorded."""

    step_name: str
    execution_count: int

    def to_payload(self) -> dict[str, Any]:
        return {"step_name": self.step_name, "execution_count": self.execution_count}


@dataclass(frozen=True)
class ExecutionLedger:
    """What the durable record says, in full, as tallies."""

    workflows: tuple[WorkflowTally, ...]
    steps: tuple[StepTally, ...]

    def has_ever_executed(self, workflow_name: str) -> bool:
        """Whether the record holds any execution of this workflow, in any status.

        A workflow that only ever errored still executed. Callers asking "did this
        succeed" want :meth:`tallies_for` and a status of their choosing.
        """

        return any(tally.workflow_name == workflow_name for tally in self.workflows)

    def tallies_for(self, workflow_name: str) -> tuple[WorkflowTally, ...]:
        return tuple(tally for tally in self.workflows if tally.workflow_name == workflow_name)

    def to_payload(self) -> dict[str, Any]:
        return {
            "workflows": [tally.to_payload() for tally in self.workflows],
            "steps": [tally.to_payload() for tally in self.steps],
        }


@dataclass(frozen=True)
class LedgerUnavailable:
    """The record could not be read, and why.

    A stopped container, a database that has never been migrated, and an unset URL
    are all runtime conditions an operator recovers from, so they are values rather
    than exceptions. Nothing here covers a malformed row: that would be a
    programmer error and is left to crash.
    """

    reason: str

    def to_payload(self) -> dict[str, Any]:
        return {"reason": self.reason}


LedgerReading = ExecutionLedger | LedgerUnavailable


def ledger_reader_url(settings: Settings | None = None) -> str | None:
    """The URL to read the durable record with, most-restricted first.

    A process holding ``LOCAL_AGENT_LEDGER_READER_DATABASE_URL`` gets the column
    grant enforcing it. Falling back to the admin URL is the operator's path and is
    deliberately silent, because the projection is closed either way; the
    difference is only whether Postgres would also stop a future edit.
    """

    settings = settings or get_settings()
    return settings.ledger_reader_database_url or settings.dbos_system_database_url


def read_execution_ledger(settings: Settings | None = None) -> LedgerReading:
    """Tally the durable record, or say why it could not be read."""

    url = ledger_reader_url(settings)
    if not url:
        return LedgerUnavailable(
            reason=(
                "no DBOS system database is configured; set "
                "LOCAL_AGENT_LEDGER_READER_DATABASE_URL or "
                "LOCAL_AGENT_DBOS_SYSTEM_DATABASE_URL"
            )
        )
    try:
        import psycopg
    except Exception as exc:  # pragma: no cover - dependency is declared
        raise RuntimeError("psycopg is required to read the durable execution ledger") from exc

    try:
        with psycopg.connect(_libpq_url(url)) as connection, connection.cursor() as cursor:
            cursor.execute(WORKFLOW_TALLY_SQL)
            workflow_rows = cursor.fetchall()
            cursor.execute(STEP_TALLY_SQL)
            step_rows = cursor.fetchall()
    except psycopg.Error as exc:
        return LedgerUnavailable(reason=_readable_failure(exc))
    return build_execution_ledger(workflow_rows, step_rows)


def build_execution_ledger(
    workflow_rows: Iterable[Sequence[Any]],
    step_rows: Iterable[Sequence[Any]],
) -> ExecutionLedger:
    """Turn positional rows into tallies.

    Split from the connection so the row shape can be tested without a server, and
    positional rather than keyed because the statements above fix the order and a
    key would invite someone to add a column to it.
    """

    return ExecutionLedger(
        workflows=tuple(
            WorkflowTally(
                workflow_name=str(row[0]),
                status=str(row[1]),
                execution_count=int(row[2]),
            )
            for row in workflow_rows
        ),
        steps=tuple(
            StepTally(step_name=str(row[0]), execution_count=int(row[1])) for row in step_rows
        ),
    )


def _libpq_url(url: str) -> str:
    """Strip the SQLAlchemy driver marker psycopg does not accept."""

    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _readable_failure(exc: Exception) -> str:
    """Name the cause an operator acts on, then carry the driver's own words.

    A refused connection and a missing table need different fixes, and the driver
    message alone leads with neither.
    """

    text = str(exc).strip() or exc.__class__.__name__
    lowered = text.lower()
    if "could not connect" in lowered or "connection refused" in lowered:
        cause = "the DBOS system database is not accepting connections"
    elif "does not exist" in lowered and "dbos." in lowered:
        cause = "the dbos schema is not present; no DBOS application has run against this database"
    elif "permission denied" in lowered:
        cause = (
            "the reader role lacks its column grant; re-run "
            "scripts/grant_execution_ledger_reader.sql"
        )
    else:
        cause = "the durable execution ledger could not be read"
    return f"{cause}: {text}"


__all__ = [
    "PAYLOAD_BEARING_COLUMNS",
    "STEP_TALLY_SQL",
    "WORKFLOW_TALLY_SQL",
    "ExecutionLedger",
    "LedgerReading",
    "LedgerUnavailable",
    "StepTally",
    "WorkflowTally",
    "build_execution_ledger",
    "ledger_reader_url",
    "read_execution_ledger",
]
