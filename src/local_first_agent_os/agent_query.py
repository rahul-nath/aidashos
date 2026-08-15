# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Direct frontier-CLI queries, recorded but not transcribed.

`pi /claude <question>` and `pi /codex <question>` reach a frontier CLI without
a worktree, a lease, or a dispatch intent. That is deliberate: a question is not
a code change, and giving it the machinery of one would make the ledger's
notion of work meaningless.

What is recorded is the question, the harness that answered it, and a pointer to
the transcript the CLI already wrote to disk. The response body is not copied
into the artifact store, because the CLI owns that file and duplicating it would
create a second, staler copy of the same conversation.

Each durable boundary is its own `@dbos_step`, so a crash between running the
CLI and recording the result recovers without asking the model twice.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ._dbos_runtime import dbos_step
from .agent_adapters import AgentResult, AgentTask
from .constants import DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS
from .contracts import AgentHarness

AGENT_QUERY_RECORD_SCHEMA = "agent_query_record.v1"

CLAUDE_TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"
CODEX_TRANSCRIPT_ROOT = Path.home() / ".codex" / "sessions"


class TranscriptResolution(StrEnum):
    """How confident the pointer is, which is not the same as whether one exists.

    Claude Code names its transcript after the session id it returns, so that
    pointer is exact. Codex names its rollout file by timestamp, so the best
    available match is the newest file written after the run began, which is a
    guess whenever two runs overlap. Recording which of the two produced a
    pointer keeps a guess from being read as a fact later.
    """

    EXACT_SESSION_ID = "exact_session_id"
    NEWEST_AFTER_START = "newest_after_start"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class TranscriptPointer:
    resolution: TranscriptResolution
    path: str | None = None
    session_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "resolution": self.resolution.value,
            "path": self.path,
            "session_id": self.session_id,
        }


def claude_project_slug(cwd: str) -> str:
    """Claude Code stores transcripts under the working directory, slugified.

    Every character that is not alphanumeric becomes a dash, so
    `/Users/rahul/ai_projects/x` becomes `-Users-rahul-ai-projects-x`.
    """

    return "".join(char if char.isalnum() else "-" for char in cwd)


@dbos_step()
def run_agent_query(payload: dict[str, Any]) -> dict[str, Any]:
    """Ask the harness the question. The only step that leaves the machine."""

    harness = AgentHarness(payload["harness"])
    adapter = _build_adapter(harness, cwd=payload["cwd"])
    task = AgentTask(
        task_id=payload["workflow_id"],
        pow_wow_id="",
        saga_id="",
        role="operator_query",
        prompt=payload["query"],
        timeout_seconds=int(payload.get("timeout_seconds", DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS)),
    )
    result = asyncio.run(adapter.run(task))
    return _result_payload(result)


@dbos_step()
def resolve_transcript_pointer(payload: dict[str, Any]) -> dict[str, Any]:
    """Locate the transcript the CLI wrote, without reading or copying it."""

    harness = AgentHarness(payload["harness"])
    if harness is AgentHarness.CLAUDE_CODE:
        pointer = _resolve_claude_transcript(
            session_id=payload.get("session_id"),
            cwd=payload["cwd"],
        )
    else:
        pointer = _resolve_codex_transcript(started_at_epoch=float(payload["started_at_epoch"]))
    return pointer.to_payload()


@dbos_step()
def build_agent_query_record(payload: dict[str, Any]) -> dict[str, Any]:
    """Assemble the durable record: the question, not the answer."""

    return {
        "schema_version": AGENT_QUERY_RECORD_SCHEMA,
        "harness": payload["harness"],
        "alias": payload.get("alias"),
        "query": payload["query"],
        "cwd": payload["cwd"],
        "asked_at": payload["asked_at"],
        "succeeded": bool(payload["succeeded"]),
        "error": payload.get("error"),
        "tokens_used": int(payload.get("tokens_used") or 0),
        "transcript": payload["transcript"],
    }


def _build_adapter(harness: AgentHarness, *, cwd: str) -> Any:
    from .agent_adapters import ClaudeCodeAdapter, CodexCLIAdapter

    if harness is AgentHarness.CLAUDE_CODE:
        return ClaudeCodeAdapter(cwd=cwd)
    return CodexCLIAdapter(cwd=cwd)


def _result_payload(result: AgentResult) -> dict[str, Any]:
    return {
        "succeeded": result.success,
        "output": result.output,
        "error": result.error,
        "tokens_used": result.tokens_used,
        "session_id": result.metadata.get("session_id"),
    }


def _resolve_claude_transcript(*, session_id: str | None, cwd: str) -> TranscriptPointer:
    if not session_id:
        return TranscriptPointer(TranscriptResolution.UNRESOLVED)
    path = CLAUDE_TRANSCRIPT_ROOT / claude_project_slug(cwd) / f"{session_id}.jsonl"
    if not path.is_file():
        return TranscriptPointer(TranscriptResolution.UNRESOLVED, session_id=session_id)
    return TranscriptPointer(
        TranscriptResolution.EXACT_SESSION_ID,
        path=str(path),
        session_id=session_id,
    )


def _resolve_codex_transcript(*, started_at_epoch: float) -> TranscriptPointer:
    candidates = [
        path
        for path in CODEX_TRANSCRIPT_ROOT.rglob("rollout-*.jsonl")
        if path.is_file() and path.stat().st_mtime >= started_at_epoch
    ]
    if not candidates:
        return TranscriptPointer(TranscriptResolution.UNRESOLVED)
    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    return TranscriptPointer(TranscriptResolution.NEWEST_AFTER_START, path=str(newest))


def agent_query_request(
    *,
    workflow_id: str,
    harness: AgentHarness,
    alias: str | None,
    query: str,
    cwd: str | None = None,
    timeout_seconds: int = DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Build the step payload once so every step reads the same fields."""

    return {
        "workflow_id": workflow_id,
        "harness": harness.value,
        "alias": alias,
        "query": query,
        "cwd": cwd or os.getcwd(),
        "timeout_seconds": timeout_seconds,
        "asked_at": datetime.now(UTC).isoformat(),
        "started_at_epoch": datetime.now(UTC).timestamp(),
    }


__all__ = [
    "AGENT_QUERY_RECORD_SCHEMA",
    "TranscriptPointer",
    "TranscriptResolution",
    "agent_query_request",
    "build_agent_query_record",
    "claude_project_slug",
    "resolve_transcript_pointer",
    "run_agent_query",
]
