# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Dispatch intent submission, claiming, reduction, and completion."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from ..constants import dispatch_settlement_topic
from ..contracts import DispatchIntentStatus, MilestoneStatus
from .contracts import DispatchTerminalStatus
from .milestones import (
    ClaimedMilestone,
    MalformedMilestoneReference,
    parse_milestone_reference,
    record_dispatch_outcome_on_milestone,
)
from .outcomes import TerminalOutcome, classify_failure
from .store import (
    connect,
    emit,
    err,
    iso,
    now,
    ok,
    rowdict,
    sql_status_list,
    tx,
)

_DISPATCH_TIERS = {"junior", "senior", "staff"}
_DISPATCH_KINDS = {"advisory", "code"}


# What lets a quorum parent settle on a child. SUPERSEDED is absent on purpose:
# a replaced child is not an answer, and the parent waits for the replacement.
# TERMINAL_DISPATCH_INTENT_STATUSES answers the other question - has this intent
# stopped moving - and a waiter wants that one instead.
_QUORUM_SETTLING_STATUSES = (
    DispatchIntentStatus.DONE,
    DispatchIntentStatus.FAILED,
    DispatchIntentStatus.CANCELED,
)
# A fan-out sibling in one of these is mid-review or replaced, so the parent
# must not be treated as finished on its behalf.
_DISPATCH_NOT_RESUMABLE = (
    DispatchIntentStatus.CHECKPOINT_REVIEW,
    DispatchIntentStatus.PAUSED,
    DispatchIntentStatus.SUPERSEDED,
)


_DISPATCH_UNSETTLED = sql_status_list(DispatchIntentStatus.PENDING, DispatchIntentStatus.CLAIMED)
_QUORUM_SETTLING_SQL = sql_status_list(*_QUORUM_SETTLING_STATUSES)


def dispatch_intent_to_dict(r: dict[str, Any]) -> dict[str, Any]:
    d = rowdict(r)
    d["created_at"] = iso(d["created_at"])
    for key in ("claimed_at", "completed_at"):
        if d.get(key):
            d[key] = iso(d[key])
    return d


_DISPATCH_REDUCES = {"none", "vote", "judge"}


def _dispatch_intent_row(intent_id: str) -> dict[str, Any] | None:
    with tx() as c:
        row = c.execute(
            "SELECT * FROM dispatch_intents WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
    return rowdict(row) if row is not None else None


def dispatch_intent_statuses(intent_ids: Sequence[str]) -> dict[str, DispatchIntentStatus]:
    """Live status for each named intent, keyed by id; unknown ids are omitted.

    The cockpit's milestone view is the caller: a milestone that says RUNNING
    while its intent sits PENDING is parked, not working, and an operator
    watching the pill cannot tell those two apart without this. One query for
    the whole view, because the view is rebuilt on every poll.
    """

    if not intent_ids:
        return {}
    placeholders = ",".join("?" for _ in intent_ids)
    with tx() as c:
        rows = c.execute(
            f"SELECT intent_id, status FROM dispatch_intents WHERE intent_id IN ({placeholders})",
            tuple(intent_ids),
        ).fetchall()
    return {
        entry["intent_id"]: DispatchIntentStatus(entry["status"])
        for entry in (rowdict(row) for row in rows)
    }


def notify_dispatch_status_change(intent_id: str) -> bool:
    """Wake the milestone parked on this intent, if one is.

    Named for what it does rather than for one of the things that trigger it. It
    used to be `notify_dispatch_settlement` and be called from exactly one place,
    `complete_dispatch_intent`, which accepts only DONE and FAILED. Every other
    status write - paused, in checkpoint review, cancelled, superseded - was
    silent, so a milestone parked on one of them slept out its whole bound before
    anybody re-read the row. The name said settlement and the requirement was
    "stopped moving"; the two differ by exactly the cases that hurt.

    Returns whether a notification was sent, which is what a test can assert on;
    False is the ordinary answer for the many producers that have nobody waiting.

    Best effort by construction, and that is a deliberate bound rather than an
    apology. The waiter's `DBOS.recv` carries its own durable timeout, so a
    notification that never arrives costs latency and not correctness: the wait
    expires, the waiter re-reads the row, and finds exactly what this message
    would have told it. Raising here instead would turn a delivery problem into a
    failure of the write that already committed.
    """

    row = _dispatch_intent_row(intent_id)
    if row is None:
        return False
    target = row.get("notify_workflow_id")
    if not target:
        return False

    from .._dbos_runtime import DBOS
    from ..dbos_app import is_dbos_active

    if DBOS is None or not is_dbos_active():
        return False
    try:
        DBOS.send(
            str(target),
            {"intent_id": intent_id, "status": str(row["status"])},
            topic=dispatch_settlement_topic(intent_id),
        )
    except Exception as exc:  # pragma: no cover - delivery is not authoritative
        emit(
            "notify_dispatch_status_change_failed",
            {"intent_id": intent_id, "error": f"{type(exc).__name__}: {exc}"},
        )
        return False
    return True


@contextmanager
def dispatch_status_notifications() -> Iterator[list[str]]:
    """Collect intents to wake, and wake them once the transaction has committed.

    Sending from inside the transaction races the waiter against the write that
    woke it: a milestone re-reads the intent row the moment it wakes, and would
    see the status the row had before the commit. `complete_dispatch_intent`
    already notified after its own `with tx()` block for that reason.

    This exists because the other writers cannot simply move the call down: they
    `return` from inside their transaction, so there is no "after the block" to
    put it in. Appending to this list is that place. On an exception the
    generator is resumed by `throw` rather than by `next`, so nothing is sent for
    a transaction that rolled back - which is the property that makes collecting
    safe rather than merely convenient.
    """

    pending: list[str] = []
    yield pending
    for intent_id in pending:
        notify_dispatch_status_change(intent_id)


def _insert_dispatch_intent_row(
    c: Any,
    *,
    intent_id: str,
    tier: str,
    kind: str,
    prompt: str,
    target_project_id: str | None,
    source: str | None,
    created_at: float,
    fanout: int = 1,
    allow_tiers: str = "[]",
    reduce: str = "none",
    reducer_tier: str | None = None,
    parent_intent_id: str | None = None,
    intent_role: str = "single",
    idempotency_key: str | None = None,
    notify_workflow_id: str | None = None,
    permitted_capabilities: str = "[]",
    base_commit_sha: str | None = None,
) -> str:
    """Insert one intent, or return the incumbent that already claimed its key.

    Returns the id of the intent that owns ``idempotency_key`` afterwards, which
    is ``intent_id`` on a fresh insert and the earlier intent's id on a
    duplicate. The check and the insert are one statement on purpose: a caller
    that read first and inserted second would still race a concurrent dispatcher
    through the gap, which is the exact failure this exists to prevent.
    """

    c.execute(
        f"""
        INSERT INTO dispatch_intents(
            intent_id, tier, kind, prompt, target_project_id, source,
            status, created_at, fanout, allow_tiers, reduce, reducer_tier,
            parent_intent_id, intent_role, idempotency_key, notify_workflow_id,
            permitted_capabilities, base_commit_sha
        ) VALUES (
            ?, ?, ?, ?, ?, ?, '{DispatchIntentStatus.PENDING}',
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT (idempotency_key) DO NOTHING
        """,
        (
            intent_id,
            tier,
            kind,
            prompt,
            target_project_id,
            source,
            created_at,
            fanout,
            allow_tiers,
            reduce,
            reducer_tier,
            parent_intent_id,
            intent_role,
            idempotency_key,
            notify_workflow_id,
            permitted_capabilities,
            base_commit_sha,
        ),
    )
    if idempotency_key is None:
        return intent_id
    row = c.execute(
        "SELECT intent_id FROM dispatch_intents WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    # The row is guaranteed present: this statement either inserted it or lost
    # the conflict to a row that is already there, and both are in this
    # transaction's snapshot. A miss means the unique index is absent, which is
    # a deployment fault and not something to paper over with a fresh intent.
    if row is None:
        raise RuntimeError(
            f"dispatch intent idempotency key {idempotency_key!r} resolved to no row; "
            "uq_dispatch_intents_idempotency_key is missing (run migrations/007)"
        )
    return str(rowdict(row)["intent_id"])


def submit_dispatch_intent(
    tier: str,
    prompt: str,
    kind: str = "advisory",
    target_project_id: str | None = None,
    source: str | None = None,
    fanout: int = 1,
    allow_tiers: list[str] | None = None,
    reduce: str = "none",
    reducer_tier: str | None = None,
    idempotency_key: str | None = None,
    notify_workflow_id: str | None = None,
    *,
    permitted_capabilities: Sequence[str] = (),
    base_commit_sha: str | None = None,
) -> dict[str, Any]:
    """Enqueue a unit of work for the reactor to dispatch to an agent tier.

    Producers (ASR triggers, gemma keyword scans, sagas, agents) call this; the
    LedgerDispatcher claims and runs PENDING intents.

    ``idempotency_key`` names the work rather than the call. Supplying it makes
    a resubmission return the existing intent and report ``deduplicated: True``
    instead of creating a second one. It is the caller's to derive, because only
    the caller knows what makes two requests the same request; a milestone
    executor derives it from the WorkUnit, milestone, and attempt exactly as
    `_milestone_workflow_id` derives a workflow id from the same three facts.
    Omitting it keeps the previous behaviour, where every call is a new intent.

    ``permitted_capabilities`` is the ceiling on what a process spawned for this
    intent may do. Keyword-only, because the existing positional signature is
    unpacked positionally by callers and a new positional would silently shift
    them. Empty is the narrowest authority, not the widest: an intent that
    declares nothing gets a read-only spawn.

    Ensemble/quorum form: `fanout=N>1` with `reduce=vote|judge` expands the
    intent at submit time into N child intents (tiers assigned round-robin over
    `allow_tiers` for decorrelation) plus one reducer intent that only becomes
    claimable once every child is terminal. The reducer's completion completes
    the quorum parent with the reduced answer. `fanout=1` with a non-empty
    `allow_tiers` is overflow/load-balancing: any listed tier's reactor may
    claim the single intent.
    """
    allow_tiers = allow_tiers or []
    if tier not in _DISPATCH_TIERS:
        return err("invalid_tier", tier=tier, valid=sorted(_DISPATCH_TIERS))
    if kind not in _DISPATCH_KINDS:
        return err("invalid_kind", kind=kind, valid=sorted(_DISPATCH_KINDS))
    if reduce not in _DISPATCH_REDUCES:
        return err("invalid_reduce", reduce=reduce, valid=sorted(_DISPATCH_REDUCES))
    invalid_allow = [item for item in allow_tiers if item not in _DISPATCH_TIERS]
    if invalid_allow:
        return err("invalid_allow_tiers", allow_tiers=invalid_allow, valid=sorted(_DISPATCH_TIERS))
    if reducer_tier is not None and reducer_tier not in _DISPATCH_TIERS:
        return err("invalid_reducer_tier", reducer_tier=reducer_tier, valid=sorted(_DISPATCH_TIERS))
    if fanout < 1:
        return err("invalid_fanout", fanout=fanout, message="fanout must be >= 1")
    if fanout == 1:
        if reduce != "none":
            return err(
                "invalid_quorum",
                message="a single answer cannot be reduced; fanout=1 requires reduce=none",
            )
        if reducer_tier is not None:
            return err("invalid_quorum", message="reducer_tier requires fanout > 1")
    else:
        if reduce == "none":
            return err(
                "invalid_quorum",
                message="fanout > 1 requires reduce=vote or reduce=judge",
            )
        if not allow_tiers:
            return err(
                "invalid_quorum",
                message="fanout > 1 requires a non-empty allow_tiers list",
            )
        if kind != "advisory":
            return err(
                "invalid_quorum",
                message="quorum dispatch is advisory-only; code fan-out has no merge semantics",
            )
        if reduce == "judge" and reducer_tier is None:
            reducer_tier = "staff"

    if idempotency_key is not None and fanout != 1:
        # A quorum submit writes N+1 rows and only the parent could carry the
        # key, so a resubmission would return the parent while silently skipping
        # the children it needs. Refusing is honest; the alternative is a
        # deduplicated ensemble with no members.
        return err(
            "invalid_idempotency_key",
            message="idempotency_key requires fanout=1; a quorum submit writes several intents",
        )

    intent_id = str(uuid.uuid4())
    allow_tiers_json = json.dumps(allow_tiers, sort_keys=True)
    capabilities_json = json.dumps(sorted(set(permitted_capabilities)), sort_keys=True)
    t = now()
    child_intent_ids: list[str] = []
    reducer_intent_id: str | None = None
    deduplicated = False
    with tx() as c:
        if fanout == 1:
            settled_intent_id = _insert_dispatch_intent_row(
                c,
                intent_id=intent_id,
                tier=tier,
                kind=kind,
                prompt=prompt,
                target_project_id=target_project_id,
                source=source,
                created_at=t,
                allow_tiers=allow_tiers_json,
                idempotency_key=idempotency_key,
                notify_workflow_id=notify_workflow_id,
                permitted_capabilities=capabilities_json,
                base_commit_sha=base_commit_sha,
            )
            deduplicated = settled_intent_id != intent_id
            intent_id = settled_intent_id
        else:
            # The quorum parent is never claimed or executed; it is the durable
            # umbrella the reducer completes with the reduced answer.
            _insert_dispatch_intent_row(
                c,
                intent_id=intent_id,
                tier=tier,
                kind=kind,
                prompt=prompt,
                target_project_id=target_project_id,
                source=source,
                created_at=t,
                fanout=fanout,
                allow_tiers=allow_tiers_json,
                reduce=reduce,
                reducer_tier=reducer_tier,
                intent_role="quorum",
                permitted_capabilities=capabilities_json,
            )
            for index in range(fanout):
                child_id = str(uuid.uuid4())
                child_intent_ids.append(child_id)
                _insert_dispatch_intent_row(
                    c,
                    intent_id=child_id,
                    tier=allow_tiers[index % len(allow_tiers)],
                    kind=kind,
                    prompt=prompt,
                    target_project_id=target_project_id,
                    source=f"quorum_child:{intent_id}",
                    created_at=t,
                    parent_intent_id=intent_id,
                    intent_role="child",
                    permitted_capabilities=capabilities_json,
                )
            reducer_intent_id = str(uuid.uuid4())
            _insert_dispatch_intent_row(
                c,
                intent_id=reducer_intent_id,
                tier=reducer_tier or tier,
                kind="advisory",
                prompt=prompt,
                target_project_id=target_project_id,
                source=f"quorum_reducer:{intent_id}",
                created_at=t,
                reduce=reduce,
                reducer_tier=reducer_tier,
                parent_intent_id=intent_id,
                intent_role="reducer",
                permitted_capabilities=capabilities_json,
            )
    # A deduplicated submit did not create anything, so the incumbent's own
    # status and creation time are the answer. Reporting PENDING and `now` for a
    # request that resolved to an intent already RUNNING for fifty minutes would
    # be the caller's evidence that it queued fresh work, which it did not.
    status = "PENDING"
    created_at = t
    if deduplicated:
        incumbent = _dispatch_intent_row(intent_id)
        if incumbent is not None:
            status = str(incumbent["status"])
            created_at = float(incumbent["created_at"])

    data = ok(
        intent_id=intent_id,
        deduplicated=deduplicated,
        tier=tier,
        kind=kind,
        target_project_id=target_project_id,
        source=source,
        status=status,
        created_at=iso(created_at),
        fanout=fanout,
        allow_tiers=allow_tiers,
        reduce=reduce,
        reducer_tier=reducer_tier,
        intent_role="quorum" if fanout > 1 else "single",
        child_intent_ids=child_intent_ids,
        reducer_intent_id=reducer_intent_id,
    )
    emit("submit_dispatch_intent", data)
    return data


def claim_next_dispatch_intent(
    claimed_by: str,
    tier: str | None = None,
) -> dict[str, Any]:
    """Atomically claim the oldest PENDING intent (optionally of one tier).

    The UPDATE ... WHERE status='PENDING' guard makes the PENDING->CLAIMED
    transition a token only one dispatcher can take, so concurrent reactors
    never double-run an intent. Returns {ok, intent: ... | None}.
    """
    t = now()
    # A suppressed duplicate is written FAILED from inside this transaction and
    # returned from inside it too, so the wake has to be collected here.
    with dispatch_status_notifications() as notify_after_commit, tx() as c:
        # Quorum parents are durable umbrellas, never executed directly. A
        # reducer only becomes claimable once every sibling child is terminal;
        # the dependency is derived from parent linkage, so it cannot drift.
        clauses = [
            f"status = '{DispatchIntentStatus.PENDING}'",
            "intent_role != 'quorum'",
            (
                "(intent_role != 'reducer' OR NOT EXISTS ("
                "  SELECT 1 FROM dispatch_intents s"
                "  WHERE s.parent_intent_id = dispatch_intents.parent_intent_id"
                "    AND s.intent_role = 'child'"
                f"    AND s.status IN ({_DISPATCH_UNSETTLED})"
                "))"
            ),
        ]
        params: list[Any] = []
        if tier is not None:
            # Overflow: a single intent whose allow_tiers lists this tier may be
            # claimed by it even when its home tier differs. Tier names are a
            # closed vocabulary, so the JSON LIKE match cannot false-positive.
            clauses.append("(tier = ? OR (intent_role = 'single' AND allow_tiers LIKE ?))")
            params.extend([tier, f'%"{tier}"%'])
        where = " AND ".join(clauses)
        row = c.execute(
            f"SELECT * FROM dispatch_intents WHERE {where} ORDER BY created_at"
            " LIMIT 1 FOR UPDATE SKIP LOCKED",
            params,
        ).fetchone()
        if not row:
            return ok(intent=None)
        reference = parse_milestone_reference(row["source"])
        if isinstance(reference, MalformedMilestoneReference):
            emit(
                "dispatch_source_malformed",
                {"intent_id": row["intent_id"], "source": reference.source},
            )
        milestone = None
        if isinstance(reference, ClaimedMilestone):
            milestone = c.execute(
                "SELECT * FROM saga_milestones WHERE milestone_id=?",
                (reference.milestone_id,),
            ).fetchone()
            if milestone is None:
                # A claim that resolves to nothing used to read exactly like a
                # healthy one: every guard below is `bool(milestone and ...)`, so
                # a dangling reference passed all of them and the IN_PROGRESS
                # write later matched zero rows in silence. The claim still
                # proceeds, because retention can legitimately remove a milestone
                # out from under a live intent, but it no longer does so quietly.
                emit(
                    "dispatch_milestone_reference_unresolved",
                    {
                        "intent_id": row["intent_id"],
                        "milestone_id": reference.milestone_id,
                        "source": row["source"],
                    },
                )
        if isinstance(reference, ClaimedMilestone) and milestone is not None:
            claimed_elsewhere = bool(
                milestone["dispatch_intent_id"]
                and milestone["dispatch_intent_id"] != row["intent_id"]
            )
            no_longer_runnable = (
                MilestoneStatus(str(milestone["status"])) is not MilestoneStatus.PENDING
            )
            if claimed_elsewhere or no_longer_runnable:
                reason = (
                    "duplicate milestone intent suppressed before execution: "
                    f"milestone_status={milestone['status']}; "
                    f"active_intent_id={milestone['dispatch_intent_id']}"
                )
                c.execute(
                    f"""
                    UPDATE dispatch_intents
                    SET status='{DispatchIntentStatus.FAILED}', outcome=?, result=?,
                        error=?, completed_at=?
                    WHERE intent_id=? AND status='{DispatchIntentStatus.PENDING}'
                    """,
                    (
                        TerminalOutcome.DUPLICATE_SUPPRESSED.value,
                        reason,
                        reason,
                        t,
                        row["intent_id"],
                    ),
                )
                suppressed = c.execute(
                    "SELECT * FROM dispatch_intents WHERE intent_id=?",
                    (row["intent_id"],),
                ).fetchone()
                suppressed_intent_id = str(row["intent_id"])
                notify_after_commit.append(suppressed_intent_id)
                return ok(
                    intent=None,
                    duplicate_suppressed=dispatch_intent_to_dict(suppressed),
                )
        cur = c.execute(
            f"UPDATE dispatch_intents SET status='{DispatchIntentStatus.CLAIMED}', "
            "claimed_by=?, claimed_at=? "
            f"WHERE intent_id=? AND status='{DispatchIntentStatus.PENDING}'",
            (claimed_by, t, row["intent_id"]),
        )
        if cur.rowcount != 1:
            # another dispatcher claimed it between our SELECT and UPDATE
            return ok(intent=None)
        claimed = c.execute(
            "SELECT * FROM dispatch_intents WHERE intent_id=?", (row["intent_id"],)
        ).fetchone()
        claimed_reference = parse_milestone_reference(claimed["source"])
        if isinstance(claimed_reference, ClaimedMilestone):
            c.execute(
                f"""
                UPDATE saga_milestones
                SET status='{MilestoneStatus.IN_PROGRESS}', dispatch_intent_id=?,
                    started_at=COALESCE(started_at, ?),
                    updated_at=?
                WHERE milestone_id=? AND status='{MilestoneStatus.PENDING}'
                """,
                (claimed["intent_id"], t, t, claimed_reference.milestone_id),
            )
    data = ok(intent=dispatch_intent_to_dict(claimed))
    emit("claim_dispatch_intent", {"intent_id": row["intent_id"], "claimed_by": claimed_by})
    return data


def complete_dispatch_intent(
    intent_id: str,
    status: str,
    result: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Resolve a claimed intent as DONE or FAILED with its captured output."""
    if status not in {member.value for member in DispatchTerminalStatus}:
        return err("invalid_status", status=status, allowed=["DONE", "FAILED"])
    outcome = (
        TerminalOutcome.AUTOMATED_COMPLETION
        if status == DispatchTerminalStatus.DONE
        else classify_failure(error or result)
    )
    t = now()
    with tx() as c:
        r = c.execute("SELECT * FROM dispatch_intents WHERE intent_id=?", (intent_id,)).fetchone()
        if not r:
            return err("not_found", intent_id=intent_id)
        if DispatchIntentStatus(str(r["status"])) in _DISPATCH_NOT_RESUMABLE:
            data = ok(
                intent_id=intent_id,
                status=r["status"],
                completion_skipped=True,
                message="checkpoint recovery state owns this intent",
                milestone_update=None,
                completed_parent_intent_id=None,
            )
            return data
        c.execute(
            "UPDATE dispatch_intents SET status=?, outcome=?, result=?, error=?, completed_at=? "
            "WHERE intent_id=?",
            (status, outcome.value, result, error, t, intent_id),
        )
        updated_intent = c.execute(
            "SELECT * FROM dispatch_intents WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
        milestone_update = record_dispatch_outcome_on_milestone(
            c,
            intent=updated_intent,
            dispatch_status=status,
            result=result,
            error=error,
            completed_at=t,
        )
        if (
            milestone_update is None
            and updated_intent["checkpoint_id"]
            and ":continuation:" in str(updated_intent["source"] or "")
        ):
            siblings = c.execute(
                "SELECT status FROM dispatch_intents WHERE checkpoint_id=? AND source LIKE ?",
                (
                    updated_intent["checkpoint_id"],
                    f"execution_checkpoint:{updated_intent['checkpoint_id']}:continuation:%",
                ),
            ).fetchall()
            if siblings and all(
                DispatchIntentStatus(str(sibling["status"])) in _QUORUM_SETTLING_STATUSES
                for sibling in siblings
            ):
                checkpoint = c.execute(
                    "SELECT intent_id FROM agent_execution_checkpoints WHERE checkpoint_id=?",
                    (updated_intent["checkpoint_id"],),
                ).fetchone()
                original = (
                    c.execute(
                        "SELECT * FROM dispatch_intents WHERE intent_id=?",
                        (checkpoint["intent_id"],),
                    ).fetchone()
                    if checkpoint and checkpoint["intent_id"]
                    else None
                )
                if original:
                    aggregate_status = (
                        "DONE"
                        if all(
                            DispatchIntentStatus(str(sibling["status"]))
                            is DispatchIntentStatus.DONE
                            for sibling in siblings
                        )
                        else "FAILED"
                    )
                    milestone_update = record_dispatch_outcome_on_milestone(
                        c,
                        intent=original,
                        dispatch_status=aggregate_status,
                        result=result,
                        error=error,
                        completed_at=t,
                    )
        # A reducer's terminal outcome IS the quorum's outcome: completing the
        # reducer completes its parent with the reduced answer (unless an
        # operator already canceled the parent).
        completed_parent_intent_id: str | None = None
        if updated_intent["intent_role"] == "reducer" and updated_intent["parent_intent_id"]:
            cur = c.execute(
                "UPDATE dispatch_intents SET status=?, outcome=?, result=?, error=?, "
                "completed_at=? "
                f"WHERE intent_id=? AND status NOT IN ({_QUORUM_SETTLING_SQL})",
                (
                    status,
                    outcome.value,
                    result,
                    error,
                    t,
                    updated_intent["parent_intent_id"],
                ),
            )
            if cur.rowcount == 1:
                completed_parent_intent_id = updated_intent["parent_intent_id"]
                parent_row = c.execute(
                    "SELECT * FROM dispatch_intents WHERE intent_id=?",
                    (completed_parent_intent_id,),
                ).fetchone()
                record_dispatch_outcome_on_milestone(
                    c,
                    intent=parent_row,
                    dispatch_status=status,
                    result=result,
                    error=error,
                    completed_at=t,
                )
    # After the commit, never inside it. A milestone woken by this notification
    # immediately re-reads the intent row, so a send from inside the transaction
    # would race the waiter against its own settle and could hand it the status
    # it had before completing.
    notify_dispatch_status_change(intent_id)

    data = ok(
        intent_id=intent_id,
        status=status,
        completed_at=iso(t),
        milestone_update=milestone_update,
        completed_parent_intent_id=completed_parent_intent_id,
    )
    emit("complete_dispatch_intent", data)
    return data


def cancel_dispatch_intent(
    intent_id: str,
    reason: str | None = None,
    canceled_by: str = "operator",
) -> dict[str, Any]:
    """Cancel a PENDING dispatch intent before any dispatcher can claim it."""
    t = now()
    reason = reason or "canceled"
    with tx() as c:
        r = c.execute("SELECT * FROM dispatch_intents WHERE intent_id=?", (intent_id,)).fetchone()
        if not r:
            return err("not_found", intent_id=intent_id)
        if DispatchIntentStatus(str(r["status"])) is not DispatchIntentStatus.PENDING:
            return err(
                "not_pending",
                intent_id=intent_id,
                current_status=r["status"],
                message="Only PENDING dispatch intents can be canceled.",
            )
        result = json.dumps(
            {
                "schema_version": "dispatch_intent_cancellation.v1",
                "canceled_by": canceled_by,
                "reason": reason,
            },
            sort_keys=True,
        )
        c.execute(
            f"UPDATE dispatch_intents SET status='{DispatchIntentStatus.CANCELED}', "
            "outcome=?, result=?, "
            "error=?, completed_at=? "
            f"WHERE intent_id=? AND status='{DispatchIntentStatus.PENDING}'",
            (TerminalOutcome.OPERATOR_CANCELED.value, result, reason, t, intent_id),
        )
        canceled_children = 0
        if r["intent_role"] == "quorum":
            # Canceling a quorum umbrella also cancels its not-yet-claimed
            # children and reducer; already-claimed children run to completion
            # but the terminal parent blocks the reducer cascade.
            canceled_children = c.execute(
                f"UPDATE dispatch_intents SET status='{DispatchIntentStatus.CANCELED}', "
                "outcome=?, result=?, error=?, completed_at=? "
                f"WHERE parent_intent_id=? AND status='{DispatchIntentStatus.PENDING}'",
                (
                    TerminalOutcome.OPERATOR_CANCELED.value,
                    result,
                    reason,
                    t,
                    intent_id,
                ),
            ).rowcount
    # A cancelled intent is one a milestone may be parked on, and cancellation is
    # exactly the case where waiting out a full bound is worst: nothing is coming.
    notify_dispatch_status_change(intent_id)
    data = ok(
        intent_id=intent_id,
        status="CANCELED",
        reason=reason,
        canceled_by=canceled_by,
        completed_at=iso(t),
        canceled_children=canceled_children,
    )
    emit("cancel_dispatch_intent", data)
    return data


def supersede_dispatch_intent(
    old_intent_id: str,
    prompt: str | None = None,
    tier: str | None = None,
    kind: str | None = None,
    target_project_id: str | None = None,
    source: str | None = None,
    reason: str | None = None,
    superseded_by: str = "operator",
) -> dict[str, Any]:
    """Atomically cancel a PENDING intent and enqueue its replacement."""
    t = now()
    reason = reason or "superseded"
    new_intent_id = str(uuid.uuid4())
    with tx() as c:
        old = c.execute(
            "SELECT * FROM dispatch_intents WHERE intent_id=?", (old_intent_id,)
        ).fetchone()
        if not old:
            return err("not_found", intent_id=old_intent_id)
        if DispatchIntentStatus(str(old["status"])) is not DispatchIntentStatus.PENDING:
            return err(
                "not_pending",
                intent_id=old_intent_id,
                current_status=old["status"],
                message="Only PENDING dispatch intents can be superseded.",
            )
        new_tier = tier or old["tier"]
        new_kind = kind or old["kind"]
        new_prompt = prompt if prompt is not None else old["prompt"]
        new_source = source if source is not None else old["source"]
        new_target_project_id = (
            target_project_id if target_project_id is not None else old["target_project_id"]
        )
        if new_tier not in _DISPATCH_TIERS:
            return err("invalid_tier", tier=new_tier, valid=sorted(_DISPATCH_TIERS))
        if new_kind not in _DISPATCH_KINDS:
            return err("invalid_kind", kind=new_kind, valid=sorted(_DISPATCH_KINDS))
        c.execute(
            f"""
            INSERT INTO dispatch_intents(
                intent_id, tier, kind, prompt, target_project_id, source,
                status, created_at, base_commit_sha
            ) VALUES (?, ?, ?, ?, ?, ?, '{DispatchIntentStatus.PENDING}', ?, ?)
            """,
            (
                new_intent_id,
                new_tier,
                new_kind,
                new_prompt,
                new_target_project_id,
                new_source,
                t,
                old["base_commit_sha"],
            ),
        )
        cancel_result = json.dumps(
            {
                "schema_version": "dispatch_intent_supersession.v1",
                "superseded_by": superseded_by,
                "reason": reason,
                "new_intent_id": new_intent_id,
            },
            sort_keys=True,
        )
        c.execute(
            f"UPDATE dispatch_intents SET status='{DispatchIntentStatus.CANCELED}', "
            "outcome=?, result=?, "
            "error=?, completed_at=? "
            f"WHERE intent_id=? AND status='{DispatchIntentStatus.PENDING}'",
            (
                TerminalOutcome.OPERATOR_CANCELED.value,
                cancel_result,
                reason,
                t,
                old_intent_id,
            ),
        )
    # The old intent has stopped moving; its waiter would otherwise sit out the
    # whole bound waiting for work that has been handed to a different row.
    notify_dispatch_status_change(old_intent_id)
    data = ok(
        old_intent_id=old_intent_id,
        new_intent_id=new_intent_id,
        status="PENDING",
        canceled_status="CANCELED",
        tier=new_tier,
        kind=new_kind,
        target_project_id=new_target_project_id,
        source=new_source,
        reason=reason,
        created_at=iso(t),
    )
    emit("supersede_dispatch_intent", data)
    return data


def list_dispatch_intents(
    status_filter: str | None = None,
    parent_intent_id: str | None = None,
) -> dict[str, Any]:
    """List dispatch intents, optionally filtered by status or quorum parent."""
    clauses: list[str] = []
    params: list[Any] = []
    if status_filter:
        clauses.append("status=?")
        params.append(status_filter)
    if parent_intent_id:
        clauses.append("parent_intent_id=?")
        params.append(parent_intent_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect() as c:
        rows = c.execute(
            f"SELECT * FROM dispatch_intents{where} ORDER BY created_at DESC",
            params,
        ).fetchall()
    return ok(intents=[dispatch_intent_to_dict(r) for r in rows])
