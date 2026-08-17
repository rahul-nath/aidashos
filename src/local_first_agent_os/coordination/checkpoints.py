# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Durable frontier-process events, checkpoints, and continuation decisions."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from typing import Any

from ..contracts import (
    CheckpointStatus,
    DispatchIntentStatus,
    LeaseStatus,
    ProgressAssessmentStatus,
)
from .dispatch import dispatch_intent_to_dict, notify_dispatch_status_change
from .frontier_usage import project_frontier_event
from .store import (
    connect,
    decode_json_object,
    emit,
    err,
    iso,
    now,
    ok,
    rowdict,
    sql_status_list,
    tx,
)

logger = logging.getLogger(__name__)

_EVENT_SOURCES = {"stdout", "stderr", "lifecycle"}
_LEASE_LIVE = sql_status_list(LeaseStatus.ACTIVE, LeaseStatus.CANCEL_REQUESTED)
_DISPATCH_IN_REVIEW = sql_status_list(
    DispatchIntentStatus.CHECKPOINT_REVIEW, DispatchIntentStatus.PAUSED
)


_CHECKPOINT_REASONS = {"deadline", "operator_cancel", "supervisor_error", "stalled_progress"}
_CHECKPOINT_STATUSES = {status.value for status in CheckpointStatus}
# Recovery review only makes sense for a run that stopped, not one already decided.
_RECOVERABLE_CHECKPOINTS = (CheckpointStatus.PAUSED, CheckpointStatus.FAILED)
_REVIEW_DECISIONS = {"resume_one", "split", "pause_operator", "abandon"}
_CHECKPOINT_NAMESPACE = uuid.UUID("e2444234-d1c0-4bee-b3a1-900f52ddd781")
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def execution_event_to_dict(r: dict[str, Any]) -> dict[str, Any]:
    d = rowdict(r)
    d["occurred_at"] = iso(d["occurred_at"])
    d["created_at"] = iso(d["created_at"])
    d["payload"] = decode_json_object(d.pop("payload_json", None))
    return d


def checkpoint_to_dict(r: dict[str, Any]) -> dict[str, Any]:
    d = rowdict(r)
    d["created_at"] = iso(d["created_at"])
    if d.get("decided_at"):
        d["decided_at"] = iso(d["decided_at"])
    d["decision"] = decode_json_object(d.pop("decision_json", None))
    return d


def append_execution_event(
    lease_id: str,
    sequence: int,
    occurred_at: float,
    source: str,
    kind: str,
    payload: dict[str, Any],
    payload_sha256: str,
) -> dict[str, Any]:
    """Append one normalized event, idempotently by ``(lease_id, sequence)``."""

    if sequence < 1:
        return err("invalid_sequence", sequence=sequence)
    if source not in _EVENT_SOURCES:
        return err("invalid_source", source=source, valid=sorted(_EVENT_SOURCES))
    if not kind.strip():
        return err("invalid_kind", message="kind is required")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    computed = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if computed != payload_sha256:
        return err(
            "payload_hash_mismatch",
            expected=computed,
            provided=payload_sha256,
        )
    event_id = str(uuid.uuid5(_CHECKPOINT_NAMESPACE, f"event:{lease_id}:{sequence}"))
    t = now()
    with tx() as c:
        lease = c.execute(
            "SELECT * FROM agent_execution_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
        if not lease:
            return err("lease_not_found", lease_id=lease_id)
        existing = c.execute(
            "SELECT * FROM agent_execution_events WHERE lease_id=? AND sequence=?",
            (lease_id, sequence),
        ).fetchone()
        if existing:
            if existing["payload_sha256"] != payload_sha256:
                return err(
                    "sequence_conflict",
                    lease_id=lease_id,
                    sequence=sequence,
                    existing_payload_sha256=existing["payload_sha256"],
                )
            event = existing
            created = False
        else:
            c.execute(
                """
                INSERT INTO agent_execution_events(
                    event_id, lease_id, sequence, occurred_at, source, kind,
                    payload_json, payload_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    lease_id,
                    sequence,
                    occurred_at,
                    source,
                    kind,
                    canonical,
                    payload_sha256,
                    t,
                ),
            )
            event = c.execute(
                "SELECT * FROM agent_execution_events WHERE event_id=?", (event_id,)
            ).fetchone()
            created = True
            if kind == "activity.progress":
                c.execute(
                    f"""
                    UPDATE agent_execution_leases
                    SET activity_status='PROGRESSING',
                        last_meaningful_progress_at=?,
                        last_meaningful_progress_sequence=?
                    WHERE lease_id=? AND status IN ({_LEASE_LIVE})
                    """,
                    (occurred_at, sequence, lease_id),
                )
            elif kind == "activity.quiet":
                c.execute(
                    "UPDATE agent_execution_leases SET activity_status='QUIET' "
                    f"WHERE lease_id=? AND status IN ({_LEASE_LIVE})",
                    (lease_id,),
                )
            elif kind == "activity.stalled_suspected":
                c.execute(
                    "UPDATE agent_execution_leases SET activity_status='STALLED_SUSPECTED' "
                    f"WHERE lease_id=? AND status IN ({_LEASE_LIVE})",
                    (lease_id,),
                )
            elif kind == "progress_assessment.started":
                c.execute(
                    "UPDATE agent_execution_leases SET "
                    f"progress_assessment_status='{ProgressAssessmentStatus.RUNNING}' "
                    "WHERE lease_id=?",
                    (lease_id,),
                )
            elif kind == "progress_assessment.completed":
                c.execute(
                    f"""
                    UPDATE agent_execution_leases
                    SET progress_assessment_status='{ProgressAssessmentStatus.COMPLETED}',
                        progress_assessment_decision_json=?,
                        progress_assessment_error=NULL,
                        progress_assessed_at=?
                    WHERE lease_id=?
                    """,
                    (canonical, occurred_at, lease_id),
                )
            elif kind == "progress_assessment.failed":
                c.execute(
                    f"""
                    UPDATE agent_execution_leases
                    SET progress_assessment_status='{ProgressAssessmentStatus.FAILED}',
                        progress_assessment_error=?, progress_assessed_at=?
                    WHERE lease_id=?
                    """,
                    (str(payload.get("error") or "assessment failed"), occurred_at, lease_id),
                )
    projection_error: str | None = None
    try:
        # The raw event is the evidence and has already committed. Projections
        # are idempotent derived state on a separate transaction so malformed
        # provider metadata cannot erase the event that lets us repair it.
        with tx() as c:
            project_frontier_event(
                c,
                lease=lease,
                sequence=sequence,
                kind=kind,
                payload=payload,
                created_at=t,
            )
    except Exception as exc:  # noqa: BLE001 - evidence must survive a derived-view defect
        projection_error = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "frontier event projection failed for lease %s sequence %s: %s",
            lease_id,
            sequence,
            projection_error,
        )
    data = ok(
        created=created,
        event=execution_event_to_dict(event),
        projection_error=projection_error,
    )
    emit(
        "append_execution_event",
        {
            "event_id": event_id,
            "lease_id": lease_id,
            "sequence": sequence,
            "kind": kind,
            "projection_error": projection_error,
        },
    )
    return data


def list_execution_events(
    lease_id: str,
    *,
    after_sequence: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    if after_sequence < 0:
        return err("invalid_after_sequence", after_sequence=after_sequence)
    if limit < 1 or limit > 1000:
        return err("invalid_limit", limit=limit, valid="1..1000")
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM agent_execution_events "
            "WHERE lease_id=? AND sequence>? ORDER BY sequence LIMIT ?",
            (lease_id, after_sequence, limit),
        ).fetchall()
    return ok(events=[execution_event_to_dict(row) for row in rows])


def execution_artifact_to_dict(r: dict[str, Any]) -> dict[str, Any]:
    d = rowdict(r)
    d["created_at"] = iso(d["created_at"])
    return d


def attach_execution_artifact(
    lease_id: str,
    artifact_id: str,
    role: str,
    schema_version: str,
) -> dict[str, Any]:
    """Attach an application artifact directly to its execution lease."""

    if not artifact_id.strip() or not role.strip() or not schema_version.strip():
        return err(
            "invalid_execution_artifact",
            message="artifact_id, role, and schema_version are required",
        )
    link_id = str(uuid.uuid5(_CHECKPOINT_NAMESPACE, f"artifact:{lease_id}:{artifact_id}:{role}"))
    t = now()
    with tx() as c:
        if not c.execute(
            "SELECT lease_id FROM agent_execution_leases WHERE lease_id=?", (lease_id,)
        ).fetchone():
            return err("lease_not_found", lease_id=lease_id)
        existing = c.execute(
            "SELECT * FROM agent_execution_artifacts WHERE lease_id=? AND artifact_id=? AND role=?",
            (lease_id, artifact_id, role),
        ).fetchone()
        if existing:
            link = existing
            created = False
        else:
            c.execute(
                "INSERT INTO agent_execution_artifacts("
                "execution_artifact_id, lease_id, artifact_id, role, schema_version, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (link_id, lease_id, artifact_id, role, schema_version, t),
            )
            link = c.execute(
                "SELECT * FROM agent_execution_artifacts WHERE execution_artifact_id=?",
                (link_id,),
            ).fetchone()
            created = True
    data = ok(created=created, execution_artifact=execution_artifact_to_dict(link))
    emit(
        "attach_execution_artifact",
        {"lease_id": lease_id, "artifact_id": artifact_id, "role": role},
    )
    return data


def list_execution_artifacts(lease_id: str) -> dict[str, Any]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM agent_execution_artifacts WHERE lease_id=? ORDER BY created_at",
            (lease_id,),
        ).fetchall()
    return ok(execution_artifacts=[execution_artifact_to_dict(row) for row in rows])


def _checkpoint_review_prompt(
    *,
    checkpoint_id: str,
    lease_id: str,
    task_contract: str,
    worktree_path: str | None,
    base_head_sha: str | None,
    transcript_artifact_id: str | None,
    patch_artifact_id: str | None,
    git_status_artifact_id: str | None,
    test_summary_artifact_id: str | None,
    event_summary: str,
) -> str:
    return f"""Assess a preserved frontier-execution checkpoint using only visible evidence.

checkpoint_id: {checkpoint_id}
lease_id: {lease_id}
worktree_path: {worktree_path or "none"}
base_head_sha: {base_head_sha or "none"}
transcript_artifact_id: {transcript_artifact_id or "none"}
patch_artifact_id: {patch_artifact_id or "none"}
git_status_artifact_id: {git_status_artifact_id or "none"}
test_summary_artifact_id: {test_summary_artifact_id or "none"}

Visible lifecycle summary:
{event_summary or "No safe event summary was available."}

Original task contract and permission envelope:
{task_contract}

Return JSON only with schema_version checkpoint_review.v1 and decision one of
resume_one, split, pause_operator, abandon. Include completed_work,
remaining_work, continuations, risks, and rationale. Do not infer private
reasoning. Continuations cannot widen scope or remove approval gates."""


def create_execution_checkpoint(
    lease_id: str,
    *,
    reason: str,
    status: str,
    saga_id: str | None = None,
    pow_wow_id: str | None = None,
    worktree_path: str | None = None,
    source_repo_path: str | None = None,
    base_head_sha: str | None = None,
    transcript_artifact_id: str | None = None,
    patch_artifact_id: str | None = None,
    git_status_artifact_id: str | None = None,
    test_summary_artifact_id: str | None = None,
    task_contract: str = "",
    event_summary: str = "",
    submit_review: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    """Create one recovery checkpoint per lease and optionally enqueue review."""

    if reason not in _CHECKPOINT_REASONS:
        return err("invalid_reason", reason=reason, valid=sorted(_CHECKPOINT_REASONS))
    if status not in _CHECKPOINT_STATUSES:
        return err("invalid_status", status=status, valid=sorted(_CHECKPOINT_STATUSES))
    if submit_review and CheckpointStatus(str(status)) is not CheckpointStatus.PENDING_JUNIOR:
        return err("review_requires_pending_junior", status=status)
    checkpoint_id = str(uuid.uuid5(_CHECKPOINT_NAMESPACE, f"checkpoint:{lease_id}"))
    review_intent_id = str(uuid.uuid5(_CHECKPOINT_NAMESPACE, f"review:{checkpoint_id}"))
    t = now()
    # Set when this call is the thing that stops an intent moving, so the wake can
    # be sent after the commit rather than from inside it.
    parked_intent_id: str | None = None
    with tx() as c:
        lease = c.execute(
            "SELECT * FROM agent_execution_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
        if not lease:
            return err("lease_not_found", lease_id=lease_id)
        existing = c.execute(
            "SELECT * FROM agent_execution_checkpoints WHERE lease_id=?", (lease_id,)
        ).fetchone()
        if existing:
            checkpoint = existing
            created = False
        else:
            intent_id = lease["intent_id"]
            c.execute(
                """
                INSERT INTO agent_execution_checkpoints(
                    checkpoint_id, lease_id, intent_id, saga_id, pow_wow_id,
                    reason, status, worktree_path, source_repo_path, base_head_sha,
                    transcript_artifact_id, patch_artifact_id,
                    git_status_artifact_id, test_summary_artifact_id,
                    review_intent_id, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    lease_id,
                    intent_id,
                    saga_id,
                    pow_wow_id,
                    reason,
                    status,
                    worktree_path,
                    source_repo_path,
                    base_head_sha,
                    transcript_artifact_id,
                    patch_artifact_id,
                    git_status_artifact_id,
                    test_summary_artifact_id,
                    None,
                    error,
                    t,
                ),
            )
            if intent_id:
                next_intent_status = "CHECKPOINT_REVIEW" if submit_review else "PAUSED"
                c.execute(
                    "UPDATE dispatch_intents SET status=? WHERE intent_id=? "
                    f"AND status='{DispatchIntentStatus.CLAIMED}'",
                    (next_intent_status, intent_id),
                )
                parked_intent_id = str(intent_id)
            if submit_review and intent_id:
                original = c.execute(
                    "SELECT * FROM dispatch_intents WHERE intent_id=?", (intent_id,)
                ).fetchone()
                if original:
                    prompt = _checkpoint_review_prompt(
                        checkpoint_id=checkpoint_id,
                        lease_id=lease_id,
                        task_contract=task_contract,
                        worktree_path=worktree_path,
                        base_head_sha=base_head_sha,
                        transcript_artifact_id=transcript_artifact_id,
                        patch_artifact_id=patch_artifact_id,
                        git_status_artifact_id=git_status_artifact_id,
                        test_summary_artifact_id=test_summary_artifact_id,
                        event_summary=event_summary,
                    )
                    c.execute(
                        f"""
                        INSERT INTO dispatch_intents(
                            intent_id, tier, kind, prompt, target_project_id,
                            source, status, created_at, fanout, allow_tiers,
                            reduce, parent_intent_id, intent_role, checkpoint_id
                        ) VALUES (?, 'junior', 'advisory', ?, ?, ?,
                                  '{DispatchIntentStatus.PENDING}', ?,
                                  1, '[]', 'none', ?, 'single', ?)
                        """,
                        (
                            review_intent_id,
                            prompt,
                            original["target_project_id"],
                            f"execution_checkpoint:{checkpoint_id}:review",
                            t,
                            intent_id,
                            checkpoint_id,
                        ),
                    )
                    c.execute(
                        "UPDATE agent_execution_checkpoints SET review_intent_id=? "
                        "WHERE checkpoint_id=?",
                        (review_intent_id, checkpoint_id),
                    )
            checkpoint = c.execute(
                "SELECT * FROM agent_execution_checkpoints WHERE checkpoint_id=?",
                (checkpoint_id,),
            ).fetchone()
            created = True
    if parked_intent_id:
        # This is the transition that produced the live 1,800-second wait: the
        # supervisor parked the intent and nothing told the milestone, so it slept
        # out its whole bound and then reported that the agent never answered.
        notify_dispatch_status_change(parked_intent_id)
    data = ok(created=created, checkpoint=checkpoint_to_dict(checkpoint))
    emit(
        "create_execution_checkpoint",
        {
            "checkpoint_id": checkpoint_id,
            "lease_id": lease_id,
            "reason": reason,
            "status": status,
            "review_intent_id": review_intent_id if submit_review else None,
        },
    )
    return data


def get_execution_checkpoint(checkpoint_id: str) -> dict[str, Any]:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM agent_execution_checkpoints WHERE checkpoint_id=?",
            (checkpoint_id,),
        ).fetchone()
    if not row:
        return err("not_found", checkpoint_id=checkpoint_id)
    return ok(checkpoint=checkpoint_to_dict(row))


def request_recovery_staff_review(
    checkpoint_id: str,
    *,
    target_project_id: str,
    branch: str,
    base_sha: str,
    commit_sha: str,
    milestone_id: str | None = None,
) -> dict[str, Any]:
    """Atomically enqueue one staff-only review for an exact retained commit."""

    if not target_project_id.strip():
        return err("target_project_required")
    if not branch.strip():
        return err("branch_required")
    if not _FULL_SHA.fullmatch(base_sha):
        return err("invalid_base_sha", base_sha=base_sha)
    if not _FULL_SHA.fullmatch(commit_sha):
        return err("invalid_commit_sha", commit_sha=commit_sha)
    source = f"execution_checkpoint:{checkpoint_id}:recovery_staff_review"
    intent_id = str(uuid.uuid5(_CHECKPOINT_NAMESPACE, f"recovery-staff:{checkpoint_id}"))
    created = False
    with tx() as c:
        checkpoint = c.execute(
            "SELECT * FROM agent_execution_checkpoints WHERE checkpoint_id=?",
            (checkpoint_id,),
        ).fetchone()
        if not checkpoint:
            return err("not_found", checkpoint_id=checkpoint_id)
        if CheckpointStatus(str(checkpoint["status"])) not in _RECOVERABLE_CHECKPOINTS:
            return err(
                "recovery_staff_review_requires_paused_checkpoint",
                checkpoint_id=checkpoint_id,
                status=checkpoint["status"],
            )
        if checkpoint["base_head_sha"] and checkpoint["base_head_sha"] != base_sha:
            return err(
                "checkpoint_base_mismatch",
                checkpoint_base_sha=checkpoint["base_head_sha"],
                requested_base_sha=base_sha,
            )
        if not checkpoint["saga_id"]:
            return err("checkpoint_saga_required", checkpoint_id=checkpoint_id)
        original = (
            c.execute(
                "SELECT * FROM dispatch_intents WHERE intent_id=?",
                (checkpoint["intent_id"],),
            ).fetchone()
            if checkpoint["intent_id"]
            else None
        )
        if original and original["target_project_id"] not in {None, target_project_id}:
            return err(
                "checkpoint_target_mismatch",
                checkpoint_target_project_id=original["target_project_id"],
                requested_target_project_id=target_project_id,
            )
        payload = {
            "schema_version": "recovery_staff_review_request.v1",
            "checkpoint_id": checkpoint_id,
            "saga_id": checkpoint["saga_id"],
            "target_project_id": target_project_id,
            "branch": branch,
            "base_sha": base_sha,
            "commit_sha": commit_sha,
            "milestone_id": milestone_id,
            "review_origin": "RECOVERY_STAFF",
            "permission_envelope": "read-only review; revisions only after BLOCK",
        }
        existing = c.execute(
            "SELECT * FROM dispatch_intents WHERE intent_id=? OR source=?",
            (intent_id, source),
        ).fetchone()
        if existing:
            try:
                existing_payload = json.loads(existing["prompt"])
            except (json.JSONDecodeError, TypeError):
                existing_payload = None
            if existing_payload != payload:
                return err(
                    "recovery_staff_review_conflict",
                    checkpoint_id=checkpoint_id,
                    existing_intent_id=existing["intent_id"],
                )
            intent = existing
        else:
            t = now()
            c.execute(
                f"""
                INSERT INTO dispatch_intents(
                    intent_id, tier, kind, prompt, target_project_id, source,
                    status, created_at, fanout, allow_tiers, reduce,
                    parent_intent_id, intent_role, checkpoint_id
                ) VALUES (?, 'staff', 'code', ?, ?, ?,
                          '{DispatchIntentStatus.PENDING}', ?, 1, '[]',
                          'none', ?, 'single', ?)
                """,
                (
                    intent_id,
                    json.dumps(payload, indent=2, sort_keys=True),
                    target_project_id,
                    source,
                    t,
                    checkpoint["intent_id"],
                    checkpoint_id,
                ),
            )
            intent = c.execute(
                "SELECT * FROM dispatch_intents WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
            created = True
    data = ok(
        created=created,
        intent=dispatch_intent_to_dict(intent),
        checkpoint_id=checkpoint_id,
        next_step="pi /dispatch",
    )
    emit(
        "request_recovery_staff_review",
        {
            "checkpoint_id": checkpoint_id,
            "intent_id": intent_id,
            "created": created,
            "branch": branch,
            "base_sha": base_sha,
            "commit_sha": commit_sha,
        },
    )
    return data


def list_execution_checkpoints(status_filter: str | None = None) -> dict[str, Any]:
    if status_filter is not None and status_filter not in _CHECKPOINT_STATUSES:
        return err("invalid_status", status=status_filter, valid=sorted(_CHECKPOINT_STATUSES))
    with connect() as c:
        if status_filter:
            rows = c.execute(
                "SELECT * FROM agent_execution_checkpoints WHERE status=? ORDER BY created_at DESC",
                (status_filter,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM agent_execution_checkpoints ORDER BY created_at DESC"
            ).fetchall()
    return ok(checkpoints=[checkpoint_to_dict(row) for row in rows])


def _validated_review_decision(decision: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    if decision.get("schema_version") != "checkpoint_review.v1":
        raise ValueError("decision schema_version must be checkpoint_review.v1")
    choice = str(decision.get("decision") or "")
    if choice not in _REVIEW_DECISIONS:
        raise ValueError(f"invalid checkpoint decision: {choice!r}")
    raw_continuations = decision.get("continuations") or []
    if not isinstance(raw_continuations, list) or not all(
        isinstance(item, dict) for item in raw_continuations
    ):
        raise ValueError("continuations must be a list of objects")
    continuations = list(raw_continuations)
    if choice == "resume_one" and len(continuations) != 1:
        raise ValueError("resume_one requires exactly one continuation")
    if choice == "split" and len(continuations) < 2:
        raise ValueError("split requires at least two continuations")
    if choice in {"pause_operator", "abandon"} and continuations:
        raise ValueError(f"{choice} cannot create continuations")
    for item in continuations:
        if not str(item.get("title") or "").strip():
            raise ValueError("each continuation requires a title")
        if not str(item.get("scope") or "").strip():
            raise ValueError("each continuation requires a bounded scope")
        if not isinstance(item.get("acceptance_criteria", []), list):
            raise ValueError("continuation acceptance_criteria must be a list")
        if not isinstance(item.get("verification_commands", []), list):
            raise ValueError("continuation verification_commands must be a list")
    return choice, continuations


def decide_execution_checkpoint(
    checkpoint_id: str,
    decision: dict[str, Any],
    *,
    junior_review_artifact_id: str | None = None,
) -> dict[str, Any]:
    """Validate review output and atomically create bounded continuations."""

    try:
        choice, continuations = _validated_review_decision(decision)
    except ValueError as exc:
        with tx() as c:
            row = c.execute(
                "SELECT * FROM agent_execution_checkpoints WHERE checkpoint_id=?",
                (checkpoint_id,),
            ).fetchone()
            if not row:
                return err("not_found", checkpoint_id=checkpoint_id)
            c.execute(
                f"UPDATE agent_execution_checkpoints SET status='{CheckpointStatus.PAUSED}', "
                "error=? "
                f"WHERE checkpoint_id=? AND status!='{CheckpointStatus.DECIDED}'",
                (f"invalid junior review: {exc}", checkpoint_id),
            )
            if row["intent_id"]:
                c.execute(
                    f"UPDATE dispatch_intents SET status='{DispatchIntentStatus.PAUSED}' "
                    "WHERE intent_id=? "
                    f"AND status='{DispatchIntentStatus.CHECKPOINT_REVIEW}'",
                    (row["intent_id"],),
                )
            updated = c.execute(
                "SELECT * FROM agent_execution_checkpoints WHERE checkpoint_id=?",
                (checkpoint_id,),
            ).fetchone()
        if row["intent_id"]:
            notify_dispatch_status_change(str(row["intent_id"]))
        data = err(
            "invalid_checkpoint_review",
            message=str(exc),
            checkpoint=checkpoint_to_dict(updated),
        )
        emit("decide_execution_checkpoint", data)
        return data

    t = now()
    continuation_ids: list[str] = []
    with tx() as c:
        row = c.execute(
            "SELECT * FROM agent_execution_checkpoints WHERE checkpoint_id=?",
            (checkpoint_id,),
        ).fetchone()
        if not row:
            return err("not_found", checkpoint_id=checkpoint_id)
        if CheckpointStatus(str(row["status"])) is CheckpointStatus.DECIDED:
            existing = c.execute(
                "SELECT intent_id FROM dispatch_intents WHERE checkpoint_id=? "
                "AND source LIKE ? ORDER BY created_at",
                (checkpoint_id, f"execution_checkpoint:{checkpoint_id}:continuation:%"),
            ).fetchall()
            return ok(
                created=False,
                checkpoint=checkpoint_to_dict(row),
                continuation_intent_ids=[item["intent_id"] for item in existing],
            )
        original = (
            c.execute(
                "SELECT * FROM dispatch_intents WHERE intent_id=?", (row["intent_id"],)
            ).fetchone()
            if row["intent_id"]
            else None
        )
        for index, item in enumerate(continuations, start=1):
            continuation_id = str(
                uuid.uuid5(_CHECKPOINT_NAMESPACE, f"continuation:{checkpoint_id}:{index}")
            )
            continuation_ids.append(continuation_id)
            prompt = json.dumps(
                {
                    "schema_version": "checkpoint_continuation.v1",
                    "checkpoint_id": checkpoint_id,
                    "preserved_worktree_path": row["worktree_path"],
                    "patch_artifact_id": row["patch_artifact_id"],
                    "base_head_sha": row["base_head_sha"],
                    "reuse_preserved_worktree": choice == "resume_one",
                    "title": item["title"],
                    "scope": item["scope"],
                    "depends_on": item.get("depends_on", []),
                    "acceptance_criteria": item.get("acceptance_criteria", []),
                    "verification_commands": item.get("verification_commands", []),
                    "permission_envelope": "inherits original; no widening",
                },
                indent=2,
                sort_keys=True,
            )
            c.execute(
                f"""
                INSERT INTO dispatch_intents(
                    intent_id, tier, kind, prompt, target_project_id, source,
                    status, created_at, fanout, allow_tiers, reduce,
                    parent_intent_id, intent_role, checkpoint_id
                ) VALUES (?, ?, ?, ?, ?, ?, '{DispatchIntentStatus.PENDING}', ?, 1, '[]',
                          'none', ?, 'single', ?)
                """,
                (
                    continuation_id,
                    original["tier"] if original else "senior",
                    original["kind"] if original else "code",
                    prompt,
                    original["target_project_id"] if original else None,
                    f"execution_checkpoint:{checkpoint_id}:continuation:{index}",
                    t + (index / 1000),
                    row["intent_id"],
                    checkpoint_id,
                ),
            )
        checkpoint_status = "PAUSED" if choice in {"pause_operator", "abandon"} else "DECIDED"
        c.execute(
            """
            UPDATE agent_execution_checkpoints
            SET status=?, decision_json=?, junior_review_artifact_id=?, decided_at=?
            WHERE checkpoint_id=?
            """,
            (
                checkpoint_status,
                json.dumps(decision, sort_keys=True),
                junior_review_artifact_id,
                t,
                checkpoint_id,
            ),
        )
        if row["intent_id"]:
            original_status = (
                "SUPERSEDED" if continuations else "CANCELED" if choice == "abandon" else "PAUSED"
            )
            c.execute(
                "UPDATE dispatch_intents SET status=?, completed_at=? "
                f"WHERE intent_id=? AND status IN ({_DISPATCH_IN_REVIEW})",
                (original_status, t, row["intent_id"]),
            )
        updated = c.execute(
            "SELECT * FROM agent_execution_checkpoints WHERE checkpoint_id=?",
            (checkpoint_id,),
        ).fetchone()
    if row["intent_id"]:
        # Whatever the decision was, the original intent has stopped moving:
        # superseded by continuations, cancelled, or paused for an operator.
        notify_dispatch_status_change(str(row["intent_id"]))
    data = ok(
        created=True,
        checkpoint=checkpoint_to_dict(updated),
        continuation_intent_ids=continuation_ids,
    )
    emit(
        "decide_execution_checkpoint",
        {
            "checkpoint_id": checkpoint_id,
            "decision": choice,
            "continuation_intent_ids": continuation_ids,
        },
    )
    return data


__all__ = [
    "attach_execution_artifact",
    "append_execution_event",
    "checkpoint_to_dict",
    "create_execution_checkpoint",
    "decide_execution_checkpoint",
    "execution_event_to_dict",
    "execution_artifact_to_dict",
    "get_execution_checkpoint",
    "list_execution_checkpoints",
    "list_execution_events",
    "list_execution_artifacts",
    "request_recovery_staff_review",
]
