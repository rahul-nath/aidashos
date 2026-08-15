# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Human approval request state and decisions."""

from __future__ import annotations

import json
import uuid
from typing import Any

from local_first_agent_os.constants import APPROVAL_REQUEST_TYPES

from ..contracts import ApprovalRequestType, ApprovalStatus
from .integration_queue import (
    EnqueueAdmitted,
    EnqueueRefused,
    admit_code_merge_approval,
    enqueue_summary,
    record_queued_request,
)
from .outcomes import require_approval_status_transition
from .store import connect, decode_json_object, emit, err, iso, now, ok, rowdict, tx

_APPROVAL_NAMESPACE = uuid.UUID("9db8e93f-04ab-4df2-ad6a-1944812b72bb")


def submit_approval_request(
    saga_id: str,
    request_type: str,
    payload: dict[str, Any] | None = None,
    requested_by: str | None = None,
) -> dict[str, Any]:
    """Submit an approval gate request.

    request_type: PURCHASE | EXTERNAL_COMMS | CODE_MERGE | MODEL_ESCALATION | GENERAL

    Policy:
    - No purchase/payment without PURCHASE approval
    - No external comms without EXTERNAL_COMMS approval
    - No code merge without CODE_MERGE approval (implies prior review)
    - No model escalation without MODEL_ESCALATION + budget reason
    """
    valid_types = set(APPROVAL_REQUEST_TYPES)
    if request_type not in valid_types:
        return err("invalid_request_type", request_type=request_type, valid=sorted(valid_types))

    return _submit_approval_request(
        approval_id=str(uuid.uuid4()),
        saga_id=saga_id,
        request_type=request_type,
        payload=payload,
        requested_by=requested_by,
    )


def submit_idempotent_approval_request(
    saga_id: str,
    request_type: str,
    *,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    requested_by: str | None = None,
) -> dict[str, Any]:
    """Submit one stable approval, refusing a conflicting replay."""

    normalized_key = idempotency_key.strip()
    if not normalized_key:
        return err("idempotency_key_required")
    return _submit_approval_request(
        approval_id=str(uuid.uuid5(_APPROVAL_NAMESPACE, normalized_key)),
        saga_id=saga_id,
        request_type=request_type,
        payload=payload,
        requested_by=requested_by,
    )


def _submit_approval_request(
    *,
    approval_id: str,
    saga_id: str,
    request_type: str,
    payload: dict[str, Any] | None,
    requested_by: str | None,
) -> dict[str, Any]:
    valid_types = set(APPROVAL_REQUEST_TYPES)
    if request_type not in valid_types:
        return err("invalid_request_type", request_type=request_type, valid=sorted(valid_types))
    normalized_payload = payload or {}
    t = now()
    created = False
    with tx() as c:
        r = c.execute("SELECT saga_id FROM sagas WHERE saga_id = ?", (saga_id,)).fetchone()
        if not r:
            return err("saga_not_found", saga_id=saga_id)
        existing = c.execute(
            "SELECT * FROM approval_requests WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        if existing:
            same_request = (
                existing["saga_id"] == saga_id
                and existing["request_type"] == request_type
                and existing["requested_by"] == requested_by
                and decode_json_object(existing["payload_json"]) == normalized_payload
            )
            if not same_request:
                return err("approval_idempotency_conflict", approval_id=approval_id)
            t = existing["created_at"]
            status = str(existing["status"])
        else:
            c.execute(
                f"""
                INSERT INTO approval_requests(
                    approval_id, saga_id, request_type, payload_json,
                    status, requested_by, created_at
                ) VALUES (?, ?, ?, ?, '{ApprovalStatus.PENDING}', ?, ?)
                """,
                (
                    approval_id,
                    saga_id,
                    request_type,
                    json.dumps(normalized_payload),
                    requested_by,
                    t,
                ),
            )
            created = True
            status = ApprovalStatus.PENDING.value
    data = ok(
        approval_id=approval_id,
        saga_id=saga_id,
        request_type=request_type,
        status=status,
        requested_by=requested_by,
        created_at=iso(t),
        created=created,
    )
    emit("submit_approval_request", data)
    return data


def resolve_approval_request(
    approval_id: str,
    approved: bool,
    resolved_by: str,
) -> dict[str, Any]:
    """Approve or deny a pending approval request.

    Approving a `CODE_MERGE` also enqueues it for integration, in the same
    transaction. That coupling is the point rather than a convenience: an
    approval is the moment a person agreed to land one exact commit, and the
    refinery is the only thing that lands anything, so a resolution that flipped
    the status without producing a queue row would leave agreed work invisible to
    the only mechanism that could act on it. `workflow/engine.py` used to close
    this path with "no code was merged by this command", and it now says which
    request the commit is queued as.

    Every resolution surface reaches this one function - the `/approve-merge`
    directive, the CLI, and the MCP tool all route here - which is why the hook
    is here and not in any of them. Queuing from one door and not the others
    would be the same two-halves defect wearing a different hat.

    A refused enqueue refuses the resolution. Nothing is written, the approval
    stays PENDING, and the operator is told which precondition failed, because
    the alternative is an approval that exists and a merge that can never happen.
    """

    t = now()
    new_status = "APPROVED" if approved else "DENIED"
    admission = _integration_admission(approval_id, approved=approved, enqueued_at=t)
    if isinstance(admission, EnqueueRefused):
        return err(
            "integration_enqueue_refused",
            approval_id=approval_id,
            refusal=admission.refusal.value,
            message=admission.message,
        )
    with tx() as c:
        r = c.execute(
            "SELECT * FROM approval_requests WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        if not r:
            return err("not_found", approval_id=approval_id)
        current = ApprovalStatus(str(r["status"]))
        if current is not ApprovalStatus.PENDING:
            return err("already_resolved", approval_id=approval_id, status=r["status"])
        require_approval_status_transition(current, ApprovalStatus(new_status))
        c.execute(
            """
            UPDATE approval_requests
            SET status = ?, resolved_by = ?, resolved_at = ?
            WHERE approval_id = ?
            """,
            (new_status, resolved_by, t, approval_id),
        )
        queued = (
            enqueue_summary(record_queued_request(c, admission.request, recorded_at=t))
            if isinstance(admission, EnqueueAdmitted)
            else {}
        )
    data = ok(
        approval_id=approval_id,
        status=new_status,
        resolved_by=resolved_by,
        resolved_at=iso(t),
        **queued,
    )
    emit("resolve_approval_request", data)
    return data


def _integration_admission(
    approval_id: str,
    *,
    approved: bool,
    enqueued_at: float,
) -> EnqueueAdmitted | EnqueueRefused | None:
    """Decide the queue question before the transaction opens, or answer `None`.

    `None` is "this resolution has nothing to do with the queue": a denial, or an
    approval of a kind that is not a request to land a commit. It is a third
    answer rather than a refusal because a PURCHASE approval failing an
    integration precondition would be nonsense.

    The read outside the transaction is repeated inside it. This one only decides
    whether git and the project registry agree that the payload is landable, and
    the one inside is what serializes against a concurrent resolution.
    """

    if not approved:
        return None
    with connect() as c:
        row = c.execute(
            "SELECT request_type, status, payload_json FROM approval_requests "
            "WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
    if row is None:
        return None
    if str(row["request_type"]) != ApprovalRequestType.CODE_MERGE.value:
        return None
    if ApprovalStatus(str(row["status"])) is not ApprovalStatus.PENDING:
        # Already resolved, or revoked. The transaction below is what says so,
        # and asking git about a commit nobody is resolving would be work done
        # for an answer that is thrown away.
        return None
    return admit_code_merge_approval(
        approval_id,
        decode_json_object(row["payload_json"]),
        enqueued_at=enqueued_at,
    )


def revoke_approval_request(
    approval_id: str,
    revoked_by: str,
    reason: str,
) -> dict[str, Any]:
    """Revoke a previously approved gate without erasing its resolution."""

    normalized_reason = reason.strip()
    if not normalized_reason:
        return err("reason_required", approval_id=approval_id)
    t = now()
    with tx() as c:
        row = c.execute(
            "SELECT * FROM approval_requests WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        if not row:
            return err("not_found", approval_id=approval_id)
        current = ApprovalStatus(str(row["status"]))
        if current is ApprovalStatus.REVOKED:
            return ok(
                approval_id=approval_id,
                status=ApprovalStatus.REVOKED.value,
                already_revoked=True,
            )
        if current is not ApprovalStatus.APPROVED:
            return err(
                "not_approved",
                approval_id=approval_id,
                status=current.value,
            )
        require_approval_status_transition(current, ApprovalStatus.REVOKED)
        c.execute(
            "UPDATE approval_requests SET status = ? WHERE approval_id = ?",
            (ApprovalStatus.REVOKED.value, approval_id),
        )
    data = ok(
        approval_id=approval_id,
        status=ApprovalStatus.REVOKED.value,
        previous_status=ApprovalStatus.APPROVED.value,
        original_resolved_by=row["resolved_by"],
        original_resolved_at=iso(row["resolved_at"]),
        revoked_by=revoked_by,
        revoked_at=iso(t),
        reason=normalized_reason,
    )
    emit("revoke_approval_request", data)
    return data


def list_approval_requests(
    saga_id: str | None = None,
    status_filter: str | None = None,
) -> dict[str, Any]:
    """List approval requests, optionally filtered."""
    if status_filter:
        try:
            status_filter = ApprovalStatus(status_filter).value
        except ValueError:
            return err(
                "invalid_status",
                status=status_filter,
                valid=[status.value for status in ApprovalStatus],
            )
    with connect() as c:
        clauses, params = [], []
        if saga_id:
            clauses.append("saga_id = ?")
            params.append(saga_id)
        if status_filter:
            clauses.append("status = ?")
            params.append(status_filter)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = c.execute(
            f"SELECT * FROM approval_requests {where} ORDER BY created_at DESC",
            params,
        ).fetchall()
    requests = []
    for r in rows:
        d = rowdict(r)
        d["payload"] = decode_json_object(d.pop("payload_json", None))
        d["created_at"] = iso(d["created_at"])
        if d.get("resolved_at"):
            d["resolved_at"] = iso(d["resolved_at"])
        requests.append(d)
    return ok(requests=requests)


# ---------------------------------------------------------------------------
# Layer 3: event-driven dispatch intents (the reactor's work queue)
# ---------------------------------------------------------------------------
