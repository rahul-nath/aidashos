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
from typing import Any, assert_never

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


class RuntimeProcessPresence(StrEnum):
    """Whether a ledger state says stopping can destroy a live process."""

    LIVE = "live"
    ABSENT = "absent"


def _dispatch_intent_process(status: DispatchIntentStatus) -> RuntimeProcessPresence:
    """Classify every intent state by whether its claimed process may be live."""

    match status:
        case DispatchIntentStatus.CLAIMED | DispatchIntentStatus.IN_PROGRESS:
            return RuntimeProcessPresence.LIVE
        case (
            DispatchIntentStatus.PENDING
            | DispatchIntentStatus.CHECKPOINT_REVIEW
            | DispatchIntentStatus.PAUSED
            | DispatchIntentStatus.DONE
            | DispatchIntentStatus.FAILED
            | DispatchIntentStatus.CANCELED
            | DispatchIntentStatus.SUPERSEDED
        ):
            return RuntimeProcessPresence.ABSENT
    assert_never(status)


def _execution_lease_process(status: LeaseStatus) -> RuntimeProcessPresence:
    """Classify every lease state by whether its agent process may be live."""

    match status:
        case LeaseStatus.ACTIVE | LeaseStatus.CANCEL_REQUESTED:
            return RuntimeProcessPresence.LIVE
        case (
            LeaseStatus.COMPLETED
            | LeaseStatus.FAILED
            | LeaseStatus.TIMED_OUT
            | LeaseStatus.CANCELED
            | LeaseStatus.COMPENSATED
        ):
            return RuntimeProcessPresence.ABSENT
    assert_never(status)


def _work_unit_process(status: WorkUnitStatus) -> RuntimeProcessPresence:
    """Classify every WorkUnit state by whether its execution may be live."""

    match status:
        case WorkUnitStatus.RUNNING | WorkUnitStatus.CANCELLING:
            return RuntimeProcessPresence.LIVE
        case (
            WorkUnitStatus.DRAFT
            | WorkUnitStatus.COMPILED
            | WorkUnitStatus.QUEUED
            | WorkUnitStatus.WAITING_FOR_OPERATOR
            | WorkUnitStatus.BLOCKED
            | WorkUnitStatus.SUCCEEDED
            | WorkUnitStatus.FAILED
            | WorkUnitStatus.CANCELLED
            | WorkUnitStatus.SUPERSEDED
        ):
            return RuntimeProcessPresence.ABSENT
    assert_never(status)


# Derive the SQL partitions from exhaustive classifiers. Adding an enum member
# now fails type checking at its classifier instead of silently authorizing a
# shutdown while that new state has a live process.
_BUSY_INTENT_STATUSES = tuple(
    status
    for status in DispatchIntentStatus
    if _dispatch_intent_process(status) is RuntimeProcessPresence.LIVE
)
_BUSY_LEASE_STATUSES = tuple(
    status
    for status in LeaseStatus
    if _execution_lease_process(status) is RuntimeProcessPresence.LIVE
)
_BUSY_WORK_UNIT_STATUSES = tuple(
    status for status in WorkUnitStatus if _work_unit_process(status) is RuntimeProcessPresence.LIVE
)

# Live work is small by construction, but a run that died without recording a
# halt leaves its lease ACTIVE until `recover_dead_execution` says otherwise, so
# the live set is not bounded by what is actually running. The cap keeps a
# refusal readable; the extra row makes truncation visible rather than silent.
_MAX_REPORTED_FACTS = 8

# One statement, three status-indexed scans, no join to history. Every table
# here has an index leading with `status`, so the cost follows the amount of
# live work and not the size of the ledger.
_LIVE_WORK_QUERY = f"""
SELECT '{RuntimeWorkKind.DISPATCH_INTENT.value}' AS fact_kind,
       intent_id AS identifier, status FROM dispatch_intents
  WHERE status IN ({sql_status_list(*_BUSY_INTENT_STATUSES)})
UNION ALL
SELECT '{RuntimeWorkKind.EXECUTION_LEASE.value}', lease_id, status FROM agent_execution_leases
  WHERE status IN ({sql_status_list(*_BUSY_LEASE_STATUSES)})
UNION ALL
SELECT '{RuntimeWorkKind.WORK_UNIT.value}', work_unit_id, status FROM work_units
  WHERE status IN ({sql_status_list(*_BUSY_WORK_UNIT_STATUSES)})
LIMIT {_MAX_REPORTED_FACTS + 1}
"""

# Closing a terminal must not wait for the pool's normal 30-second checkout.
# Giving up quickly may retain an idle runtime; waiting or guessing idle may
# destroy live work.
_CHECKOUT_TIMEOUT_SECONDS = 1.0


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
    read_failure: Exception | None = None
    rows: list[Any] = []
    try:
        rows = connection.execute(_LIVE_WORK_QUERY).fetchall()
    except Exception as exc:
        read_failure = exc

    cleanup_failures: list[Exception] = []
    try:
        connection.rollback()
    except Exception as exc:
        cleanup_failures.append(exc)
    try:
        connection.close()
    except Exception as exc:
        cleanup_failures.append(exc)

    failures = ([read_failure] if read_failure is not None else []) + cleanup_failures
    if failures:
        reason = "; ".join(f"{type(exc).__name__}: {exc}" for exc in failures)
        return RuntimeActivityUnknown(reason=reason)
    if not rows:
        return RuntimeActivityIdle()
    truncated = len(rows) > _MAX_REPORTED_FACTS
    try:
        facts = tuple(_fact_of(row) for row in rows[:_MAX_REPORTED_FACTS])
    except Exception as exc:
        return RuntimeActivityUnknown(reason=f"{type(exc).__name__}: {exc}")
    return RuntimeActivityBusy(
        facts=facts,
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
    "RuntimeProcessPresence",
    "RuntimeWorkFact",
    "RuntimeWorkKind",
    "answer_of",
    "read_runtime_activity",
    "render_runtime_activity",
]
