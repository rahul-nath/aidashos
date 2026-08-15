# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Whether this runtime is currently doing work, asked of the ledger.

`leave_session` used to stop the whole runtime - both resident loops, the API,
the web server, and the Docker Compose infrastructure including Postgres -
because the last terminal closed. The count it consulted was of terminals, not
of work, so closing a window while a milestone was executing took the ledger out
from under a live frontier process that had already spent quota.

The runtime's lifetime should follow the work it is doing, so something has to
answer what that work is. That answer is here rather than in the shell hook
because it is a question about the coordination ledger, and because it is worth
being able to ask it, and test it, without a terminal.

Three answers, not two. An unreadable ledger is `RuntimeActivityUnknown`, never
`RuntimeActivityIdle`: the caller is deciding whether to run a destructive stop,
and "I could not tell" must not authorise that. This is deliberately the
opposite polarity from `resident-loop-owners.sh`, where an unanswerable query
and an unowned loop both mean proceed - there, proceeding costs a logged no-op;
here it costs the work.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..contracts import DispatchIntentStatus, LeaseStatus
from ..work_units.lifecycle import WorkUnitStatus
from .store import connect, sql_status_list


class RuntimeWorkKind(StrEnum):
    """The three ledger facts that can hold a runtime up.

    One member per table read below. A fact names its kind so an operator
    reading a refusal knows which record to go look at, rather than being told
    only that something is live.
    """

    DISPATCH_INTENT = "dispatch_intent"
    EXECUTION_LEASE = "execution_lease"
    WORK_UNIT = "work_unit"


# What each column has to hold for a process to be live behind it.
#
# Included: a CLAIMED or IN_PROGRESS intent has an executor; an ACTIVE lease is
# heartbeating and a CANCEL_REQUESTED one is still being torn down; a RUNNING
# work unit is executing and a CANCELLING one has not finished stopping. In each
# case something is running right now that a shutdown would destroy rather than
# defer.
#
# Excluded, deliberately: PENDING intents and QUEUED work units, because nothing
# has claimed them and a stop leaves them exactly where they are; PAUSED and
# CHECKPOINT_REVIEW intents and WAITING_FOR_OPERATOR or BLOCKED work units,
# because those stopped on purpose pending a person and hold no process; and
# every terminal status, because those are over. Excluding them keeps this
# answer to "work would be destroyed", which is the only thing that justifies
# leaving a runtime the operator did not ask to keep.
_BUSY_INTENT_STATUSES = (DispatchIntentStatus.CLAIMED, DispatchIntentStatus.IN_PROGRESS)
_BUSY_LEASE_STATUSES = (LeaseStatus.ACTIVE, LeaseStatus.CANCEL_REQUESTED)
_BUSY_WORK_UNIT_STATUSES = (WorkUnitStatus.RUNNING, WorkUnitStatus.CANCELLING)

# Live work is small by construction, but a run that died without recording a
# halt leaves its lease ACTIVE until `recover_dead_execution` says otherwise, so
# the live set is not bounded by what is actually running. The cap keeps a
# refusal readable; the extra row makes truncation visible rather than silent.
_MAX_REPORTED_FACTS = 8

# One statement, three status-indexed scans, no join to history. Every table
# here has an index leading with `status`, so the cost follows the amount of
# live work and not the size of the ledger.
_LIVE_WORK_QUERY = f"""
SELECT 'dispatch_intent' AS fact_kind, intent_id AS identifier, status FROM dispatch_intents
  WHERE status IN ({sql_status_list(*_BUSY_INTENT_STATUSES)})
UNION ALL
SELECT 'execution_lease', lease_id, status FROM agent_execution_leases
  WHERE status IN ({sql_status_list(*_BUSY_LEASE_STATUSES)})
UNION ALL
SELECT 'work_unit', work_unit_id, status FROM work_units
  WHERE status IN ({sql_status_list(*_BUSY_WORK_UNIT_STATUSES)})
LIMIT {_MAX_REPORTED_FACTS + 1}
"""

# A caller on this path is closing a terminal. The pool waits 30 seconds for a
# connection by default and keeps retrying a refused one for the whole wait, and
# a Postgres that is already down is exactly the case this check has to answer
# quickly rather than correctly. Giving up early costs an idle runtime nobody
# needed; waiting costs the operator a hung terminal every time.
_CHECKOUT_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True, slots=True)
class RuntimeWorkFact:
    """One live ledger row, named well enough to go look at."""

    kind: RuntimeWorkKind
    identifier: str
    status: str

    def describe(self) -> str:
        return f"{self.kind.value} {self.identifier} is {self.status}"


@dataclass(frozen=True, slots=True)
class RuntimeActivityBusy:
    """Work is in flight, and these rows are it."""

    facts: tuple[RuntimeWorkFact, ...]
    truncated: bool = False

    def describe(self) -> list[str]:
        lines = [fact.describe() for fact in self.facts]
        if self.truncated:
            lines.append(f"more live rows than the {_MAX_REPORTED_FACTS} listed")
        return lines


@dataclass(frozen=True, slots=True)
class RuntimeActivityIdle:
    """No dispatch intent, execution lease, or work unit is live."""

    def describe(self) -> list[str]:
        return []


@dataclass(frozen=True, slots=True)
class RuntimeActivityUnknown:
    """The ledger could not be read, so nothing may be concluded from it."""

    reason: str

    def describe(self) -> list[str]:
        return [f"the coordination ledger could not be read: {self.reason}"]


RuntimeActivity = RuntimeActivityBusy | RuntimeActivityIdle | RuntimeActivityUnknown


class RuntimeActivityAnswer(StrEnum):
    """The word a caller branches on.

    A shell caller cannot hold a sum type, so this is the wire form of one. It
    is a closed vocabulary rather than an exit status because a caller that does
    not recognise the word must be able to fall back to `UNKNOWN`, and an
    unrecognised exit status looks like a crash.
    """

    BUSY = "busy"
    IDLE = "idle"
    UNKNOWN = "unknown"


def answer_of(activity: RuntimeActivity) -> RuntimeActivityAnswer:
    match activity:
        case RuntimeActivityBusy():
            return RuntimeActivityAnswer.BUSY
        case RuntimeActivityIdle():
            return RuntimeActivityAnswer.IDLE
        case RuntimeActivityUnknown():
            return RuntimeActivityAnswer.UNKNOWN


def _fact_of(row: Any) -> RuntimeWorkFact:
    return RuntimeWorkFact(
        kind=RuntimeWorkKind(row["fact_kind"]),
        identifier=str(row["identifier"]),
        status=str(row["status"]),
    )


def read_runtime_activity() -> RuntimeActivity:
    """Ask the ledger, once, whether anything would be destroyed by a stop.

    Read-only and side-effect free, so anyone may run it at any time. Every
    failure - no database URL configured, Postgres down, schema missing, a
    driver error - lands on `RuntimeActivityUnknown` with the reason attached,
    because to a caller they are the same fact: this did not answer.
    """

    try:
        connection = connect(checkout_timeout_seconds=_CHECKOUT_TIMEOUT_SECONDS)
    except Exception as exc:
        return RuntimeActivityUnknown(reason=f"{type(exc).__name__}: {exc}")
    try:
        rows = connection.execute(_LIVE_WORK_QUERY).fetchall()
    except Exception as exc:
        return RuntimeActivityUnknown(reason=f"{type(exc).__name__}: {exc}")
    finally:
        # Read-only, so there is nothing to commit; the rollback ends the
        # transaction the read opened before the connection goes back to the
        # pool holding it.
        try:
            connection.rollback()
        finally:
            connection.close()
    if not rows:
        return RuntimeActivityIdle()
    truncated = len(rows) > _MAX_REPORTED_FACTS
    return RuntimeActivityBusy(
        facts=tuple(_fact_of(row) for row in rows[:_MAX_REPORTED_FACTS]),
        truncated=truncated,
    )


def render_runtime_activity(activity: RuntimeActivity) -> str:
    """The form a shell hook reads: the answer on line one, then the facts.

    Line one is the whole decision, so a caller parses one word and never has to
    understand the rest. The remaining lines exist for the operator who reads
    the refusal afterwards and wants to know which lease or intent held the
    runtime up.
    """

    return "\n".join([answer_of(activity).value, *activity.describe()])


__all__ = [
    "RuntimeActivity",
    "RuntimeActivityAnswer",
    "RuntimeActivityBusy",
    "RuntimeActivityIdle",
    "RuntimeActivityUnknown",
    "RuntimeWorkFact",
    "RuntimeWorkKind",
    "answer_of",
    "read_runtime_activity",
    "render_runtime_activity",
]
