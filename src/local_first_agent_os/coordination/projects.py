# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""GAWD documents and saga lifecycle operations."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from ..contracts import GawdDocStatus, SagaStage, SagaStatus
from .store import (
    AMBIGUITY_THRESHOLDS,
    STAGNATION_MIN_DELTA_RATIO,
    connect,
    decode_json_array,
    decode_json_object,
    emit,
    err,
    iso,
    now,
    ok,
    optional_session,
    rowdict,
    tx,
)


def create_gawd_doc(
    goal: str,
    constraints: list[str] | None = None,
    success_criteria: list[str] | None = None,
    unresolved_questions: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    task_graph: dict[str, Any] | None = None,
    saga_id: str | None = None,
) -> dict[str, Any]:
    """Create a new GAWD doc in DRAFT state.

    GAWD_DOC_V1 is immutable once approved. Changes create V2, not silent edits.
    """
    doc_id = str(uuid.uuid4())
    t = now()
    with tx() as c:
        # determine version if superseding existing doc for same saga
        version = 1
        if saga_id:
            r = c.execute(
                "SELECT MAX(version) AS v FROM gawd_docs WHERE saga_id = ?",
                (saga_id,),
            ).fetchone()
            if r and r["v"]:
                version = r["v"] + 1

        c.execute(
            f"""
            INSERT INTO gawd_docs(
                gawd_doc_id, saga_id, version, goal,
                constraints_json, success_criteria_json,
                unresolved_questions_json, acceptance_criteria_json,
                task_graph_json, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{GawdDocStatus.DRAFT}', ?)
            """,
            (
                doc_id,
                saga_id,
                version,
                goal,
                json.dumps(constraints or []),
                json.dumps(success_criteria or []),
                json.dumps(unresolved_questions or []),
                json.dumps(acceptance_criteria or []),
                json.dumps(task_graph or {}),
                t,
            ),
        )

    data = ok(
        gawd_doc_id=doc_id,
        saga_id=saga_id,
        version=version,
        goal=goal,
        status="DRAFT",
        created_at=iso(t),
    )
    emit("create_gawd_doc", data)
    return data


def approve_gawd_doc(gawd_doc_id: str) -> dict[str, Any]:
    """Approve a DRAFT GAWD doc (makes it immutable)."""
    t = now()
    with tx() as c:
        r = c.execute("SELECT * FROM gawd_docs WHERE gawd_doc_id = ?", (gawd_doc_id,)).fetchone()
        if not r:
            return err("not_found", gawd_doc_id=gawd_doc_id)
        if GawdDocStatus(str(r["status"])) is not GawdDocStatus.DRAFT:
            return err("not_draft", gawd_doc_id=gawd_doc_id, current_status=r["status"])
        c.execute(
            f"UPDATE gawd_docs SET status = '{GawdDocStatus.APPROVED}', "
            "approved_at = ? WHERE gawd_doc_id = ?",
            (t, gawd_doc_id),
        )
    data = ok(gawd_doc_id=gawd_doc_id, status="APPROVED", approved_at=iso(t))
    emit("approve_gawd_doc", data)
    return data


def supersede_gawd_doc(
    old_gawd_doc_id: str,
    goal: str,
    constraints: list[str] | None = None,
    success_criteria: list[str] | None = None,
    unresolved_questions: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    task_graph: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create V(n+1) that supersedes an approved GAWD doc.

    The old doc is marked SUPERSEDED; the new one starts as DRAFT.
    """
    with tx() as c:
        old = c.execute(
            "SELECT * FROM gawd_docs WHERE gawd_doc_id = ?",
            (old_gawd_doc_id,),
        ).fetchone()
        if not old:
            return err("not_found", gawd_doc_id=old_gawd_doc_id)
        if GawdDocStatus(str(old["status"])) is not GawdDocStatus.APPROVED:
            return err(
                "cannot_supersede",
                message="Only APPROVED docs can be superseded",
                status=old["status"],
            )

    result = create_gawd_doc(
        goal=goal,
        constraints=constraints,
        success_criteria=success_criteria,
        unresolved_questions=unresolved_questions,
        acceptance_criteria=acceptance_criteria,
        task_graph=task_graph,
        saga_id=old["saga_id"],
    )
    if not result.get("ok"):
        return result

    new_id = result["gawd_doc_id"]
    t = now()
    with tx() as c:
        c.execute(
            f"UPDATE gawd_docs SET status = '{GawdDocStatus.SUPERSEDED}', "
            "superseded_by = ? WHERE gawd_doc_id = ?",
            (new_id, old_gawd_doc_id),
        )
    data = ok(
        new_gawd_doc_id=new_id,
        superseded_gawd_doc_id=old_gawd_doc_id,
        updated_at=iso(t),
    )
    emit("supersede_gawd_doc", data)
    return data


def get_gawd_doc(gawd_doc_id: str) -> dict[str, Any]:
    """Fetch a single GAWD doc by ID."""
    with connect() as c:
        r = c.execute("SELECT * FROM gawd_docs WHERE gawd_doc_id = ?", (gawd_doc_id,)).fetchone()
    if not r:
        return err("not_found", gawd_doc_id=gawd_doc_id)
    d = rowdict(r)
    for key in (
        "constraints_json",
        "success_criteria_json",
        "unresolved_questions_json",
        "acceptance_criteria_json",
    ):
        d[key.replace("_json", "")] = decode_json_array(d.pop(key, None))
    d["task_graph"] = decode_json_object(d.pop("task_graph_json", None))
    if d.get("approved_at"):
        d["approved_at"] = iso(d["approved_at"])
    d["created_at"] = iso(d["created_at"])
    return ok(gawd_doc=d)


def attach_gawd_doc_to_saga(saga_id: str, gawd_doc_id: str) -> dict[str, Any]:
    """Set the active GAWD doc for a saga.

    This keeps the saga pointer explicit while preserving every draft/version as
    its own immutable ledger row.
    """
    t = now()
    with tx() as c:
        saga = c.execute("SELECT * FROM sagas WHERE saga_id = ?", (saga_id,)).fetchone()
        if not saga:
            return err("saga_not_found", saga_id=saga_id)
        doc = c.execute(
            "SELECT * FROM gawd_docs WHERE gawd_doc_id = ?",
            (gawd_doc_id,),
        ).fetchone()
        if not doc:
            return err("gawd_doc_not_found", gawd_doc_id=gawd_doc_id)
        if doc["saga_id"] not in (None, saga_id):
            return err(
                "gawd_doc_saga_mismatch",
                saga_id=saga_id,
                gawd_doc_id=gawd_doc_id,
                gawd_doc_saga_id=doc["saga_id"],
            )
        c.execute(
            "UPDATE gawd_docs SET saga_id = ? WHERE gawd_doc_id = ?",
            (saga_id, gawd_doc_id),
        )
        c.execute(
            "UPDATE sagas SET gawd_doc_id = ?, updated_at = ? WHERE saga_id = ?",
            (gawd_doc_id, t, saga_id),
        )
    data = ok(saga_id=saga_id, gawd_doc_id=gawd_doc_id, updated_at=iso(t))
    emit("attach_gawd_doc_to_saga", data)
    return data


def check_ambiguity(gawd_doc_id: str) -> dict[str, Any]:
    """Heuristic ambiguity gate.

    Returns clarity scores and whether each threshold passes.
    Scores are heuristic (length / completeness) — call an LLM for
    semantic scoring if you need higher fidelity.

    Thresholds (Ouroboros-inspired):
      goal_clarity          >= 0.85
      constraints_clarity   >= 0.80
      success_criteria_clarity >= 0.80
      unresolved_critical   == 0
    """
    with connect() as c:
        r = c.execute("SELECT * FROM gawd_docs WHERE gawd_doc_id = ?", (gawd_doc_id,)).fetchone()
    if not r:
        return err("not_found", gawd_doc_id=gawd_doc_id)

    goal: str = r["goal"] or ""
    constraints = decode_json_array(r["constraints_json"])
    success_criteria = decode_json_array(r["success_criteria_json"])
    unresolved = decode_json_array(r["unresolved_questions_json"])

    # Heuristic: goal completeness by character length bands
    goal_len = len(goal.strip())
    goal_clarity = min(1.0, goal_len / 200.0) if goal_len > 0 else 0.0

    # Constraints: fraction of entries that are non-trivial strings
    if constraints:
        non_trivial = sum(1 for c in constraints if isinstance(c, str) and len(c.strip()) >= 10)
        constraints_clarity = non_trivial / len(constraints)
    else:
        constraints_clarity = 0.0

    # Success criteria: fraction that look measurable (contain a number or comparison word)
    import re

    measurable_re = re.compile(r"\d+|>=|<=|must|shall|will|pass|fail|complete", re.I)
    if success_criteria:
        measurable = sum(1 for s in success_criteria if measurable_re.search(str(s)))
        success_criteria_clarity = measurable / len(success_criteria)
    else:
        success_criteria_clarity = 0.0

    unresolved_critical = len([q for q in unresolved if isinstance(q, str) and q.strip()])

    scores = {
        "goal_clarity": round(goal_clarity, 3),
        "constraints_clarity": round(constraints_clarity, 3),
        "success_criteria_clarity": round(success_criteria_clarity, 3),
        "unresolved_critical": unresolved_critical,
    }
    thresholds = AMBIGUITY_THRESHOLDS
    passes = {
        "goal_clarity": scores["goal_clarity"] >= thresholds["goal_clarity"],
        "constraints_clarity": scores["constraints_clarity"] >= thresholds["constraints_clarity"],
        "success_criteria_clarity": (
            scores["success_criteria_clarity"] >= thresholds["success_criteria_clarity"]
        ),
        "unresolved_critical": (
            scores["unresolved_critical"] <= thresholds["max_unresolved_critical"]
        ),
    }
    ready = all(passes.values())
    return ok(
        gawd_doc_id=gawd_doc_id,
        scores=scores,
        thresholds=thresholds,
        passes=passes,
        ready_to_execute=ready,
        message=(
            "GAWD doc passes ambiguity gate"
            if ready
            else "Resolve failing checks before proceeding"
        ),
    )


# ---------------------------------------------------------------------------
# Layer 2: Saga
# ---------------------------------------------------------------------------


def saga_content_digest(raw_text: str) -> str:
    """Digest the draft text that a saga was created from.

    Content, not path and not goal: the same draft ingested from two paths is
    one project, and two different drafts can easily share a goal prefix. Five
    sagas once shared the prefix "New project intake: Two live prospects exist"
    because the goal was the only thing anyone compared.
    """

    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()


def create_saga(
    goal: str,
    budget_tokens: int = 1_000_000,
    budget_seconds: int = 86400,
    gawd_doc_id: str | None = None,
    content_digest: str | None = None,
) -> dict[str, Any]:
    """Create a new saga, or replay the existing one for the same content.

    Dedupe lives here rather than in intake because every caller would
    otherwise have to remember to check first, and one that forgets creates the
    duplicate anyway. With a unique index behind it, a second saga for one
    draft is unrepresentable rather than merely unlikely.

    A replay returns the existing saga with ``replayed=True``. Callers that
    pass no digest keep the old behavior exactly.
    """

    t = now()
    if content_digest:
        existing = _saga_by_content_digest(content_digest)
        if existing is not None:
            data = ok(replayed=True, **existing)
            emit("create_saga", data)
            return data

    saga_id = str(uuid.uuid4())
    with tx() as c:
        c.execute(
            f"""
            INSERT INTO sagas(saga_id, goal, gawd_doc_id, current_stage, status,
                              budget_tokens, budget_seconds, tokens_used,
                              created_at, updated_at, content_digest)
            VALUES (?, ?, ?, '{SagaStage.IDEA_INTAKE}', '{SagaStatus.PLANNING}', ?, ?, 0, ?, ?, ?)
            """,
            (saga_id, goal, gawd_doc_id, budget_tokens, budget_seconds, t, t, content_digest),
        )
    data = ok(
        saga_id=saga_id,
        goal=goal,
        gawd_doc_id=gawd_doc_id,
        current_stage="IDEA_INTAKE",
        status="PLANNING",
        budget_tokens=budget_tokens,
        budget_seconds=budget_seconds,
        created_at=iso(t),
        replayed=False,
    )
    emit("create_saga", data)
    return data


def _saga_by_content_digest(content_digest: str) -> dict[str, Any] | None:
    with connect() as c:
        row = c.execute(
            "SELECT saga_id, goal, gawd_doc_id, current_stage, status, budget_tokens, "
            "budget_seconds, created_at FROM sagas WHERE content_digest = ?",
            (content_digest,),
        ).fetchone()
    if row is None:
        return None
    existing = dict(row)
    existing["created_at"] = iso(float(existing["created_at"]))
    return existing


def get_saga(saga_id: str) -> dict[str, Any]:
    """Fetch a saga by ID including pow-wow count and task summary."""
    with connect() as c:
        r = c.execute("SELECT * FROM sagas WHERE saga_id = ?", (saga_id,)).fetchone()
        if not r:
            return err("not_found", saga_id=saga_id)
        pw_count = c.execute(
            "SELECT COUNT(*) AS n FROM pow_wows WHERE saga_id = ?", (saga_id,)
        ).fetchone()["n"]
        task_counts = c.execute(
            """
            SELECT status, COUNT(*) AS n FROM saga_tasks
            WHERE saga_id = ?
            GROUP BY status
            """,
            (saga_id,),
        ).fetchall()
        milestone_counts = c.execute(
            """
            SELECT status, COUNT(*) AS n FROM saga_milestones
            WHERE saga_id = ?
            GROUP BY status
            """,
            (saga_id,),
        ).fetchall()
    d = rowdict(r)
    d["created_at"] = iso(d["created_at"])
    d["updated_at"] = iso(d["updated_at"])
    if d.get("completed_at"):
        d["completed_at"] = iso(d["completed_at"])
    d["pow_wow_count"] = pw_count
    d["task_summary"] = {row["status"]: row["n"] for row in task_counts}
    d["milestone_summary"] = {row["status"]: row["n"] for row in milestone_counts}
    return ok(saga=d)


def list_sagas(status_filter: str | None = None) -> dict[str, Any]:
    """List sagas, optionally filtered by status."""
    with connect() as c:
        if status_filter:
            rows = c.execute(
                "SELECT * FROM sagas WHERE status = ? ORDER BY created_at DESC",
                (status_filter,),
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM sagas ORDER BY created_at DESC").fetchall()
    sagas = []
    for r in rows:
        d = rowdict(r)
        d["created_at"] = iso(d["created_at"])
        d["updated_at"] = iso(d["updated_at"])
        if d.get("completed_at"):
            d["completed_at"] = iso(d["completed_at"])
        sagas.append(d)
    return ok(sagas=sagas)


def complete_saga(saga_id: str, outcome: str, session_id: str | None = None) -> dict[str, Any]:
    """Mark a saga as completed or failed."""
    valid = {"COMPLETED", "FAILED", "STAGNATED"}
    if outcome not in valid:
        return err("invalid_outcome", outcome=outcome, valid=sorted(valid))
    t = now()
    with tx() as c:
        s = optional_session(c, session_id)
        cur = c.execute(
            "UPDATE sagas SET status = ?, updated_at = ?, completed_at = ? WHERE saga_id = ?",
            (outcome, t, t, saga_id),
        )
        if cur.rowcount != 1:
            return err("not_found", saga_id=saga_id)
    agent_name = s["agent_name"] if s else None
    data = ok(saga_id=saga_id, status=outcome, completed_at=iso(t), completed_by=agent_name)
    emit("complete_saga", data)
    return data


def check_stagnation(saga_id: str) -> dict[str, Any]:
    """Detect stagnation: two consecutive pow-wow cycles with < 10% new artifact delta.

    Returns stagnated=True and a recommendation if the saga is spinning.
    """
    with connect() as c:
        pows = c.execute(
            f"""
            SELECT pow_wow_id, cycle_count FROM pow_wows
            WHERE saga_id = ? AND status = '{SagaStatus.COMPLETED}'
            ORDER BY completed_at DESC
            LIMIT 4
            """,
            (saga_id,),
        ).fetchall()
        if len(pows) < 2:
            return ok(
                saga_id=saga_id,
                stagnated=False,
                reason="Insufficient completed pow-wow history",
                cycles_checked=len(pows),
            )

        # Sum artifact sizes per pow-wow (last 2)
        sizes: list[int] = []
        for pw in pows[:2]:
            r = c.execute(
                (
                    "SELECT COALESCE(SUM(size_bytes), 0) AS total FROM task_artifacts "
                    "WHERE pow_wow_id = ?"
                ),
                (pw["pow_wow_id"],),
            ).fetchone()
            sizes.append(r["total"])

    prev_size, curr_size = sizes[1], sizes[0]
    if prev_size == 0:
        return ok(saga_id=saga_id, stagnated=False, reason="No baseline to compare against")

    delta_ratio = abs(curr_size - prev_size) / max(prev_size, 1)
    stagnated = delta_ratio < STAGNATION_MIN_DELTA_RATIO

    return ok(
        saga_id=saga_id,
        stagnated=stagnated,
        delta_ratio=round(delta_ratio, 4),
        threshold=STAGNATION_MIN_DELTA_RATIO,
        reason=(
            f"Artifact delta {delta_ratio:.1%} < {STAGNATION_MIN_DELTA_RATIO:.0%} threshold"
            if stagnated
            else f"Artifact delta {delta_ratio:.1%} is healthy"
        ),
        recommendation=(
            "Escalate to user or Staff agent — loop is producing diminishing returns"
            if stagnated
            else None
        ),
        pow_wows_checked=[p["pow_wow_id"] for p in pows[:2]],
    )


# ---------------------------------------------------------------------------
# Layer 2: Saga milestones
# ---------------------------------------------------------------------------
