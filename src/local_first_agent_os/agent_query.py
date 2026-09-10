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
from typing import TYPE_CHECKING, Any, assert_never

from ._dbos_runtime import dbos_step
from .agent_adapters import AgentResult, AgentTask, CliRunProvenance
from .constants import DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS
from .contracts import AgentHarness
from .staffing import BenchSlot, Harness, Staffing, load_staffing

if TYPE_CHECKING:
    from .agent_adapters import ClaudeCodeAdapter, CodexCLIAdapter

AGENT_QUERY_RECORD_SCHEMA = "agent_query_record.v2"

CLAUDE_TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"
CODEX_TRANSCRIPT_ROOT = Path.home() / ".codex" / "sessions"


class TranscriptResolution(StrEnum):
    """How confident the pointer is, which is not the same as whether one exists.

    Both CLIs return session ids on their structured output, and a rollout file
    containing that id is exact. The timestamp fallback for older Codex output
    is explicitly recorded as a guess whenever it is needed.
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
    adapter = _build_adapter(harness, cwd=payload["cwd"], model=payload["model"])
    task = AgentTask(
        task_id=payload["workflow_id"],
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
        pointer = _resolve_codex_transcript(
            session_id=payload.get("session_id"),
            started_at_epoch=float(payload["started_at_epoch"]),
        )
    return pointer.to_payload()


@dbos_step()
def build_agent_query_record(payload: dict[str, Any]) -> dict[str, Any]:
    """Assemble the durable record: the question, not the answer."""

    return {
        "schema_version": AGENT_QUERY_RECORD_SCHEMA,
        "harness": payload["harness"],
        "model": payload["model"],
        "alias": payload.get("alias"),
        "query": payload["query"],
        "cwd": payload["cwd"],
        "asked_at": payload["asked_at"],
        "succeeded": bool(payload["succeeded"]),
        "error": payload.get("error"),
        "tokens_used": int(payload.get("tokens_used") or 0),
        "provenance": payload["provenance"],
        "transcript": payload["transcript"],
    }


def _build_adapter(
    harness: AgentHarness, *, cwd: str, model: str
) -> ClaudeCodeAdapter | CodexCLIAdapter:
    from .agent_adapters import ClaudeCodeAdapter, CodexCLIAdapter

    match harness:
        case AgentHarness.CLAUDE_CODE:
            return ClaudeCodeAdapter(cwd=cwd, model=model)
        case AgentHarness.CODEX_CLI:
            return CodexCLIAdapter(cwd=cwd, model=model)
    assert_never(harness)


def configured_agent_query_model(harness: AgentHarness, *, config_dir: Path) -> str:
    """Resolve one explicit model for a direct query or refuse ambiguity.

    A direct query names a vendor rather than a tier. It can borrow the seated
    pairing's model only when its seated slots identify one explicit model for
    that vendor. A direct_query_models entry resolves different models without
    guessing whether an unstaffed question is senior or staff work.
    """

    match harness:
        case AgentHarness.CLAUDE_CODE:
            staffing_harness = Harness.CLAUDE
        case AgentHarness.CODEX_CLI:
            staffing_harness = Harness.CODEX
        case _:
            assert_never(harness)
    staffing = load_staffing(config_dir / "staffing.toml")
    if model := staffing.direct_query_models.get(staffing_harness):
        return model
    candidates = {
        slot.model.strip()
        for slot in staffing_harness_slots(staffing, staffing_harness)
        if slot.model is not None and slot.model.strip()
    }
    if len(candidates) != 1:
        raise ValueError(
            f"direct {harness.value} queries require exactly one explicit model in the "
            f"seated staffing pairing; found {sorted(candidates)}; set "
            f"direct_query_models.{staffing_harness.value} explicitly"
        )
    return candidates.pop()


def staffing_harness_slots(staffing: Staffing, harness: Harness) -> tuple[BenchSlot, ...]:
    """Return seated slots for one harness without inventing a fallback route."""

    return tuple(slot for slot in staffing.seated.seats().values() if slot.harness is harness)


def _result_payload(result: AgentResult) -> dict[str, Any]:
    if not isinstance(result.provenance, CliRunProvenance):
        raise TypeError("direct agent queries require CLI provenance")
    return {
        "succeeded": result.success,
        "output": result.output,
        "error": result.error_text,
        "tokens_used": result.tokens_used,
        "session_id": result.provenance.session_id,
        "provenance": result.provenance.to_payload(),
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


def _resolve_codex_transcript(
    *,
    started_at_epoch: float,
    session_id: str | None = None,
) -> TranscriptPointer:
    if session_id:
        exact = [
            path for path in CODEX_TRANSCRIPT_ROOT.rglob(f"*{session_id}*.jsonl") if path.is_file()
        ]
        if len(exact) == 1:
            return TranscriptPointer(
                TranscriptResolution.EXACT_SESSION_ID,
                path=str(exact[0]),
                session_id=session_id,
            )
    candidates = [
        path
        for path in CODEX_TRANSCRIPT_ROOT.rglob("rollout-*.jsonl")
        if path.is_file() and path.stat().st_mtime >= started_at_epoch
    ]
    if not candidates:
        return TranscriptPointer(TranscriptResolution.UNRESOLVED, session_id=session_id)
    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    return TranscriptPointer(
        TranscriptResolution.NEWEST_AFTER_START,
        path=str(newest),
        session_id=session_id,
    )


def agent_query_request(
    *,
    workflow_id: str,
    harness: AgentHarness,
    model: str,
    alias: str | None,
    query: str,
    cwd: str | None = None,
    timeout_seconds: int = DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Build the step payload once so every step reads the same fields."""

    return {
        "workflow_id": workflow_id,
        "harness": harness.value,
        "model": model,
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
    "configured_agent_query_model",
    "resolve_transcript_pointer",
    "run_agent_query",
]
