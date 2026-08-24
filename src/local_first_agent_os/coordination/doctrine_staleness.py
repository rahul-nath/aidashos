# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read-only scan: which MERGE_PENDING reviews the merge gate would now refuse.

A doctrine bump invalidates every review stamped under the earlier version, by
design. The bump itself is silent at the moment it happens: the invalidated
reviews only surface one at a time, when an operator tries to approve each
merge. This scan makes the blast radius one command instead of N failed
approvals.

Deliberately a query, not a repair. Re-reviewing a commit spends a staff-tier
frontier seat, and a code edit must not fan out spending on its own. The scan
reports each stale review with the same named diagnosis the gate raises, and
the CLI's next-command affordance turns each row into the exact recovery
command the operator may choose to run.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..contracts import ApprovalRequestType, ApprovalStatus, CheckpointStatus
from ..dispatch_results import normalize_dispatch_runner_result
from ..engineering_doctrine import CURRENT_ENGINEERING_DOCTRINE
from ..review_recovery import diagnose_staff_review_provenance, merge_gate_evidence
from ..work_units.dispatch_adoption import WORK_UNIT_DISPATCH_SOURCE
from .outcomes import DispatchPromotionState
from .store import connect, decode_json_object, ok, rowdict

SCHEMA_VERSION_DOCTRINE_STALENESS_SCAN = "doctrine_staleness_scan.v1"

_RECOVERABLE = {CheckpointStatus.PAUSED.value, CheckpointStatus.FAILED.value}


def list_doctrine_stale_reviews() -> dict[str, Any]:
    """Diagnose every MERGE_PENDING dispatch result against the current doctrine.

    Every row is judged by the same ``diagnose_staff_review_provenance`` the
    merge gate itself runs, over the same ``merge_gate_evidence`` selection, so
    a review this scan calls current is one the gate will accept.
    """

    with connect() as connection:
        intent_rows = [
            rowdict(row)
            for row in connection.execute(
                "SELECT * FROM dispatch_intents WHERE result IS NOT NULL"
            ).fetchall()
        ]
        approval_rows = [
            rowdict(row)
            for row in connection.execute(
                "SELECT * FROM approval_requests WHERE request_type = ? AND status = ?",
                (ApprovalRequestType.CODE_MERGE.value, ApprovalStatus.PENDING.value),
            ).fetchall()
        ]
        checkpoint_rows = [
            rowdict(row)
            for row in connection.execute("SELECT * FROM agent_execution_checkpoints").fetchall()
        ]

    pending_approvals: dict[str, str] = {}
    for approval in approval_rows:
        payload = decode_json_object(approval.get("payload_json"))
        intent_id = str(payload.get("intent_id") or "").strip()
        if intent_id:
            pending_approvals[intent_id] = str(approval.get("approval_id") or "")

    # Two ways an intent reaches a checkpoint, and both occur. A dispatch that
    # parked mid-run owns the checkpoint through the checkpoint's own
    # `intent_id`. The recovery-review intent that `request_recovery_staff_review`
    # enqueues is a different row, and reaches the same checkpoint through its
    # own `checkpoint_id` column - which is precisely the intent whose review a
    # later doctrine bump invalidates.
    checkpoints_by_id: dict[str, dict[str, Any]] = {
        str(checkpoint.get("checkpoint_id") or ""): checkpoint for checkpoint in checkpoint_rows
    }
    checkpoints_by_intent: dict[str, list[dict[str, Any]]] = {}
    for checkpoint in checkpoint_rows:
        intent_id = str(checkpoint.get("intent_id") or "")
        if intent_id:
            checkpoints_by_intent.setdefault(intent_id, []).append(checkpoint)

    merge_pending = 0
    unreadable: list[dict[str, Any]] = []
    current: list[str] = []
    stale: list[dict[str, Any]] = []
    for intent in intent_rows:
        intent_id = str(intent.get("intent_id") or "")
        try:
            dispatch_result = normalize_dispatch_runner_result(
                intent_result=intent.get("result"),
                approval_payload={},
            )
        except ValueError as exc:
            unreadable.append({"intent_id": intent_id, "error": str(exc)})
            continue
        if dispatch_result.promotion_state is not DispatchPromotionState.MERGE_PENDING:
            continue
        merge_pending += 1
        row = _diagnose_intent(intent, dispatch_result.run_result)
        if row is None:
            current.append(intent_id)
            continue
        row["approval_id"] = pending_approvals.get(intent_id)
        own = checkpoints_by_id.get(str(intent.get("checkpoint_id") or ""))
        candidates = ([own] if own is not None else []) + checkpoints_by_intent.get(intent_id, [])
        row["recovery_review"] = _recovery_review_facts(candidates, row)
        stale.append(row)

    return ok(
        schema_version=SCHEMA_VERSION_DOCTRINE_STALENESS_SCAN,
        current_doctrine=CURRENT_ENGINEERING_DOCTRINE.provenance_payload(),
        merge_pending=merge_pending,
        current=current,
        stale=stale,
        unreadable=unreadable,
    )


def _diagnose_intent(
    intent: Mapping[str, Any], run_result: Mapping[str, Any]
) -> dict[str, Any] | None:
    """One scan row for a failing intent, or ``None`` when the gate would accept it."""

    evidence = merge_gate_evidence(run_result)
    final = evidence.final_review
    checkpoint = evidence.checkpoint
    if final is None:
        issue_code = "staff_review_missing"
        issue_message = "the run result retains no review_result.v1 evidence"
        stamped_doctrine: Any = None
    elif not checkpoint:
        issue_code = "verified_checkpoint_missing"
        issue_message = "the run result retains no commit checkpoint to judge"
        stamped_doctrine = final.get("engineering_doctrine")
    else:
        issue = diagnose_staff_review_provenance(final, evidence.reviews[:-1], checkpoint)
        if issue is None:
            return None
        issue_code = issue.code.value
        issue_message = issue.message
        stamped_doctrine = final.get("engineering_doctrine")

    source = str(intent.get("source") or "")
    match = WORK_UNIT_DISPATCH_SOURCE.fullmatch(source)
    return {
        "intent_id": str(intent.get("intent_id") or ""),
        "intent_status": intent.get("status"),
        "source": source,
        "work_unit_id": match.group("work_unit_id") if match else None,
        "milestone_key": match.group("milestone_key") if match else None,
        "target_project_id": intent.get("target_project_id"),
        "branch": checkpoint.get("branch_name"),
        "base_sha": checkpoint.get("base_head_sha"),
        "commit_sha": checkpoint.get("commit_sha"),
        "issue_code": issue_code,
        "issue_message": issue_message,
        "stamped_doctrine": stamped_doctrine,
    }


def _recovery_review_facts(
    checkpoints: list[dict[str, Any]],
    row: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The facts a runnable ``request_recovery_staff_review`` needs, or ``None``.

    ``None`` means the verb would refuse: the verb accepts only a PAUSED or
    FAILED execution checkpoint whose base and saga agree with the retained
    commit, and a dispatch that completed cleanly leaves no such checkpoint.
    Deciding this here keeps the next-command affordance's invariant - it must
    never print a READY command that cannot run - out of the renderer's hands.
    """

    branch = str(row.get("branch") or "")
    base_sha = str(row.get("base_sha") or "")
    commit_sha = str(row.get("commit_sha") or "")
    target_project_id = str(row.get("target_project_id") or "")
    if not (branch and base_sha and commit_sha and target_project_id):
        return None
    for checkpoint in checkpoints:
        if str(checkpoint.get("status") or "") not in _RECOVERABLE:
            continue
        if not checkpoint.get("saga_id"):
            continue
        checkpoint_base = str(checkpoint.get("base_head_sha") or "")
        if checkpoint_base and checkpoint_base != base_sha:
            continue
        return {
            "checkpoint_id": str(checkpoint.get("checkpoint_id") or ""),
            "target_project_id": target_project_id,
            "branch": branch,
            "base_sha": base_sha,
            "commit_sha": commit_sha,
        }
    return None


__all__ = [
    "SCHEMA_VERSION_DOCTRINE_STALENESS_SCAN",
    "list_doctrine_stale_reviews",
]
