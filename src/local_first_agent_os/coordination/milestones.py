# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Durable saga milestone state and evidence transitions."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, assert_never

from ..contracts import MilestoneStatus, SagaStage, SagaStatus
from ..lifecycle_failure_harness import (
    LifecycleTransitionPoint,
    reach_lifecycle_transition,
)
from .contracts import DispatchTerminalStatus
from .outcomes import TerminalOutcome, classify_failure
from .store import (
    ConnectionLike,
    connect,
    decode_json_array,
    emit,
    err,
    iso,
    now,
    ok,
    rowdict,
    tx,
)

_MILESTONE_STATUSES = {status.value for status in MilestoneStatus}
_SETTLED_STATUSES = (
    MilestoneStatus.COMPLETED,
    MilestoneStatus.FAILED,
    MilestoneStatus.CANCELED,
)


_MILESTONE_TERMINAL_VALUES = tuple(status.value for status in _SETTLED_STATUSES)
_TERMINAL_PLACEHOLDERS = ", ".join("?" * len(_MILESTONE_TERMINAL_VALUES))
_DISPATCH_TERMINAL_VALUES = tuple(status.value for status in DispatchTerminalStatus)
_DISPATCH_TERMINAL_PLACEHOLDERS = ", ".join("?" * len(_DISPATCH_TERMINAL_VALUES))


class _Progress(StrEnum):
    """What a milestone status means for the saga above it.

    The projection cares about four things, not six. Naming them separately is
    what lets `_progress_of` be exhaustive over MilestoneStatus while
    `derive_saga_lifecycle` stays readable.
    """

    NOT_STARTED = "not_started"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    HALTED = "halted"


def _progress_of(status: MilestoneStatus) -> _Progress:
    """Classify one status, exhaustively.

    `assert_never` is the point of this function: adding a MilestoneStatus
    member fails the type check here rather than silently falling through to
    whatever the last branch of the projection happened to be. BLOCKED reached
    production doing exactly that, and a saga holding a blocked milestone
    reported PLANNING, meaning "nothing has happened yet".

    HALTED covers BLOCKED, FAILED, and CANCELED together because
    `retry_saga_milestone` accepts all three: none of them ends the saga, so
    none of them should read as terminal above it.
    """

    match status:
        case MilestoneStatus.PENDING:
            return _Progress.NOT_STARTED
        case MilestoneStatus.IN_PROGRESS:
            return _Progress.RUNNING
        case MilestoneStatus.COMPLETED:
            return _Progress.SUCCEEDED
        case MilestoneStatus.BLOCKED | MilestoneStatus.FAILED | MilestoneStatus.CANCELED:
            return _Progress.HALTED
    assert_never(status)


def _is_settled(status: MilestoneStatus) -> bool:
    """Whether this milestone's attempt is closed.

    Distinct from _Progress.HALTED, which includes BLOCKED. A blocked milestone
    has stopped making progress but has not finished, so reconciliation still
    considers it while a failed or canceled one is left alone.
    """

    return status in _SETTLED_STATUSES


def _ends_the_attempt(status: MilestoneStatus) -> bool:
    """Whether halting this way stamps completed_at.

    BLOCKED does not: it is waiting, not finished. FAILED and CANCELED close the
    attempt even though retry_saga_milestone can reopen it.
    """

    return status in (MilestoneStatus.FAILED, MilestoneStatus.CANCELED)


def _status_of(row: Any) -> MilestoneStatus:
    """Parse a row's status once, at the boundary where it stops being text.

    Rows come back from SQLite as strings. Every comparison downstream should be
    against a member, so the string dies here rather than spreading through the
    module as literals that no rename would find.
    """

    return MilestoneStatus(str(row["status"]))


def _halted_status_values() -> list[str]:
    """The statuses an operator may set directly, derived rather than listed."""

    return [status.value for status in MilestoneStatus if _progress_of(status) is _Progress.HALTED]


def _reload_after_transition(c: ConnectionLike, milestone_id: str, t: float) -> Any:
    """Reload a milestone and refresh its saga's projected lifecycle.

    Bundled deliberately. Every transition already reloads the row it just
    wrote, so folding the projection into that reload means a new transition
    cannot update a milestone without the saga following it. Two separate calls
    would be two things to remember, and the bug this replaces was exactly one
    of them never being written at all.
    """

    row = c.execute(
        "SELECT * FROM saga_milestones WHERE milestone_id = ?",
        (milestone_id,),
    ).fetchone()
    if row is not None:
        _apply_saga_lifecycle(c, str(rowdict(row)["saga_id"]), t)
    return row


def derive_saga_lifecycle(
    milestones: list[dict[str, Any]],
) -> tuple[SagaStatus, SagaStage] | None:
    """Derive ``(status, current_stage)`` from a saga's milestones.

    Two lifecycle models used to coexist and only one was live. The saga-stage
    model ran from ``create_saga`` through an imperative stage setter, reachable
    from exactly one call site; the milestone model is what ``dispatcher_runner``
    actually drives, and it never wrote ``sagas.status`` at all. All 43 sagas in
    the ledger sat at their creation-time ``IDEA_INTAKE`` while pest showed five
    of six milestones complete. That setter is gone: this projection is the only
    writer of both columns.

    The milestones are the source of truth and these two columns are a
    projection of them, maintained by ``_apply_saga_lifecycle`` inside the same
    transaction as every milestone transition. Returning ``None`` for a saga
    with no milestones leaves it at its creation-time value, which is honest:
    nothing has happened to it yet.
    """

    if not milestones:
        return None
    progress = [_progress_of(_status_of(row)) for row in milestones]
    if all(item is _Progress.SUCCEEDED for item in progress):
        return (SagaStatus.COMPLETED, SagaStage.USER_APPROVAL)
    if any(item is _Progress.RUNNING for item in progress):
        return (SagaStatus.ACTIVE, SagaStage.IMPLEMENTATION)

    # Nothing is running. The operator-meaningful question is whether the next
    # actionable milestone is waiting on them, which is the /approved-gawd gate.
    pending = sorted(
        (row for row in milestones if _status_of(row) is MilestoneStatus.PENDING),
        key=lambda row: int(row["sequence"]),
    )
    if pending and bool(pending[0]["approval_required"]):
        return (SagaStatus.AWAITING_APPROVAL, SagaStage.USER_APPROVAL)
    if any(item is not _Progress.NOT_STARTED for item in progress):
        return (SagaStatus.ACTIVE, SagaStage.IMPLEMENTATION)
    return (SagaStatus.PLANNING, SagaStage.REQUIREMENT_DECOMPOSITION)


def _apply_saga_lifecycle(c: ConnectionLike, saga_id: str, t: float) -> None:
    """Recompute and write the saga's projected lifecycle. Same tx as its cause."""

    rows = [
        rowdict(row)
        for row in c.execute(
            "SELECT status, sequence, approval_required FROM saga_milestones "
            "WHERE saga_id = ? ORDER BY sequence",
            (saga_id,),
        ).fetchall()
    ]
    derived = derive_saga_lifecycle(rows)
    if derived is None:
        return
    status, stage = derived
    c.execute(
        "UPDATE sagas SET status = ?, current_stage = ?, updated_at = ? WHERE saga_id = ?",
        (status.value, stage.value, t, saga_id),
    )


MILESTONE_EVIDENCE_TYPES = {
    "test_log",
    "migration_status",
    "live_check_result",
    "summary",
}


def _milestone_to_dict(r: dict[str, Any]) -> dict[str, Any]:
    d = rowdict(r)
    d["depends_on"] = decode_json_array(d.pop("depends_on_json", None))
    d["entry_criteria"] = decode_json_array(d.pop("entry_criteria_json", None))
    d["exit_criteria"] = decode_json_array(d.pop("exit_criteria_json", None))
    d["required_artifacts"] = decode_json_array(d.pop("required_artifacts_json", None))
    d["approval_required"] = bool(d["approval_required"])
    for key in ("created_at", "updated_at", "started_at", "completed_at"):
        if d.get(key):
            d[key] = iso(d[key])
    return d


def _evidence_to_dict(r: dict[str, Any]) -> dict[str, Any]:
    d = rowdict(r)
    d["created_at"] = iso(d["created_at"])
    return d


def create_saga_milestone(
    saga_id: str,
    name: str,
    sequence: int,
    *,
    milestone_id: str | None = None,
    gawd_doc_id: str | None = None,
    description: str = "",
    depends_on: list[str] | None = None,
    entry_criteria: list[str] | None = None,
    exit_criteria: list[str] | None = None,
    required_artifacts: list[str] | None = None,
    approval_required: bool = False,
) -> dict[str, Any]:
    """Create one durable saga milestone.

    Milestones are the resume boundary above pow-wows/tasks. After a crash, the
    next runnable unit is the oldest PENDING milestone whose dependencies and
    approval gate are satisfied.
    """

    if sequence < 1:
        return err("invalid_sequence", sequence=sequence)
    milestone_id = milestone_id or str(uuid.uuid4())
    t = now()
    with tx() as c:
        saga = c.execute("SELECT * FROM sagas WHERE saga_id = ?", (saga_id,)).fetchone()
        if not saga:
            return err("saga_not_found", saga_id=saga_id)
        effective_gawd_doc_id = gawd_doc_id or saga["gawd_doc_id"]
        c.execute(
            """
            INSERT INTO saga_milestones(
                milestone_id, saga_id, gawd_doc_id, sequence, name, description,
                depends_on_json, entry_criteria_json, exit_criteria_json,
                required_artifacts_json, approval_required, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                milestone_id,
                saga_id,
                effective_gawd_doc_id,
                sequence,
                name,
                description,
                json.dumps(depends_on or []),
                json.dumps(entry_criteria or []),
                json.dumps(exit_criteria or []),
                json.dumps(required_artifacts or []),
                1 if approval_required else 0,
                MilestoneStatus.PENDING.value,
                t,
                t,
            ),
        )
        row = _reload_after_transition(c, milestone_id, t)
    data = ok(milestone=_milestone_to_dict(row))
    emit("create_saga_milestone", data)
    return data


def amend_saga_milestone(
    milestone_id: str,
    *,
    reason: str,
    amended_by: str = "operator",
    description: str | None = None,
    entry_criteria: list[str] | None = None,
    exit_criteria: list[str] | None = None,
    required_artifacts: list[str] | None = None,
) -> dict[str, Any]:
    """Amend a pending milestone contract and preserve an audit record.

    Approved GAWD documents remain immutable. This operation changes only the
    pending execution projection, records the exact before/after contract as
    milestone evidence, and refuses to rewrite work that has already started.
    """

    normalized_reason = reason.strip()
    normalized_amended_by = amended_by.strip()
    if not normalized_reason:
        return err("invalid_reason", message="Milestone amendment reason is required.")
    if not normalized_amended_by:
        return err("invalid_amended_by", message="Milestone amendment actor is required.")
    if all(
        value is None
        for value in (
            description,
            entry_criteria,
            exit_criteria,
            required_artifacts,
        )
    ):
        return err("empty_amendment", message="At least one milestone field must change.")

    t = now()
    with tx() as c:
        row = _reload_after_transition(c, milestone_id, t)
        if not row:
            return err("not_found", milestone_id=milestone_id)
        if _status_of(row) is not MilestoneStatus.PENDING:
            return err(
                "milestone_not_pending",
                milestone_id=milestone_id,
                status=row["status"],
            )

        before = _milestone_to_dict(row)
        next_description = row["description"] if description is None else description
        next_entry_criteria = before["entry_criteria"] if entry_criteria is None else entry_criteria
        next_exit_criteria = before["exit_criteria"] if exit_criteria is None else exit_criteria
        next_required_artifacts = (
            before["required_artifacts"] if required_artifacts is None else required_artifacts
        )
        c.execute(
            """
            UPDATE saga_milestones
            SET description=?, entry_criteria_json=?, exit_criteria_json=?,
                required_artifacts_json=?, updated_at=?
            WHERE milestone_id=?
            """,
            (
                next_description,
                json.dumps(next_entry_criteria),
                json.dumps(next_exit_criteria),
                json.dumps(next_required_artifacts),
                t,
                milestone_id,
            ),
        )
        updated = c.execute(
            "SELECT * FROM saga_milestones WHERE milestone_id = ?",
            (milestone_id,),
        ).fetchone()
        after = _milestone_to_dict(updated)
        evidence_id = str(uuid.uuid4())
        amendment = {
            "schema_version": "milestone_contract_amendment.v1",
            "reason": normalized_reason,
            "amended_by": normalized_amended_by,
            "before": before,
            "after": after,
        }
        c.execute(
            """
            INSERT INTO milestone_evidence(
                evidence_id, milestone_id, saga_id, evidence_type, content,
                schema_version, created_at
            ) VALUES (?, ?, ?, 'summary', ?, 'milestone_contract_amendment.v1', ?)
            """,
            (
                evidence_id,
                milestone_id,
                row["saga_id"],
                json.dumps(amendment, sort_keys=True),
                t,
            ),
        )
        evidence_row = c.execute(
            "SELECT * FROM milestone_evidence WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()

    data = ok(milestone=after, evidence=_evidence_to_dict(evidence_row))
    emit(
        "amend_saga_milestone",
        {
            "milestone_id": milestone_id,
            "amended_by": normalized_amended_by,
            "reason": normalized_reason,
            "evidence_id": evidence_id,
        },
    )
    return data


def list_saga_milestones(
    saga_id: str,
    status_filter: str | None = None,
) -> dict[str, Any]:
    with connect() as c:
        if status_filter:
            rows = c.execute(
                """
                SELECT * FROM saga_milestones
                WHERE saga_id = ? AND status = ?
                ORDER BY sequence, created_at
                """,
                (saga_id, status_filter),
            ).fetchall()
        else:
            rows = c.execute(
                """
                SELECT * FROM saga_milestones
                WHERE saga_id = ?
                ORDER BY sequence, created_at
                """,
                (saga_id,),
            ).fetchall()
    return ok(milestones=[_milestone_to_dict(row) for row in rows])


def get_saga_milestone(milestone_id: str) -> dict[str, Any]:
    with connect() as c:
        row = c.execute(
            "SELECT * FROM saga_milestones WHERE milestone_id = ?",
            (milestone_id,),
        ).fetchone()
        if not row:
            return err("not_found", milestone_id=milestone_id)
        evidence_rows = c.execute(
            """
            SELECT * FROM milestone_evidence
            WHERE milestone_id = ?
            ORDER BY created_at
            """,
            (milestone_id,),
        ).fetchall()
    milestone = _milestone_to_dict(row)
    milestone["evidence"] = [_evidence_to_dict(item) for item in evidence_rows]
    return ok(milestone=milestone)


def record_milestone_evidence(
    milestone_id: str,
    evidence_type: str,
    content: str,
    *,
    schema_version: str = "milestone_evidence.v1",
) -> dict[str, Any]:
    if evidence_type not in MILESTONE_EVIDENCE_TYPES:
        return err(
            "invalid_evidence_type",
            evidence_type=evidence_type,
            valid=sorted(MILESTONE_EVIDENCE_TYPES),
        )
    evidence_id = str(uuid.uuid4())
    t = now()
    with tx() as c:
        milestone = c.execute(
            "SELECT * FROM saga_milestones WHERE milestone_id = ?",
            (milestone_id,),
        ).fetchone()
        if not milestone:
            return err("milestone_not_found", milestone_id=milestone_id)
        c.execute(
            """
            INSERT INTO milestone_evidence(
                evidence_id, milestone_id, saga_id, evidence_type, content,
                schema_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence_id,
                milestone_id,
                milestone["saga_id"],
                evidence_type,
                content,
                schema_version,
                t,
            ),
        )
        row = c.execute(
            "SELECT * FROM milestone_evidence WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchone()
    data = ok(evidence=_evidence_to_dict(row))
    emit("record_milestone_evidence", data)
    return data


def start_saga_milestone(
    milestone_id: str,
    *,
    dispatch_intent_id: str | None = None,
) -> dict[str, Any]:
    t = now()
    with tx() as c:
        milestone = c.execute(
            "SELECT * FROM saga_milestones WHERE milestone_id = ?",
            (milestone_id,),
        ).fetchone()
        if not milestone:
            return err("not_found", milestone_id=milestone_id)
        if _status_of(milestone) is not MilestoneStatus.PENDING:
            return err(
                "not_pending",
                milestone_id=milestone_id,
                current_status=milestone["status"],
            )
        c.execute(
            """
            UPDATE saga_milestones
            SET status=?, dispatch_intent_id=?, started_at=?, updated_at=?
            WHERE milestone_id=?
            """,
            (
                MilestoneStatus.IN_PROGRESS.value,
                dispatch_intent_id or milestone["dispatch_intent_id"],
                t,
                t,
                milestone_id,
            ),
        )
        row = _reload_after_transition(c, milestone_id, t)
    data = ok(milestone=_milestone_to_dict(row))
    emit("start_saga_milestone", data)
    return data


def complete_saga_milestone(
    milestone_id: str,
    *,
    evidence_type: str | None = None,
    evidence_content: str | None = None,
    outcome: str = TerminalOutcome.MANUAL_RECOVERY_COMPLETION,
) -> dict[str, Any]:
    try:
        terminal_outcome = TerminalOutcome(outcome)
    except ValueError:
        return err(
            "invalid_outcome",
            outcome=outcome,
            valid=[item.value for item in TerminalOutcome],
        )
    if terminal_outcome not in {
        TerminalOutcome.AUTOMATED_COMPLETION,
        TerminalOutcome.MANUAL_RECOVERY_COMPLETION,
    }:
        return err("invalid_completion_outcome", outcome=terminal_outcome.value)
    t = now()
    with tx() as c:
        milestone = c.execute(
            "SELECT * FROM saga_milestones WHERE milestone_id = ?",
            (milestone_id,),
        ).fetchone()
        if not milestone:
            return err("not_found", milestone_id=milestone_id)
        if _status_of(milestone) is MilestoneStatus.COMPLETED and not milestone["outcome"]:
            c.execute(
                "UPDATE saga_milestones SET outcome=?, updated_at=? WHERE milestone_id=?",
                (terminal_outcome.value, t, milestone_id),
            )
            row = _reload_after_transition(c, milestone_id, t)
            data = ok(milestone=_milestone_to_dict(row), evidence=None, reclassified=True)
            emit("complete_saga_milestone", data)
            return data
        if _is_settled(_status_of(milestone)):
            return err(
                "already_terminal",
                milestone_id=milestone_id,
                current_status=milestone["status"],
            )
        evidence = None
        if evidence_type or evidence_content:
            if not evidence_type or evidence_content is None:
                return err("incomplete_evidence", message="Provide type and content together.")
            if evidence_type not in MILESTONE_EVIDENCE_TYPES:
                return err(
                    "invalid_evidence_type",
                    evidence_type=evidence_type,
                    valid=sorted(MILESTONE_EVIDENCE_TYPES),
                )
            evidence_id = str(uuid.uuid4())
            c.execute(
                """
                INSERT INTO milestone_evidence(
                    evidence_id, milestone_id, saga_id, evidence_type, content,
                    schema_version, created_at
                ) VALUES (?, ?, ?, ?, ?, 'milestone_evidence.v1', ?)
                """,
                (
                    evidence_id,
                    milestone_id,
                    milestone["saga_id"],
                    evidence_type,
                    evidence_content,
                    t,
                ),
            )
            evidence = _evidence_to_dict(
                c.execute(
                    "SELECT * FROM milestone_evidence WHERE evidence_id = ?",
                    (evidence_id,),
                ).fetchone()
            )
        reach_lifecycle_transition(
            LifecycleTransitionPoint.BEFORE_MILESTONE_COMPLETED,
            milestone_id=milestone_id,
            saga_id=str(milestone["saga_id"]),
            outcome=terminal_outcome.value,
            evidence_id=str(evidence["evidence_id"]) if evidence else None,
        )
        c.execute(
            """
            UPDATE saga_milestones
            SET status=?, outcome=?, completed_at=?, updated_at=?
            WHERE milestone_id=?
            """,
            (MilestoneStatus.COMPLETED.value, terminal_outcome.value, t, t, milestone_id),
        )
        row = _reload_after_transition(c, milestone_id, t)
    data = ok(milestone=_milestone_to_dict(row), evidence=evidence)
    emit("complete_saga_milestone", data)
    return data


def fail_saga_milestone(
    milestone_id: str,
    reason: str,
    *,
    status: str = MilestoneStatus.FAILED.value,
) -> dict[str, Any]:
    try:
        halted = MilestoneStatus(status)
    except ValueError:
        return err("invalid_status", status=status, valid=_halted_status_values())
    if _progress_of(halted) is not _Progress.HALTED:
        return err("invalid_status", status=status, valid=_halted_status_values())
    t = now()
    outcome = (
        TerminalOutcome.OPERATOR_CANCELED
        if halted is MilestoneStatus.CANCELED
        else classify_failure(reason)
    )
    with tx() as c:
        milestone = c.execute(
            "SELECT * FROM saga_milestones WHERE milestone_id = ?",
            (milestone_id,),
        ).fetchone()
        if not milestone:
            return err("not_found", milestone_id=milestone_id)
        c.execute(
            """
            UPDATE saga_milestones
            SET status=?, outcome=?, updated_at=?,
                completed_at=CASE WHEN ? = 1 THEN ? ELSE completed_at END
            WHERE milestone_id=?
            """,
            (halted.value, outcome.value, t, int(_ends_the_attempt(halted)), t, milestone_id),
        )
        row = _reload_after_transition(c, milestone_id, t)
    data = ok(milestone=_milestone_to_dict(row), reason=reason)
    emit("fail_saga_milestone", data)
    return data


def retry_saga_milestone(milestone_id: str, reason: str) -> dict[str, Any]:
    """Explicitly reopen one terminal milestone while preserving its forensic history."""
    t = now()
    with tx() as c:
        milestone = c.execute(
            "SELECT * FROM saga_milestones WHERE milestone_id = ?",
            (milestone_id,),
        ).fetchone()
        if not milestone:
            return err("not_found", milestone_id=milestone_id)
        if _progress_of(_status_of(milestone)) is not _Progress.HALTED:
            return err(
                "not_retryable",
                milestone_id=milestone_id,
                current_status=milestone["status"],
            )
        checkpoint = None
        if milestone["dispatch_intent_id"]:
            checkpoint = c.execute(
                """
                SELECT checkpoint_id, status, worktree_path, source_repo_path,
                       base_head_sha, transcript_artifact_id, patch_artifact_id,
                       git_status_artifact_id, test_summary_artifact_id, created_at
                FROM agent_execution_checkpoints
                WHERE intent_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (milestone["dispatch_intent_id"],),
            ).fetchone()
        if checkpoint is not None:
            checkpoint_summary = rowdict(checkpoint)
            checkpoint_summary["created_at"] = iso(checkpoint_summary["created_at"])
            return ok(
                retried=False,
                status="checkpoint_recovery_required",
                milestone=_milestone_to_dict(milestone),
                checkpoint=checkpoint_summary,
                dispatch_intent_id=milestone["dispatch_intent_id"],
                recovery_path="execution_checkpoint",
                next_step="pi /ledger",
                message=(
                    "Generic milestone retry is denied because the previous execution "
                    "has a durable checkpoint. Continue through checkpoint review or "
                    "checkpoint-bound recovery so preserved work and context are reused."
                ),
            )
        c.execute(
            """
            UPDATE saga_milestones
            SET status=?, dispatch_intent_id=NULL, started_at=NULL,
                completed_at=NULL, outcome=NULL, updated_at=?
            WHERE milestone_id=?
            """,
            (MilestoneStatus.PENDING.value, t, milestone_id),
        )
        row = _reload_after_transition(c, milestone_id, t)
    data = ok(milestone=_milestone_to_dict(row), reason=reason)
    emit("retry_saga_milestone", data)
    return data


def _milestone_is_dependency_ready(
    c: ConnectionLike,
    depends_on: list[str],
) -> bool:
    for dependency_id in depends_on:
        dependency = c.execute(
            "SELECT status FROM saga_milestones WHERE milestone_id = ?",
            (dependency_id,),
        ).fetchone()
        if not dependency or _status_of(dependency) is not MilestoneStatus.COMPLETED:
            return False
    return True


def _milestone_is_approval_ready(c: ConnectionLike, milestone: dict[str, Any]) -> bool:
    if not bool(milestone["approval_required"]):
        return True
    approval = c.execute(
        """
        SELECT status FROM approval_requests
        WHERE saga_id = ?
          AND request_type = 'GENERAL'
          AND status = 'APPROVED'
          AND payload_json LIKE ?
        LIMIT 1
        """,
        (milestone["saga_id"], f"%{milestone['milestone_id']}%"),
    ).fetchone()
    return bool(approval)


# The token that marks a dispatch source as naming a saga milestone.
#
# The WorkUnit engine spells the same eight characters inside its DBOS workflow
# IDs (work_units/root_workflow.py). That is a coincidence, not shared knowledge:
# one is a routing key in a ledger column read by the dispatcher, the other is a
# segment of a durable execution identity. They are deliberately written twice so
# that changing either cannot silently change the other, and they must not be
# unified into one constant.
SAGA_MILESTONE_SOURCE_MARKER = ":milestone:"


@dataclass(frozen=True)
class ClaimedMilestone:
    """The source claims this saga milestone.

    A claim, not a fact: whether the id resolves to a row is a lookup the caller
    performs. The parser only reports what the string says.
    """

    milestone_id: str


@dataclass(frozen=True)
class NoMilestoneReference:
    """The source makes no milestone claim.

    Covers both a missing source and a source without the marker, which mean the
    same thing to every caller: leave saga_milestones alone. A WorkUnit dispatch
    source lands here, which is what keeps the WorkUnit engine's intents out of
    the legacy milestone lane.
    """


@dataclass(frozen=True)
class MalformedMilestoneReference:
    """The source carries the marker and no identifier after it.

    Nothing legitimately builds this, so it means a source was assembled by hand
    or by a caller that bypassed `build_approved_gawd_milestone_dispatch_source`.
    It is reported rather than raised because the string arrives from a persisted
    column: one poisoned row must not take down every dispatcher poll.
    """

    source: str


MilestoneReference = ClaimedMilestone | NoMilestoneReference | MalformedMilestoneReference


def parse_milestone_reference(source: str | None) -> MilestoneReference:
    """What a dispatch source says about a saga milestone.

    Returning a sum rather than `str | None` is the point. The old signature
    collapsed "this is not a milestone dispatch" and "this is a malformed
    milestone dispatch" onto the same `None`, so a source that claimed a link and
    failed to carry one was indistinguishable from a source that never claimed
    one, and the caller skipped both in silence.
    """

    if not source or SAGA_MILESTONE_SOURCE_MARKER not in source:
        return NoMilestoneReference()
    milestone_id = source.split(SAGA_MILESTONE_SOURCE_MARKER, 1)[1].strip()
    if not milestone_id:
        return MalformedMilestoneReference(source=source)
    return ClaimedMilestone(milestone_id=milestone_id)


def _record_milestone_summary_evidence(
    c: ConnectionLike,
    *,
    milestone: dict[str, Any],
    intent: dict[str, Any],
    dispatch_status: str,
    result: str | None,
    error: str | None,
    created_at: float,
) -> dict[str, Any]:
    evidence_id = str(uuid.uuid4())
    payload = {
        "schema_version": "dispatch_milestone_evidence.v1",
        "intent_id": intent["intent_id"],
        "dispatch_status": dispatch_status,
        "result": result,
        "error": error,
    }
    c.execute(
        """
        INSERT INTO milestone_evidence(
            evidence_id, milestone_id, saga_id, evidence_type, content,
            schema_version, created_at
        ) VALUES (?, ?, ?, 'summary', ?, 'dispatch_milestone_evidence.v1', ?)
        """,
        (
            evidence_id,
            milestone["milestone_id"],
            milestone["saga_id"],
            json.dumps(payload, sort_keys=True),
            created_at,
        ),
    )
    return {
        "evidence_id": evidence_id,
        "milestone_id": milestone["milestone_id"],
        "evidence_type": "summary",
    }


def _complete_saga_if_all_milestones_completed(
    c: ConnectionLike,
    *,
    saga_id: str,
    completed_at: float,
) -> bool:
    remaining = c.execute(
        """
        SELECT COUNT(*) AS n FROM saga_milestones
        WHERE saga_id = ? AND status != ?
        """,
        (saga_id, MilestoneStatus.COMPLETED.value),
    ).fetchone()["n"]
    if remaining:
        return False
    c.execute(
        """
        UPDATE sagas
        SET status=?, updated_at=?, completed_at=?
        WHERE saga_id=? AND status != ?
        """,
        (
            SagaStatus.COMPLETED.value,
            completed_at,
            completed_at,
            saga_id,
            SagaStatus.COMPLETED.value,
        ),
    )
    return True


def record_dispatch_outcome_on_milestone(
    c: ConnectionLike,
    *,
    intent: dict[str, Any],
    dispatch_status: str,
    result: str | None,
    error: str | None,
    completed_at: float,
) -> dict[str, Any] | None:
    reference = parse_milestone_reference(intent["source"])
    if not isinstance(reference, ClaimedMilestone):
        return None
    milestone_id = reference.milestone_id
    milestone = c.execute(
        "SELECT * FROM saga_milestones WHERE milestone_id = ?",
        (milestone_id,),
    ).fetchone()
    if not milestone:
        return None
    if _is_settled(_status_of(milestone)):
        return {
            "milestone_id": milestone_id,
            "status": milestone["status"],
            "already_terminal": True,
        }
    if dispatch_status == DispatchTerminalStatus.DONE:
        new_status = MilestoneStatus.COMPLETED
        outcome = intent["outcome"] or TerminalOutcome.AUTOMATED_COMPLETION.value
    elif dispatch_status == DispatchTerminalStatus.FAILED:
        new_status = MilestoneStatus.FAILED
        outcome = intent["outcome"] or classify_failure(error).value
    else:
        return None
    c.execute(
        """
        UPDATE saga_milestones
        SET status=?, outcome=?, dispatch_intent_id=?, completed_at=?, updated_at=?
        WHERE milestone_id=?
        """,
        (
            new_status.value,
            outcome,
            intent["intent_id"],
            completed_at,
            completed_at,
            milestone_id,
        ),
    )
    evidence = _record_milestone_summary_evidence(
        c,
        milestone=milestone,
        intent=intent,
        dispatch_status=dispatch_status,
        result=result,
        error=error,
        created_at=completed_at,
    )
    saga_completed = False
    if new_status is MilestoneStatus.COMPLETED:
        reach_lifecycle_transition(
            LifecycleTransitionPoint.BEFORE_MILESTONE_COMPLETED,
            milestone_id=milestone_id,
            saga_id=str(milestone["saga_id"]),
            outcome=str(outcome),
            evidence_id=str(evidence["evidence_id"]),
        )
        saga_completed = _complete_saga_if_all_milestones_completed(
            c,
            saga_id=milestone["saga_id"],
            completed_at=completed_at,
        )
    return {
        "milestone_id": milestone_id,
        "status": new_status.value,
        "evidence": evidence,
        "saga_completed": saga_completed,
    }


def next_ready_saga_milestone(saga_id: str) -> dict[str, Any]:
    """Return the oldest runnable PENDING milestone, or explain blockers."""
    with connect() as c:
        saga = c.execute("SELECT * FROM sagas WHERE saga_id = ?", (saga_id,)).fetchone()
        if not saga:
            return err("saga_not_found", saga_id=saga_id)
        rows = c.execute(
            """
            SELECT * FROM saga_milestones
            WHERE saga_id = ? AND status = ?
            ORDER BY sequence, created_at
            """,
            (saga_id, MilestoneStatus.PENDING.value),
        ).fetchall()
        blocked: list[dict[str, Any]] = []
        for row in rows:
            depends_on = decode_json_array(row["depends_on_json"])
            dependency_ready = _milestone_is_dependency_ready(c, depends_on)
            approval_ready = _milestone_is_approval_ready(c, row)
            if dependency_ready and approval_ready:
                return ok(milestone=_milestone_to_dict(row), blocked=blocked)
            blocked.append(
                {
                    "milestone_id": row["milestone_id"],
                    "dependency_ready": dependency_ready,
                    "approval_ready": approval_ready,
                    "depends_on": depends_on,
                }
            )
    return ok(milestone=None, blocked=blocked)


def reconcile_saga_milestones(saga_id: str) -> dict[str, Any]:
    """Repair milestone terminal state from linked terminal dispatch intents."""
    t = now()
    reconciled: list[dict[str, Any]] = []
    with tx() as c:
        saga = c.execute("SELECT * FROM sagas WHERE saga_id = ?", (saga_id,)).fetchone()
        if not saga:
            return err("saga_not_found", saga_id=saga_id)
        milestones = c.execute(
            f"""
            SELECT * FROM saga_milestones
            WHERE saga_id = ? AND status NOT IN ({_TERMINAL_PLACEHOLDERS})
            ORDER BY sequence, created_at
            """,
            (saga_id, *_MILESTONE_TERMINAL_VALUES),
        ).fetchall()
        for milestone in milestones:
            if milestone["dispatch_intent_id"]:
                intent = c.execute(
                    f"""
                    SELECT * FROM dispatch_intents
                    WHERE intent_id = ? AND status IN ({_DISPATCH_TERMINAL_PLACEHOLDERS})
                    ORDER BY completed_at DESC
                    LIMIT 1
                    """,
                    (milestone["dispatch_intent_id"], *_DISPATCH_TERMINAL_VALUES),
                ).fetchone()
            else:
                intent = c.execute(
                    f"""
                    SELECT * FROM dispatch_intents
                    WHERE source LIKE ? AND status IN ({_DISPATCH_TERMINAL_PLACEHOLDERS})
                    ORDER BY completed_at DESC
                    LIMIT 1
                    """,
                    (
                        f"%{SAGA_MILESTONE_SOURCE_MARKER}{milestone['milestone_id']}",
                        *_DISPATCH_TERMINAL_VALUES,
                    ),
                ).fetchone()
            if not intent:
                continue
            applied = record_dispatch_outcome_on_milestone(
                c,
                intent=intent,
                dispatch_status=intent["status"],
                result=intent["result"],
                error=intent["error"],
                completed_at=t,
            )
            if applied is not None:
                reconciled.append(applied)
    ready = next_ready_saga_milestone(saga_id)
    data = ok(
        saga_id=saga_id,
        reconciled=reconciled,
        next_ready_milestone=ready.get("milestone"),
        blocked_milestones=ready.get("blocked", []),
    )
    emit("reconcile_saga_milestones", data)
    return data


# ---------------------------------------------------------------------------
# Layer 2: Pow-wow
# ---------------------------------------------------------------------------
