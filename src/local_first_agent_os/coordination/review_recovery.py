# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Durable adoption path for host-stamped staff reviews missed by an old parser."""

from __future__ import annotations

from typing import Any

from ..contracts import ApprovalRequestType, DispatchIntentStatus
from ..review_recovery import ReviewRecoveryRefused, recover_unparsed_dispatch_review
from .approvals import submit_idempotent_approval_request
from .store import connect, emit, err, ok, rowdict


def recover_unparsed_staff_review(intent_id: str) -> dict[str, Any]:
    """Open the normal merge gate for one exact, independently approved commit."""

    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM dispatch_intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
    if row is None:
        return err("not_found", intent_id=intent_id)
    intent = rowdict(row)
    if intent.get("status") != DispatchIntentStatus.FAILED.value:
        return err(
            "dispatch_intent_not_failed",
            intent_id=intent_id,
            status=intent.get("status"),
        )
    if intent.get("kind") != "code":
        return err("dispatch_intent_not_code", intent_id=intent_id)
    target_project_id = str(intent.get("target_project_id") or "").strip()
    if not target_project_id:
        return err("target_project_required", intent_id=intent_id)
    try:
        recovery = recover_unparsed_dispatch_review(intent_id, intent.get("result"))
    except ReviewRecoveryRefused as exc:
        return err(exc.code, intent_id=intent_id, message=str(exc))

    dispatch_result = recovery.dispatch_result
    saga_id = str(dispatch_result.get("saga_id") or "").strip()
    pow_wow_id = str(dispatch_result.get("pow_wow_id") or "").strip()
    if not saga_id:
        return err("dispatch_result_saga_missing", intent_id=intent_id)
    checkpoint = recovery.checkpoint
    branch = str(checkpoint.get("branch_name") or "").strip()
    base_sha = str(checkpoint.get("base_head_sha") or "").strip()
    commit_sha = str(checkpoint.get("commit_sha") or "").strip()
    changed_files = list(dispatch_result["run_result"].get("changed_files") or ())
    approval = submit_idempotent_approval_request(
        saga_id,
        ApprovalRequestType.CODE_MERGE.value,
        idempotency_key=f"unparsed-staff-review:{intent_id}",
        requested_by="review_verdict_recovery",
        payload={
            "intent_id": intent_id,
            "pow_wow_id": pow_wow_id,
            "executor_status": "COMPLETED",
            "target_project_id": target_project_id,
            "changed_files": changed_files,
            "branch": branch,
            "base_sha": base_sha,
            "commit_sha": commit_sha,
            "dispatch_result": dispatch_result,
            "review_recovery": {
                "schema_version": "review_verdict_recovery.v1",
                "source_intent_id": intent_id,
                "source_review_sha256": recovery.source_review_sha256,
                "decision_line": recovery.decision_line,
            },
        },
    )
    if approval.get("ok") is not True:
        return approval
    data = ok(
        created=approval["created"],
        intent_id=intent_id,
        approval_id=approval["approval_id"],
        approval_status=approval["status"],
        target_project_id=target_project_id,
        branch=branch,
        base_sha=base_sha,
        commit_sha=commit_sha,
        decision_line=recovery.decision_line,
        next_step=f"review and resolve CODE_MERGE approval {approval['approval_id']}",
    )
    emit("recover_unparsed_staff_review", data)
    return data


__all__ = ["recover_unparsed_staff_review"]
