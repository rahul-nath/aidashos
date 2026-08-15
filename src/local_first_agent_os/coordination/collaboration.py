# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Agent sessions, file claims, notes, and handoffs."""

from __future__ import annotations

import json
import uuid
from typing import Any

from .store import (
    connect,
    emit,
    err,
    iso,
    normalize_paths,
    now,
    ok,
    optional_session,
    rowdict,
    session_to_dict,
    tx,
)


def register_agent(agent_name: str, session_id: str | None = None) -> dict[str, Any]:
    """Register or refresh an agent session. Call this before claiming files."""
    sid = session_id or str(uuid.uuid4())
    t = now()
    with tx() as c:
        c.execute(
            """
            INSERT INTO sessions(session_id, agent_name, created_at, last_heartbeat_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                agent_name = excluded.agent_name,
                last_heartbeat_at = excluded.last_heartbeat_at
            """,
            (sid, agent_name, t, t),
        )
    data = ok(agent_name=agent_name, session_id=sid, created_or_refreshed_at=iso(t))
    emit("register_agent", data)
    return data


def heartbeat(session_id: str) -> dict[str, Any]:
    """Refresh a registered session timestamp."""
    t = now()
    with tx() as c:
        cur = c.execute(
            "UPDATE sessions SET last_heartbeat_at = ? WHERE session_id = ?",
            (t, session_id),
        )
        if cur.rowcount != 1:
            return err("unknown_session", session_id=session_id)
    data = ok(session_id=session_id, last_heartbeat_at=iso(t))
    emit("heartbeat", data)
    return data


def append_note(scope: str, message: str, session_id: str | None = None) -> dict[str, Any]:
    """Append an intent, status, summary, test result, or coordination note."""
    t = now()
    with tx() as c:
        s = optional_session(c, session_id)
        cur = c.execute(
            """
            INSERT INTO notes(scope, session_id, agent_name, message, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (scope, s["session_id"] if s else None, s["agent_name"] if s else None, message, t),
        )
        note_id = cur.lastrowid

    data = ok(
        id=note_id,
        scope=scope,
        session_id=s["session_id"] if s else None,
        agent_name=s["agent_name"] if s else None,
        message=message,
        created_at=iso(t),
    )
    emit("append_note", data)
    return data


def read_notes(scope: str, limit: int = 50) -> dict[str, Any]:
    """Read notes for a scope. Use scope='*' for all notes."""
    with connect() as c:
        if scope == "*":
            rows = c.execute(
                "SELECT * FROM notes ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = c.execute(
                """
                SELECT * FROM notes
                WHERE scope = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (scope, limit),
            ).fetchall()

    notes = []
    for r in rows:
        d = rowdict(r)
        d["created_at"] = iso(d["created_at"])
        notes.append(d)
    return ok(notes=notes)


def handoff(
    paths: list[str],
    summary: str,
    status: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Record a handoff for paths with summary and status."""
    paths = normalize_paths(paths)
    t = now()
    with tx() as c:
        s = optional_session(c, session_id)
        cur = c.execute(
            """
            INSERT INTO handoffs(paths_json, summary, status, session_id, agent_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                json.dumps(paths),
                summary,
                status,
                s["session_id"] if s else None,
                s["agent_name"] if s else None,
                t,
            ),
        )
        handoff_id = cur.lastrowid

    data = ok(
        id=handoff_id,
        paths=paths,
        summary=summary,
        status=status,
        session_id=s["session_id"] if s else None,
        agent_name=s["agent_name"] if s else None,
        created_at=iso(t),
    )
    emit("handoff", data)
    return data


def list_sessions() -> dict[str, Any]:
    """List registered agent sessions."""
    with connect() as c:
        rows = c.execute("SELECT * FROM sessions ORDER BY last_heartbeat_at DESC").fetchall()
    return ok(sessions=[session_to_dict(r) for r in rows])


# ---------------------------------------------------------------------------
# Layer 2: GAWD doc
# ---------------------------------------------------------------------------
