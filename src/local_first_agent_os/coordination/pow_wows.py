# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pow-wow, task, artifact, delegation, permission, and evaluation state."""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from typing import Any, Literal

from local_first_agent_os.constants import DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS

from ..capabilities import UnknownCapability, parse_capability
from ..contracts import ApprovalStatus, PowWowStatus, TaskStatus
from .store import (
    connect,
    decode_json_array,
    emit,
    err,
    iso,
    now,
    ok,
    optional_session,
    rowdict,
    tx,
)


def create_pow_wow(
    saga_id: str,
    stage: str,
    goal: str,
    exit_criteria: str,
    budget_tokens: int = 100_000,
    required_outputs: list[str] | None = None,
    input_artifacts: list[str] | None = None,
    allowed_tools: list[str] | None = None,
) -> dict[str, Any]:
    """Create a new pow-wow stage within a saga."""
    pw_id = str(uuid.uuid4())
    t = now()
    with tx() as c:
        r = c.execute("SELECT saga_id FROM sagas WHERE saga_id = ?", (saga_id,)).fetchone()
        if not r:
            return err("saga_not_found", saga_id=saga_id)
        c.execute(
            f"""
            INSERT INTO pow_wows(
                pow_wow_id, saga_id, stage, goal,
                input_artifacts_json, allowed_tools_json,
                budget_tokens, exit_criteria, required_outputs_json,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{PowWowStatus.FORMING}', ?, ?)
            """,
            (
                pw_id,
                saga_id,
                stage,
                goal,
                json.dumps(input_artifacts or []),
                json.dumps(allowed_tools or []),
                budget_tokens,
                exit_criteria,
                json.dumps(required_outputs or []),
                t,
                t,
            ),
        )
    data = ok(
        pow_wow_id=pw_id,
        saga_id=saga_id,
        stage=stage,
        goal=goal,
        exit_criteria=exit_criteria,
        status="FORMING",
        created_at=iso(t),
    )
    emit("create_pow_wow", data)
    return data


def get_pow_wow(pow_wow_id: str) -> dict[str, Any]:
    """Fetch a pow-wow by ID including its agent roster and task summary."""
    with connect() as c:
        r = c.execute("SELECT * FROM pow_wows WHERE pow_wow_id = ?", (pow_wow_id,)).fetchone()
        if not r:
            return err("not_found", pow_wow_id=pow_wow_id)
        agents = c.execute(
            "SELECT * FROM pow_wow_agents WHERE pow_wow_id = ? ORDER BY joined_at",
            (pow_wow_id,),
        ).fetchall()
        task_counts = c.execute(
            "SELECT status, COUNT(*) AS n FROM saga_tasks WHERE pow_wow_id = ? GROUP BY status",
            (pow_wow_id,),
        ).fetchall()
    d = rowdict(r)
    for k in ("input_artifacts_json", "allowed_tools_json", "required_outputs_json"):
        d[k.replace("_json", "")] = decode_json_array(d.pop(k, None))
    d["created_at"] = iso(d["created_at"])
    d["updated_at"] = iso(d["updated_at"])
    if d.get("completed_at"):
        d["completed_at"] = iso(d["completed_at"])
    d["agents"] = [
        {
            **rowdict(a),
            "joined_at": iso(a["joined_at"]),
            "allowed_tools": decode_json_array(a["allowed_tools_json"]),
        }
        for a in agents
    ]
    d["task_summary"] = {row["status"]: row["n"] for row in task_counts}
    return ok(pow_wow=d)


def list_pow_wows(saga_id: str, status_filter: str | None = None) -> dict[str, Any]:
    """List pow-wows for a saga."""
    with connect() as c:
        if status_filter:
            rows = c.execute(
                "SELECT * FROM pow_wows WHERE saga_id = ? AND status = ? ORDER BY created_at",
                (saga_id, status_filter),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM pow_wows WHERE saga_id = ? ORDER BY created_at",
                (saga_id,),
            ).fetchall()
    result = []
    for r in rows:
        d = rowdict(r)
        d["created_at"] = iso(d["created_at"])
        d["updated_at"] = iso(d["updated_at"])
        if d.get("completed_at"):
            d["completed_at"] = iso(d["completed_at"])
        result.append(d)
    return ok(pow_wows=result)


def join_pow_wow(
    pow_wow_id: str,
    role: str,
    allowed_tools: list[str] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Enroll this agent in a pow-wow with a specific role.

    Role does NOT grant tool permissions — those must be requested explicitly.
    """
    t = now()
    with tx() as c:
        pw = c.execute("SELECT * FROM pow_wows WHERE pow_wow_id = ?", (pow_wow_id,)).fetchone()
        if not pw:
            return err("pow_wow_not_found", pow_wow_id=pow_wow_id)
        if PowWowStatus(str(pw["status"])) not in (PowWowStatus.FORMING, PowWowStatus.ACTIVE):
            return err("pow_wow_not_accepting_agents", status=pw["status"])
        s = optional_session(c, session_id)
        agent_name = s["agent_name"] if s else (session_id or "unknown")
        sid = s["session_id"] if s else None
        c.execute(
            f"""
            INSERT INTO pow_wow_agents(pow_wow_id, session_id, agent_name, role,
                                       allowed_tools_json, status, joined_at)
            VALUES (?, ?, ?, ?, ?, '{PowWowStatus.ACTIVE}', ?)
            ON CONFLICT(pow_wow_id, agent_name) DO UPDATE SET
                role = excluded.role,
                allowed_tools_json = excluded.allowed_tools_json,
                status = '{PowWowStatus.ACTIVE}',
                joined_at = excluded.joined_at
            """,
            (pow_wow_id, sid, agent_name, role, json.dumps(allowed_tools or []), t),
        )
        # Transition pow-wow to ACTIVE once at least one agent joins
        if PowWowStatus(str(pw["status"])) is PowWowStatus.FORMING:
            c.execute(
                f"UPDATE pow_wows SET status = '{PowWowStatus.ACTIVE}', "
                "updated_at = ? WHERE pow_wow_id = ?",
                (t, pow_wow_id),
            )
    data = ok(
        pow_wow_id=pow_wow_id,
        agent_name=agent_name,
        role=role,
        allowed_tools=allowed_tools or [],
        joined_at=iso(t),
    )
    emit("join_pow_wow", data)
    return data


def complete_pow_wow(
    pow_wow_id: str,
    output_summary: str,
    status: str = "COMPLETED",
    session_id: str | None = None,
) -> dict[str, Any]:
    """Mark a pow-wow as completed with an output summary."""
    allowed_statuses = {"COMPLETED", "VERIFICATION_FAILED", "FAILED", "BLOCKED"}
    if status not in allowed_statuses:
        return err("invalid_status", status=status, allowed_statuses=sorted(allowed_statuses))
    t = now()
    with tx() as c:
        s = optional_session(c, session_id)
        pw = c.execute("SELECT * FROM pow_wows WHERE pow_wow_id = ?", (pow_wow_id,)).fetchone()
        if not pw:
            return err("not_found", pow_wow_id=pow_wow_id)
        c.execute(
            """
            UPDATE pow_wows
            SET status = ?, output_summary = ?,
                cycle_count = cycle_count + 1,
                updated_at = ?, completed_at = ?
            WHERE pow_wow_id = ?
            """,
            (status, output_summary, t, t, pow_wow_id),
        )
    agent_name = s["agent_name"] if s else None
    data = ok(
        pow_wow_id=pow_wow_id,
        status=status,
        output_summary=output_summary,
        completed_by=agent_name,
        completed_at=iso(t),
    )
    emit("complete_pow_wow", data)
    return data


# ---------------------------------------------------------------------------
# Layer 2: Tasks
# ---------------------------------------------------------------------------


def claim_task(
    pow_wow_id: str,
    task_name: str,
    description: str,
    blocked_by: list[str] | None = None,
    max_retries: int = 3,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Create and immediately claim a task within a pow-wow."""
    task_id = str(uuid.uuid4())
    t = now()
    with tx() as c:
        pw = c.execute("SELECT * FROM pow_wows WHERE pow_wow_id = ?", (pow_wow_id,)).fetchone()
        if not pw:
            return err("pow_wow_not_found", pow_wow_id=pow_wow_id)
        s = optional_session(c, session_id)
        agent_name = s["agent_name"] if s else None
        sid = s["session_id"] if s else None
        c.execute(
            f"""
            INSERT INTO saga_tasks(
                task_id, pow_wow_id, saga_id, task_name, description,
                assigned_session_id, assigned_agent_name, status,
                blocked_by_json, max_retries, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, '{TaskStatus.CLAIMED}', ?, ?, ?, ?)
            """,
            (
                task_id,
                pow_wow_id,
                pw["saga_id"],
                task_name,
                description,
                sid,
                agent_name,
                json.dumps(blocked_by or []),
                max_retries,
                t,
                t,
            ),
        )
    data = ok(
        task_id=task_id,
        pow_wow_id=pow_wow_id,
        saga_id=pw["saga_id"],
        task_name=task_name,
        status="CLAIMED",
        assigned_agent=agent_name,
        created_at=iso(t),
    )
    emit("claim_task", data)
    return data


def complete_task(
    task_id: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Mark a task as completed."""
    t = now()
    with tx() as c:
        s = optional_session(c, session_id)
        task = c.execute("SELECT * FROM saga_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if not task:
            return err("not_found", task_id=task_id)
        c.execute(
            (
                f"UPDATE saga_tasks SET status = '{TaskStatus.COMPLETED}', updated_at = ?, "
                "completed_at = ? WHERE task_id = ?"
            ),
            (t, t, task_id),
        )
    agent_name = s["agent_name"] if s else None
    data = ok(task_id=task_id, status="COMPLETED", completed_by=agent_name, completed_at=iso(t))
    emit("complete_task", data)
    return data


def fail_task(
    task_id: str,
    reason: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Mark a task as failed; increments retry_count."""
    t = now()
    with tx() as c:
        s = optional_session(c, session_id)
        task = c.execute("SELECT * FROM saga_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if not task:
            return err("not_found", task_id=task_id)
        new_retries = task["retry_count"] + 1
        new_status = "FAILED" if new_retries >= task["max_retries"] else "PENDING"
        c.execute(
            """
            UPDATE saga_tasks
            SET status = ?, retry_count = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (new_status, new_retries, t, task_id),
        )
    agent_name = s["agent_name"] if s else None
    data = ok(
        task_id=task_id,
        status=new_status,
        retry_count=new_retries,
        reason=reason,
        failed_by=agent_name,
    )
    emit("fail_task", data)
    return data


def list_tasks(pow_wow_id: str, status_filter: str | None = None) -> dict[str, Any]:
    """List tasks for a pow-wow."""
    with connect() as c:
        if status_filter:
            rows = c.execute(
                "SELECT * FROM saga_tasks WHERE pow_wow_id = ? AND status = ? ORDER BY created_at",
                (pow_wow_id, status_filter),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM saga_tasks WHERE pow_wow_id = ? ORDER BY created_at",
                (pow_wow_id,),
            ).fetchall()
    tasks = []
    for r in rows:
        d = rowdict(r)
        d["blocked_by"] = decode_json_array(d.pop("blocked_by_json", None))
        d["created_at"] = iso(d["created_at"])
        d["updated_at"] = iso(d["updated_at"])
        if d.get("completed_at"):
            d["completed_at"] = iso(d["completed_at"])
        tasks.append(d)
    return ok(tasks=tasks)


# ---------------------------------------------------------------------------
# Layer 2: Artifacts
# ---------------------------------------------------------------------------


def get_artifact(artifact_id: str) -> dict[str, Any]:
    """Fetch one artifact's full content by id (e.g. to apply a code_patch)."""
    with connect() as c:
        r = c.execute(
            "SELECT * FROM task_artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
    if not r:
        return err("not_found", artifact_id=artifact_id)
    d = rowdict(r)
    d["created_at"] = iso(d["created_at"])
    return ok(artifact=d)


def latest_repo_audit(target_project_id: str, tier: str) -> dict[str, Any]:
    """The most recent repository audit for one target project and one tier.

    Returns a collection of at most one artifact, and an empty collection when
    no audit exists yet: the first dispatch for a project starts cold by
    construction, so not-found is a normal answer rather than an error. The
    project and tier live inside the artifact's JSON content - the audit names
    the repository it was read from, not the pow-wow that happened to run it -
    so the filter parses content rather than joining rows.
    """

    with connect() as c:
        rows = c.execute(
            "SELECT * FROM task_artifacts WHERE artifact_type = 'repo_audit' "
            "ORDER BY created_at DESC",
        ).fetchall()
    for r in rows:
        d = rowdict(r)
        try:
            content = json.loads(d.get("content") or "")
        except json.JSONDecodeError:
            continue
        if not isinstance(content, dict):
            continue
        if content.get("target_project_id") != target_project_id:
            continue
        if content.get("tier") != tier:
            continue
        d["created_at"] = iso(d["created_at"])
        return ok(artifacts=[d])
    return ok(artifacts=[])


def submit_artifact(
    pow_wow_id: str,
    artifact_type: str,
    content: str,
    task_id: str | None = None,
    schema_version: str = "v1",
    session_id: str | None = None,
) -> dict[str, Any]:
    """Submit an artifact produced within a pow-wow."""
    artifact_id = str(uuid.uuid4())
    t = now()
    size_bytes = len(content.encode("utf-8"))
    with tx() as c:
        pw = c.execute("SELECT * FROM pow_wows WHERE pow_wow_id = ?", (pow_wow_id,)).fetchone()
        if not pw:
            return err("pow_wow_not_found", pow_wow_id=pow_wow_id)
        s = optional_session(c, session_id)
        sid = s["session_id"] if s else None
        agent_name = s["agent_name"] if s else None
        c.execute(
            f"""
            INSERT INTO task_artifacts(
                artifact_id, task_id, pow_wow_id, saga_id,
                artifact_type, content, schema_version,
                submitted_by_session, submitted_by_agent,
                size_bytes, evaluation_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{TaskStatus.PENDING}', ?)
            """,
            (
                artifact_id,
                task_id,
                pow_wow_id,
                pw["saga_id"],
                artifact_type,
                content,
                schema_version,
                sid,
                agent_name,
                size_bytes,
                t,
            ),
        )
        if task_id:
            c.execute(
                f"UPDATE saga_tasks SET status = '{TaskStatus.IN_PROGRESS}', "
                "updated_at = ? WHERE task_id = ?",
                (t, task_id),
            )
    data = ok(
        artifact_id=artifact_id,
        pow_wow_id=pow_wow_id,
        saga_id=pw["saga_id"],
        task_id=task_id,
        artifact_type=artifact_type,
        size_bytes=size_bytes,
        evaluation_status="PENDING",
        created_at=iso(t),
    )
    emit("submit_artifact", data)
    return data


# ---------------------------------------------------------------------------
# Layer 2: Delegation
# ---------------------------------------------------------------------------


def _run_coroutine_blocking(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["result"] = asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - re-raised in caller
            box["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["result"]


def _run_delegate_task(
    *,
    prompt: str,
    tier: str = "weak",
    adapter: str | None = None,
    model_role: str = "general",
    role: str = "delegate",
    pow_wow_id: str | None = None,
    task_id: str | None = None,
    max_tokens: int = 2048,
    timeout_seconds: int = DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
    session_id: str | None = None,
) -> dict[str, Any]:
    from local_first_agent_os.delegation import agent_result_payload, delegate_agent_task
    from local_first_agent_os.runtime import get_runtime

    runtime = get_runtime()
    return agent_result_payload(
        _run_coroutine_blocking(
            delegate_agent_task(
                runtime,
                prompt=prompt,
                tier=tier,
                adapter=adapter,
                model_role=model_role,
                role=role,
                pow_wow_id=pow_wow_id,
                task_id=task_id,
                session_id=session_id,
                max_tokens=max_tokens,
                timeout_seconds=timeout_seconds,
            )
        )
    )


def delegate_task(
    prompt: str,
    tier: Literal["weak", "strong", "special"] = "weak",
    adapter: str | None = "local_llama",
    model_role: str = "general",
    role: str = "delegate",
    pow_wow_id: str | None = None,
    task_id: str | None = None,
    max_tokens: int = 2048,
    timeout_seconds: int = DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
    submit_result: bool = True,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Delegate a bounded prompt to an agent adapter.

    Defaults to the local llama.cpp adapter so Codex/Claude can offload cheap
    summarization, classification, drafting, and extraction work through MCP.
    If ``pow_wow_id`` is provided, successful output is also submitted as a
    pow-wow artifact.
    """
    result = _run_delegate_task(
        prompt=prompt,
        tier=tier,
        adapter=adapter,
        model_role=model_role,
        role=role,
        pow_wow_id=pow_wow_id,
        task_id=task_id,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        session_id=session_id,
    )

    artifact: dict[str, Any] | None = None
    if pow_wow_id and submit_result and result["ok"]:
        artifact = submit_artifact(
            pow_wow_id=pow_wow_id,
            artifact_type="delegated_task_result",
            content=result["output"],
            task_id=task_id,
            schema_version="delegated_task_result.v1",
            session_id=session_id,
        )
        artifact_ok = artifact.get("ok", False)
        if not artifact_ok:
            result = {
                **result,
                "ok": False,
                "error": artifact.get("error") or "artifact_submission_failed",
            }
        if task_id and artifact_ok:
            complete_task(task_id, session_id=session_id)
    elif task_id and not result["ok"]:
        fail_task(task_id, result.get("error") or "delegate_task failed", session_id=session_id)

    data = ok(**result, submitted_artifact=artifact)
    emit("delegate_task", data)
    return data


# ---------------------------------------------------------------------------
# Layer 2: Tool permission requests
# ---------------------------------------------------------------------------


def request_tool_permission(
    tool_name: str,
    reason: str,
    task_id: str | None = None,
    pow_wow_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Request explicit permission to use a tool.

    Roles do NOT imply permissions. Every sensitive tool use must be
    requested and granted independently of the agent's role.

    The name must be a known `Capability`. The column is free text, and free text
    meant a typo produced a durable, audited, granted row that could never match
    any policy - a permission that looks granted and is not. Refusing at write
    time is the only point where that mistake is still cheap.
    """
    try:
        tool_name = parse_capability(tool_name).value
    except UnknownCapability as exc:
        return err("unknown_capability", message=str(exc), tool_name=tool_name)
    request_id = str(uuid.uuid4())
    t = now()
    with tx() as c:
        s = optional_session(c, session_id)
        agent_name = s["agent_name"] if s else (session_id or "unknown")
        sid = s["session_id"] if s else None
        c.execute(
            f"""
            INSERT INTO tool_permission_requests(
                request_id, session_id, agent_name, task_id, pow_wow_id,
                tool_name, reason, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, '{ApprovalStatus.PENDING}', ?)
            """,
            (request_id, sid, agent_name, task_id, pow_wow_id, tool_name, reason, t),
        )
    data = ok(
        request_id=request_id,
        agent_name=agent_name,
        tool_name=tool_name,
        reason=reason,
        status="PENDING",
        created_at=iso(t),
    )
    emit("request_tool_permission", data)
    return data


def revoke_tool_permission(
    *,
    pow_wow_id: str,
    agent_name: str,
    tool_name: str,
    revoked_by: str,
) -> dict[str, Any]:
    """Take a grant back while the work is still running.

    The half that makes the gate worth consulting. Without it the ledger can only
    ever agree with the plan, so reading it proves nothing; with it an operator
    who sees an agent doing something alarming can stop the *next* spawn without
    cancelling the WorkUnit.

    It does not stop a process already running. That is `cancellation.py`'s job,
    and conflating them would give an operator one verb with two meanings.
    """

    try:
        capability = parse_capability(tool_name).value
    except UnknownCapability as exc:
        return err("unknown_capability", message=str(exc), tool_name=tool_name)
    t = now()
    with tx() as c:
        cur = c.execute(
            "UPDATE tool_permission_requests SET status='REVOKED', granted_by=?, resolved_at=? "
            "WHERE agent_name=? AND pow_wow_id=? AND tool_name=? AND status='GRANTED'",
            (revoked_by, t, agent_name, pow_wow_id, capability),
        )
        if cur.rowcount < 1:
            return err(
                "not_granted",
                pow_wow_id=pow_wow_id,
                agent_name=agent_name,
                tool_name=capability,
            )
    data = ok(
        pow_wow_id=pow_wow_id,
        agent_name=agent_name,
        tool_name=capability,
        status="REVOKED",
        revoked_by=revoked_by,
    )
    emit("revoke_tool_permission", data)
    return data


DEFAULT_GRANT_TTL_SECONDS = 24 * 60 * 60
"""How long a grant lasts when the operator does not say.

A day, not forever. The previous default was forever and was not chosen: the
column did not exist, so every grant outlived the work it was made for and there
was no moment at which anyone was asked. A default that expires makes the
standing grant the deliberate case, which is the right way round - `ttl_seconds=0`
still says "no expiry" and now says it on purpose.
"""


def grant_tool_permission(
    request_id: str,
    granted_by: str,
    ttl_seconds: int = DEFAULT_GRANT_TTL_SECONDS,
) -> dict[str, Any]:
    """Grant a pending tool-permission request."""
    if ttl_seconds < 0:
        raise ValueError("ttl_seconds must be non-negative; use 0 for a standing grant")
    t = now()
    with tx() as c:
        r = c.execute(
            "SELECT * FROM tool_permission_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if not r:
            return err("not_found", request_id=request_id)
        if TaskStatus(str(r["status"])) is not TaskStatus.PENDING:
            return err("already_resolved", request_id=request_id, status=r["status"])
        c.execute(
            """
            UPDATE tool_permission_requests
            SET status = 'GRANTED', granted_by = ?, resolved_at = ?, expires_at = ?
            WHERE request_id = ?
            """,
            (granted_by, t, (t + ttl_seconds) if ttl_seconds > 0 else None, request_id),
        )
    data = ok(
        request_id=request_id,
        status="GRANTED",
        granted_by=granted_by,
        # Said at the moment the grant is made, not discovered when it lapses.
        expires_at=iso(t + ttl_seconds) if ttl_seconds > 0 else None,
        resolved_at=iso(t),
    )
    emit("grant_tool_permission", data)
    return data


def deny_tool_permission(request_id: str, denied_by: str) -> dict[str, Any]:
    """Deny a pending tool-permission request."""
    t = now()
    with tx() as c:
        r = c.execute(
            "SELECT * FROM tool_permission_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if not r:
            return err("not_found", request_id=request_id)
        if TaskStatus(str(r["status"])) is not TaskStatus.PENDING:
            return err("already_resolved", request_id=request_id, status=r["status"])
        c.execute(
            f"""
            UPDATE tool_permission_requests
            SET status = '{ApprovalStatus.DENIED}', granted_by = ?, resolved_at = ?
            WHERE request_id = ?
            """,
            (denied_by, t, request_id),
        )
    data = ok(
        request_id=request_id,
        status="DENIED",
        denied_by=denied_by,
        resolved_at=iso(t),
    )
    emit("deny_tool_permission", data)
    return data


def list_tool_permission_requests(
    status_filter: str | None = None,
    pow_wow_id: str | None = None,
) -> dict[str, Any]:
    """List tool-permission requests, optionally filtered."""
    with connect() as c:
        clauses, params = [], []
        if status_filter:
            clauses.append("status = ?")
            params.append(status_filter)
        if pow_wow_id:
            clauses.append("pow_wow_id = ?")
            params.append(pow_wow_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = c.execute(
            f"SELECT * FROM tool_permission_requests {where} ORDER BY created_at DESC",
            params,
        ).fetchall()
    requests = []
    for r in rows:
        d = rowdict(r)
        d["created_at"] = iso(d["created_at"])
        if d.get("resolved_at"):
            d["resolved_at"] = iso(d["resolved_at"])
        requests.append(d)
    return ok(requests=requests)


# ---------------------------------------------------------------------------
# Layer 2: Evaluation
# ---------------------------------------------------------------------------


def evaluate_artifact(
    artifact_id: str,
    eval_type: str,
    score: float,
    passed: bool,
    notes: str = "",
    session_id: str | None = None,
) -> dict[str, Any]:
    """Submit an evaluation result for an artifact.

    eval_type: MECHANICAL | SEMANTIC | CONSENSUS
    score: 0.0–1.0
    """
    valid_types = {"MECHANICAL", "SEMANTIC", "CONSENSUS"}
    if eval_type not in valid_types:
        return err("invalid_eval_type", eval_type=eval_type, valid=sorted(valid_types))
    if not (0.0 <= score <= 1.0):
        return err("invalid_score", score=score, message="score must be 0.0–1.0")

    eval_id = str(uuid.uuid4())
    t = now()
    with tx() as c:
        art = c.execute(
            "SELECT * FROM task_artifacts WHERE artifact_id = ?", (artifact_id,)
        ).fetchone()
        if not art:
            return err("artifact_not_found", artifact_id=artifact_id)
        s = optional_session(c, session_id)
        sid = s["session_id"] if s else None
        agent_name = s["agent_name"] if s else None
        c.execute(
            """
            INSERT INTO evaluation_results(
                eval_id, artifact_id, pow_wow_id,
                evaluator_session_id, evaluator_agent_name,
                eval_type, score, passed, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                eval_id,
                artifact_id,
                art["pow_wow_id"],
                sid,
                agent_name,
                eval_type,
                score,
                1 if passed else 0,
                notes,
                t,
            ),
        )
        # Update artifact evaluation status
        new_eval_status = "PASSED" if passed else "FAILED"
        c.execute(
            """
            UPDATE task_artifacts
            SET evaluation_score = ?, evaluation_status = ?
            WHERE artifact_id = ?
            """,
            (score, new_eval_status, artifact_id),
        )
    data = ok(
        eval_id=eval_id,
        artifact_id=artifact_id,
        eval_type=eval_type,
        score=score,
        passed=passed,
        notes=notes,
        created_at=iso(t),
    )
    emit("evaluate_artifact", data)
    return data


def get_evaluation_summary(pow_wow_id: str) -> dict[str, Any]:
    """Aggregate evaluation results for a pow-wow.

    Returns per-type pass rates and an overall consensus verdict.
    """
    with connect() as c:
        rows = c.execute(
            """
            SELECT eval_type,
                   COUNT(*) AS total,
                   SUM(passed) AS passed_count,
                   AVG(score) AS avg_score
            FROM evaluation_results
            WHERE pow_wow_id = ?
            GROUP BY eval_type
            """,
            (pow_wow_id,),
        ).fetchall()

    summary: dict[str, Any] = {}
    for r in rows:
        summary[r["eval_type"]] = {
            "total": r["total"],
            "passed": r["passed_count"],
            "pass_rate": round(r["passed_count"] / r["total"], 3) if r["total"] else 0,
            "avg_score": round(r["avg_score"], 3) if r["avg_score"] is not None else None,
        }

    # Overall: all three types must have avg_score >= 0.7 to pass
    all_pass = all(v["pass_rate"] >= 0.7 for v in summary.values()) if summary else False
    return ok(
        pow_wow_id=pow_wow_id,
        by_type=summary,
        overall_pass=all_pass,
        verdict="PASS" if all_pass else "FAIL",
    )


# ---------------------------------------------------------------------------
# Layer 2: Approval gates
# ---------------------------------------------------------------------------
