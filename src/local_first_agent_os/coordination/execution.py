# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Execution leases, durable events, and ledger retention."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

from local_first_agent_os.constants import (
    AGENT_BRANCH_AUTO_MERGE,
    DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
    DISPATCH_RETRY_POLICY,
)

from ..contracts import DispatchIntentStatus, LeaseStatus, LedgerEventStatus, TaskStatus
from .contracts import ExecutionLeaseTerminalStatus
from .outcomes import (
    AgentStatus,
    ExecutionActivityStatus,
    FailureCategory,
    PersistenceStatus,
    SupervisorStatus,
    TerminalOutcome,
    classify_failure,
    failure_category,
)
from .store import (
    ConnectionLike,
    connect,
    decode_json_array,
    decode_json_object,
    emit,
    err,
    events_path,
    iso,
    now,
    ok,
    rowdict,
    sql_status_list,
    tx,
)

logger = logging.getLogger(__name__)

_LEASE_ACTIVE_STATUSES = {"ACTIVE", "CANCEL_REQUESTED"}
LEASE_TERMINAL_STATUSES = {
    "COMPLETED",
    "FAILED",
    "TIMED_OUT",
    "CANCELED",
    "COMPENSATED",
}
_LEASE_STATUSES = _LEASE_ACTIVE_STATUSES | LEASE_TERMINAL_STATUSES


_LEASE_LIVE = sql_status_list(LeaseStatus.ACTIVE, LeaseStatus.CANCEL_REQUESTED)
_LEASE_TERMINAL = sql_status_list(*ExecutionLeaseTerminalStatus)
# An abandoned outbox row is settled: nothing will ever claim or resolve it.
_LEDGER_SETTLED = sql_status_list(
    LedgerEventStatus.PROCESSED, LedgerEventStatus.FAILED, LedgerEventStatus.ABANDONED
)
_DISPATCH_SETTLED_SQL = sql_status_list(
    DispatchIntentStatus.DONE, DispatchIntentStatus.FAILED, DispatchIntentStatus.CANCELED
)
# Superseded intents are collectable too: nothing will ever run them.
_DISPATCH_COLLECTABLE = sql_status_list(
    DispatchIntentStatus.DONE,
    DispatchIntentStatus.FAILED,
    DispatchIntentStatus.CANCELED,
    DispatchIntentStatus.SUPERSEDED,
)


def _json_arg(raw: str | None, *, default: Any) -> str:
    if raw is None:
        return json.dumps(default, sort_keys=True)
    parsed = json.loads(raw)
    return json.dumps(parsed, sort_keys=True)


def execution_lease_to_dict(r: dict[str, Any]) -> dict[str, Any]:
    d = rowdict(r)
    for key in ("created_at", "heartbeat_at", "lease_expires_at"):
        d[key] = iso(d[key])
    for key in (
        "cancel_requested_at",
        "completed_at",
        "last_meaningful_progress_at",
        "progress_assessed_at",
    ):
        if d.get(key):
            d[key] = iso(d[key])
    d["command"] = decode_json_array(d.get("command_json"))
    d["compensation"] = decode_json_object(d.get("compensation_json"))
    d["result"] = decode_json_object(d.get("result_json"))
    d["progress_assessment_decision"] = decode_json_object(
        d.get("progress_assessment_decision_json")
    )
    return d


def open_execution_lease(
    idempotency_key: str,
    worker_id: str,
    intent_id: str | None = None,
    task_id: str | None = None,
    agent_tier: str | None = None,
    agent_name: str | None = None,
    worktree_path: str | None = None,
    command_json: str | None = None,
    compensation_json: str | None = None,
    timeout_seconds: int = DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Create or return the lease for a long-running external agent process."""

    if not idempotency_key.strip():
        return err("invalid_idempotency_key", message="idempotency_key is required")
    if not worker_id.strip():
        return err("invalid_worker_id", message="worker_id is required")
    if timeout_seconds <= 0:
        return err("invalid_timeout_seconds", timeout_seconds=timeout_seconds)
    command = _json_arg(command_json, default=[])
    compensation = _json_arg(compensation_json, default={})
    lease_id = str(uuid.uuid4())
    t = now()
    expires_at = t + timeout_seconds
    created = False
    with tx() as c:
        existing = c.execute(
            "SELECT * FROM agent_execution_leases WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            lease = existing
        else:
            c.execute(
                f"""
                INSERT INTO agent_execution_leases(
                    lease_id, idempotency_key, intent_id, task_id, worker_id,
                    agent_tier, agent_name, worktree_path, command_json, status,
                    timeout_seconds, lease_expires_at, compensation_json,
                    created_at, heartbeat_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{LeaseStatus.ACTIVE}', ?, ?, ?, ?, ?)
                """,
                (
                    lease_id,
                    idempotency_key,
                    intent_id,
                    task_id,
                    worker_id,
                    agent_tier,
                    agent_name,
                    worktree_path,
                    command,
                    timeout_seconds,
                    expires_at,
                    compensation,
                    t,
                    t,
                ),
            )
            created = True
            lease = c.execute(
                "SELECT * FROM agent_execution_leases WHERE lease_id=?",
                (lease_id,),
            ).fetchone()
    data = ok(created=created, lease=execution_lease_to_dict(lease))
    emit("open_execution_lease", {"lease_id": data["lease"]["lease_id"], "created": created})
    return data


def heartbeat_execution_lease(
    lease_id: str,
    worker_id: str,
) -> dict[str, Any]:
    """Refresh a live execution lease and report whether cancel was requested."""

    t = now()
    with tx() as c:
        row = c.execute(
            "SELECT * FROM agent_execution_leases WHERE lease_id=?",
            (lease_id,),
        ).fetchone()
        if not row:
            return err("not_found", lease_id=lease_id)
        if row["worker_id"] != worker_id:
            return err(
                "worker_mismatch",
                lease_id=lease_id,
                expected_worker_id=row["worker_id"],
                worker_id=worker_id,
            )
        if row["status"] not in _LEASE_ACTIVE_STATUSES:
            return err("not_active", lease_id=lease_id, status=row["status"])
        expires_at = t + int(row["timeout_seconds"])
        c.execute(
            """
            UPDATE agent_execution_leases
            SET heartbeat_at=?, lease_expires_at=?
            WHERE lease_id=?
            """,
            (t, expires_at, lease_id),
        )
        updated = c.execute(
            "SELECT * FROM agent_execution_leases WHERE lease_id=?",
            (lease_id,),
        ).fetchone()
    lease = execution_lease_to_dict(updated)
    data = ok(lease=lease, cancel_requested=lease["status"] == "CANCEL_REQUESTED")
    emit("heartbeat_execution_lease", {"lease_id": lease_id, "worker_id": worker_id})
    return data


def request_execution_cancel(
    lease_id: str,
    reason: str | None = None,
    requested_by: str = "operator",
) -> dict[str, Any]:
    """Ask a worker to stop at its next heartbeat/checkpoint."""

    t = now()
    message = reason or "cancel requested"
    with tx() as c:
        row = c.execute(
            "SELECT * FROM agent_execution_leases WHERE lease_id=?",
            (lease_id,),
        ).fetchone()
        if not row:
            return err("not_found", lease_id=lease_id)
        if row["status"] in LEASE_TERMINAL_STATUSES:
            return err("already_terminal", lease_id=lease_id, status=row["status"])
        c.execute(
            """
            UPDATE agent_execution_leases
            SET status='CANCEL_REQUESTED', cancel_requested_at=?, error=?
            WHERE lease_id=?
            """,
            (t, f"{requested_by}: {message}", lease_id),
        )
        updated = c.execute(
            "SELECT * FROM agent_execution_leases WHERE lease_id=?",
            (lease_id,),
        ).fetchone()
    data = ok(lease=execution_lease_to_dict(updated), requested_by=requested_by)
    emit(
        "request_execution_cancel",
        {"lease_id": lease_id, "requested_by": requested_by, "reason": message},
    )
    return data


def complete_execution_lease(
    lease_id: str,
    status: str,
    result_json: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Resolve an execution lease with an explicit terminal status."""

    if status not in LEASE_TERMINAL_STATUSES:
        return err(
            "invalid_status",
            status=status,
            allowed=sorted(LEASE_TERMINAL_STATUSES),
        )
    result = _json_arg(result_json, default={})
    result_data = decode_json_object(result)
    if status == ExecutionLeaseTerminalStatus.COMPLETED:
        outcome = TerminalOutcome.AUTOMATED_COMPLETION
    elif status == ExecutionLeaseTerminalStatus.CANCELED:
        outcome = TerminalOutcome.OPERATOR_CANCELED
    elif status == "TIMED_OUT":
        outcome = TerminalOutcome.DEADLINE_EXCEEDED
    elif status == "COMPENSATED":
        outcome = TerminalOutcome.COMPENSATED
    else:
        outcome = classify_failure(error or result)
    agent_status = str(
        result_data.get(
            "agent_status",
            AgentStatus.COMPLETED.value
            if status == ExecutionLeaseTerminalStatus.COMPLETED
            else (
                AgentStatus.CANCELED.value
                if status == ExecutionLeaseTerminalStatus.CANCELED
                else AgentStatus.FAILED.value
            ),
        )
    )
    agent_failure = result_data.get("agent_failure")
    if agent_failure is None and agent_status == AgentStatus.FAILED.value:
        agent_failure = outcome.value
    category = result_data.get("agent_failure_category")
    if category is None:
        inferred_category = failure_category(str(agent_failure) if agent_failure else None)
        category = inferred_category.value if inferred_category else None
    supervisor_status = str(result_data.get("supervisor_status", SupervisorStatus.COMPLETED.value))
    supervisor_failure = result_data.get("supervisor_failure")
    persistence_status = str(
        result_data.get("persistence_status", PersistenceStatus.COMPLETED.value)
    )
    persistence_failure = result_data.get("persistence_failure")
    next_action = result_data.get("next_action")
    if category is not None and str(category) not in FailureCategory._value2member_map_:
        return err("invalid_failure_category", category=category)
    t = now()
    with tx() as c:
        row = c.execute(
            "SELECT * FROM agent_execution_leases WHERE lease_id=?",
            (lease_id,),
        ).fetchone()
        if not row:
            return err("not_found", lease_id=lease_id)
        if row["status"] in LEASE_TERMINAL_STATUSES:
            return err("already_terminal", lease_id=lease_id, status=row["status"])
        c.execute(
            """
            UPDATE agent_execution_leases
            SET status=?, outcome=?, result_json=?, error=?, completed_at=?, heartbeat_at=?,
                agent_status=?, agent_failure_category=?, agent_failure=?,
                supervisor_status=?, supervisor_failure=?, persistence_status=?,
                persistence_failure=?, next_action=?, activity_status='TERMINAL'
            WHERE lease_id=?
            """,
            (
                status,
                outcome.value,
                result,
                error,
                t,
                t,
                agent_status,
                category,
                agent_failure,
                supervisor_status,
                supervisor_failure,
                persistence_status,
                persistence_failure,
                next_action,
                lease_id,
            ),
        )
        updated = c.execute(
            "SELECT * FROM agent_execution_leases WHERE lease_id=?",
            (lease_id,),
        ).fetchone()
    data = ok(lease=execution_lease_to_dict(updated))
    emit("complete_execution_lease", {"lease_id": lease_id, "status": status})
    return data


def live_execution_leases_for_intent(intent_id: str) -> list[dict[str, Any]]:
    """The leases for one dispatch intent that can still be acting.

    Returns rows rather than a command envelope because the caller is application
    code deciding what to stop, not an operator reading a result.

    `agent_execution_leases.intent_id` is a real foreign key, so this is the whole
    link from "a milestone asked for work" to "a process is doing it". Without it
    a cancellation can flip ledger rows and never reach the agent, which is
    exactly the gap this exists to close.
    """

    with connect() as c:
        rows = c.execute(
            "SELECT * FROM agent_execution_leases "
            f"WHERE intent_id=? AND status IN ({_LEASE_LIVE}) ORDER BY created_at",
            (intent_id,),
        ).fetchall()
    return [execution_lease_to_dict(row) for row in rows]


USAGE_LIMIT_EVIDENCE_ROW_CAP = 500
"""How many failure rows one spent-quota read is allowed to return.

The window bounds the read against the age of the ledger; this bounds it against
a failure storm inside the window. The subject is failure rows and nothing else,
because a busy machine heartbeats hundreds of live leases, and an earlier cap
over all recent leases let those live rows starve the handful of terminal ones
the caller's rule actually reads - dispatch sailed straight back to a spent
provider precisely when the machine was busiest.
"""


def agent_failure_leases_since(
    cutoff: float,
    *,
    failure: str,
    limit: int = USAGE_LIMIT_EVIDENCE_ROW_CAP,
    checkout_timeout_seconds: float | None = None,
) -> list[dict[str, Any]]:
    """Terminal leases whose agent reported ``failure`` at or after ``cutoff``.

    Rows rather than a command envelope, like ``live_execution_leases_for_intent``:
    the caller is application code deciding where to send work, not an operator
    reading a result.

    Filtered on the ``agent_failure`` column because that is the caller's subject.
    Both terminal writers stamp it alongside ``completed_at`` and ``heartbeat_at``
    in one statement, so every row the caller's rule could accept carries a
    ``completed_at`` inside the window and one sargable predicate covers them all.
    The column can also hold an outcome-inferred value whose result payload says
    nothing; callers re-read the payload, so the extra rows cost a filter and
    never a wrong answer.

    Newest first, so if the cap ever truncates it drops the oldest evidence in
    the window - and it says so, because a silently truncated read is how this
    query's predecessor reintroduced the bug it existed to fix.
    """

    with connect(checkout_timeout_seconds=checkout_timeout_seconds) as c:
        rows = c.execute(
            "SELECT * FROM agent_execution_leases "
            "WHERE agent_failure = ? AND completed_at >= ? "
            "ORDER BY completed_at DESC LIMIT ?",
            (failure, cutoff, limit),
        ).fetchall()
    if len(rows) == limit:
        logger.warning(
            "agent_failure_leases_truncated",
            extra={
                "detail": (
                    f"{limit} rows with agent_failure={failure!r} since {cutoff}; "
                    "older evidence in the window was dropped"
                )
            },
        )
    return [execution_lease_to_dict(row) for row in rows]


def list_execution_leases(status_filter: str | None = None) -> dict[str, Any]:
    """List long-running agent execution leases, optionally filtered."""

    if status_filter is not None and status_filter not in _LEASE_STATUSES:
        return err("invalid_status", status=status_filter, allowed=sorted(_LEASE_STATUSES))
    with connect() as c:
        if status_filter:
            rows = c.execute(
                "SELECT * FROM agent_execution_leases WHERE status=? ORDER BY created_at DESC",
                (status_filter,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM agent_execution_leases ORDER BY created_at DESC"
            ).fetchall()
    return ok(leases=[execution_lease_to_dict(r) for r in rows])


_LEDGER_EVENT_STATUSES = {status.value for status in LedgerEventStatus}

# A ledger event no consumer has claimed in this long is not in-flight work, it
# is a row nothing will ever drain. Outbox consumers claim within seconds when
# one is running, so a full day is generous: it is the analogue of a lease's
# per-row lease_expires_at, which ledger_events has no column for.
WORK_ABANDONED_AFTER_SECONDS = 86_400


def ledger_event_to_dict(r: dict[str, Any]) -> dict[str, Any]:
    d = rowdict(r)
    d["created_at"] = iso(d["created_at"])
    for key in ("claimed_at", "processed_at"):
        if d.get(key):
            d[key] = iso(d[key])
    d["payload"] = decode_json_object(d.get("payload_json"))
    return d


def claim_next_ledger_event(
    claimed_by: str,
    event_type: str | None = None,
) -> dict[str, Any]:
    """Atomically claim the oldest PENDING ledger event."""

    t = now()
    with tx() as c:
        clauses = [f"status = '{LedgerEventStatus.PENDING}'"]
        params: list[Any] = []
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type)
        where = " AND ".join(clauses)
        row = c.execute(
            f"SELECT * FROM ledger_events WHERE {where} ORDER BY created_at"
            " LIMIT 1 FOR UPDATE SKIP LOCKED",
            params,
        ).fetchone()
        if not row:
            return ok(event=None)
        cur = c.execute(
            "UPDATE ledger_events "
            f"SET status='{LedgerEventStatus.CLAIMED}', claimed_by=?, claimed_at=?, "
            "attempts=attempts + 1 "
            f"WHERE event_id=? AND status='{LedgerEventStatus.PENDING}'",
            (claimed_by, t, row["event_id"]),
        )
        if cur.rowcount != 1:
            return ok(event=None)
        claimed = c.execute(
            "SELECT * FROM ledger_events WHERE event_id=?", (row["event_id"],)
        ).fetchone()
    data = ok(event=ledger_event_to_dict(claimed))
    emit("claim_ledger_event", {"event_id": row["event_id"], "claimed_by": claimed_by})
    return data


def complete_ledger_event(
    event_id: str,
    status: str,
    error: str | None = None,
) -> dict[str, Any]:
    """Resolve a claimed ledger event as PROCESSED or FAILED."""

    if status not in {"PROCESSED", "FAILED"}:
        return err("invalid_status", status=status, allowed=["PROCESSED", "FAILED"])
    t = now()
    with tx() as c:
        row = c.execute("SELECT * FROM ledger_events WHERE event_id=?", (event_id,)).fetchone()
        if not row:
            return err("not_found", event_id=event_id)
        c.execute(
            "UPDATE ledger_events SET status=?, error=?, processed_at=? WHERE event_id=?",
            (status, error, t, event_id),
        )
    data = ok(event_id=event_id, status=status, processed_at=iso(t))
    emit("complete_ledger_event", data)
    return data


def list_ledger_events(status_filter: str | None = None) -> dict[str, Any]:
    """List durable ledger outbox events, optionally filtered by status."""

    if status_filter is not None and status_filter not in _LEDGER_EVENT_STATUSES:
        return err(
            "invalid_status",
            status=status_filter,
            allowed=sorted(_LEDGER_EVENT_STATUSES),
        )
    with connect() as c:
        if status_filter:
            rows = c.execute(
                "SELECT * FROM ledger_events WHERE status=? ORDER BY created_at DESC",
                (status_filter,),
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM ledger_events ORDER BY created_at DESC").fetchall()
    return ok(events=[ledger_event_to_dict(r) for r in rows])


# ---------------------------------------------------------------------------
# Retention GC (prune dead rows so append-only tables do not grow unbounded)
# ---------------------------------------------------------------------------


def _prune_events_file(cutoff_epoch: float) -> int:
    """Rewrite events.jsonl keeping only records at or after the cutoff.

    Atomic via temp-file plus replace. Best run when the ledger is idle: an
    append racing the rewrite window could be dropped. Unparseable lines are
    kept rather than silently discarded. Returns the number of lines removed.
    """
    path = events_path()
    if not path.exists():
        return 0
    kept: list[str] = []
    removed = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            ts_epoch = datetime.fromisoformat(record["ts"]).timestamp()
        except (json.JSONDecodeError, KeyError, ValueError):
            kept.append(line)
            continue
        if ts_epoch >= cutoff_epoch:
            kept.append(line)
        else:
            removed += 1
    tmp = path.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    tmp.replace(path)
    return removed


def _sweep_abandoned_work(c: ConnectionLike, t: float, expiry_seconds: int) -> dict[str, int]:
    """Terminalize every row a dead worker left behind, and report the counts.

    One rule applied to four tables: a row held in a working state past the
    deadline of whatever proves its holder is alive is not in-flight, it is
    debris. Each table gets the strongest liveness evidence it actually has, so
    the sweeps run in dependence order. Leases go first because they carry a
    real per-row deadline and the intent sweep reads their fresh statuses.
    """

    cutoff = t - expiry_seconds
    swept: dict[str, int] = {}
    expired_payload = json.dumps(
        {
            "schema_version": "external_agent_execution_attempt.v1",
            "status": LeaseStatus.TIMED_OUT.value,
            "failure_reason": "lease_expired",
            "retry_policy": DISPATCH_RETRY_POLICY,
            "auto_merge": AGENT_BRANCH_AUTO_MERGE,
        },
        sort_keys=True,
    )
    # A lease renews lease_expires_at on every heartbeat, so the column is
    # proof of life and no external window is needed.
    swept["expired_execution_leases"] = c.execute(
        f"""
        UPDATE agent_execution_leases
        SET status='{LeaseStatus.TIMED_OUT}',
            activity_status='{ExecutionActivityStatus.TERMINAL}',
            outcome='{TerminalOutcome.ORPHANED_LEASE_EXPIRED}',
            result_json=?,
            error='lease expired before completion',
            completed_at=?,
            heartbeat_at=?
        WHERE status IN ({_LEASE_LIVE})
          AND lease_expires_at < ?
        """,
        (expired_payload, t, t, t),
    ).rowcount
    # An outbox row has no heartbeat at all, so age is the only evidence there
    # is. PENDING ages from creation, CLAIMED from the claim that stalled.
    swept["expired_ledger_events"] = c.execute(
        f"""
        UPDATE ledger_events
        SET status='{LedgerEventStatus.ABANDONED}',
            error='no consumer claimed this event before it expired',
            processed_at=?
        WHERE status='{LedgerEventStatus.PENDING}'
          AND created_at < ?
        """,
        (t, cutoff),
    ).rowcount
    swept["stalled_ledger_events"] = c.execute(
        f"""
        UPDATE ledger_events
        SET status='{LedgerEventStatus.ABANDONED}',
            error='consumer claimed this event and never resolved it',
            processed_at=?
        WHERE status='{LedgerEventStatus.CLAIMED}'
          AND COALESCE(claimed_at, created_at) < ?
        """,
        (t, cutoff),
    ).rowcount
    # An intent's worker holds a lease, which is stronger evidence than the
    # intent's own age: an old claim under a live lease is real work. The lease
    # sweep above already ran, so an expired lease reads as terminal here.
    #
    # The status is CANCELED rather than a new word, because quorum settlement
    # and the retention guards below enumerate terminal intent statuses in
    # several places; a fifth one would hang every quorum holding an abandoned
    # child. The outcome column carries why, exactly as it does for leases.
    swept["abandoned_dispatch_intents"] = c.execute(
        f"""
        UPDATE dispatch_intents
        SET status='{DispatchIntentStatus.CANCELED}',
            outcome='{TerminalOutcome.ORPHANED_CLAIM_EXPIRED}'
        WHERE status='{DispatchIntentStatus.CLAIMED}'
          AND COALESCE(claimed_at, created_at) < ?
          AND NOT EXISTS (
            SELECT 1 FROM agent_execution_leases l
            WHERE l.intent_id = dispatch_intents.intent_id
              AND l.status IN ({_LEASE_LIVE})
          )
        """,
        (cutoff,),
    ).rowcount
    # A task's holder is a session, and a session heartbeats. A task claimed by
    # a session still checking in is live however old the claim is.
    swept["abandoned_saga_tasks"] = c.execute(
        f"""
        UPDATE saga_tasks
        SET status='{TaskStatus.ABANDONED}', updated_at=?
        WHERE status='{TaskStatus.CLAIMED}'
          AND updated_at < ?
          AND NOT EXISTS (
            SELECT 1 FROM sessions s
            WHERE s.session_id = saga_tasks.assigned_session_id
              AND s.last_heartbeat_at >= ?
          )
        """,
        (t, cutoff, cutoff),
    ).rowcount
    return swept


def gc_ledger(
    retention_seconds: int | None = None,
    abandoned_after_seconds: int = WORK_ABANDONED_AFTER_SECONDS,
) -> dict[str, Any]:
    """Prune dead ledger rows and return per-target deleted counts.

    Work abandoned by a dead holder is
    always terminalized, so retry and cleanup code has a terminal fact to
    inspect; see _sweep_abandoned_work for which evidence each table uses.
    With retention_seconds, records older than the window are also removed:
    notes, handoffs, terminal execution leases, terminal dispatch intents with
    no remaining lease, terminal ledger events, and old lines from events.jsonl.
    Work whose holder is still proving it is alive is never touched, however old
    the row is, so in-flight work is safe.
    """
    t = now()
    deleted: dict[str, int] = {}
    with tx() as c:
        deleted.update(_sweep_abandoned_work(c, t, abandoned_after_seconds))
        if retention_seconds is not None:
            cutoff = t - retention_seconds
            deleted["notes"] = c.execute(
                "DELETE FROM notes WHERE created_at < ?", (cutoff,)
            ).rowcount
            deleted["handoffs"] = c.execute(
                "DELETE FROM handoffs WHERE created_at < ?", (cutoff,)
            ).rowcount
            deleted["agent_execution_events"] = c.execute(
                "DELETE FROM agent_execution_events WHERE created_at < ? "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM agent_execution_checkpoints cp "
                "  WHERE cp.lease_id = agent_execution_events.lease_id"
                ")",
                (cutoff,),
            ).rowcount
            deleted["agent_execution_artifacts"] = c.execute(
                "DELETE FROM agent_execution_artifacts WHERE created_at < ? "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM agent_execution_checkpoints cp "
                "  WHERE cp.lease_id = agent_execution_artifacts.lease_id"
                ")",
                (cutoff,),
            ).rowcount
            deleted["agent_execution_leases"] = c.execute(
                "DELETE FROM agent_execution_leases "
                "WHERE COALESCE(completed_at, created_at) < ? "
                f"AND status IN ({_LEASE_TERMINAL}) "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM agent_execution_checkpoints cp "
                "  WHERE cp.lease_id = agent_execution_leases.lease_id"
                ")",
                (cutoff,),
            ).rowcount
            deleted["dispatch_intents"] = c.execute(
                "DELETE FROM dispatch_intents WHERE created_at < ? "
                f"AND status IN ({_DISPATCH_COLLECTABLE}) "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM agent_execution_leases "
                "  WHERE agent_execution_leases.intent_id = dispatch_intents.intent_id"
                ") "
                # A terminal child/reducer of a still-live quorum is pending
                # evidence for the reducer, not garbage.
                "AND NOT EXISTS ("
                "  SELECT 1 FROM dispatch_intents p "
                "  WHERE p.intent_id = dispatch_intents.parent_intent_id "
                f"    AND p.status NOT IN ({_DISPATCH_SETTLED_SQL})"
                ") "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM agent_execution_checkpoints cp "
                "  WHERE cp.intent_id = dispatch_intents.intent_id "
                "     OR cp.review_intent_id = dispatch_intents.intent_id"
                ")",
                (cutoff,),
            ).rowcount
            deleted["ledger_events"] = c.execute(
                f"DELETE FROM ledger_events WHERE created_at < ? AND status IN ({_LEDGER_SETTLED})",
                (cutoff,),
            ).rowcount
    if retention_seconds is not None:
        deleted["events"] = _prune_events_file(t - retention_seconds)
    data = ok(deleted=deleted, ran_at=iso(t))
    emit("gc_ledger", data)
    return data


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
