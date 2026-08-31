# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Adopt the durable effects of a transport-interrupted WorkUnit attempt."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from .coordination.outcomes import TerminalOutcome
from .coordination.store import ConnectionLike, rowdict, tx
from .ids import sha256_text

INTERRUPTED_RECOVERY_EVENT: Final = "interrupted_attempt_recovery"
INTERRUPTED_RECOVERY_SCHEMA: Final = "interrupted_attempt_recovery.v1"


@dataclass(frozen=True)
class NoInterruptedEffects:
    previous_intent_id: str | None = None


@dataclass(frozen=True)
class RetainedWorktree:
    effect_id: str
    previous_intent_id: str
    worktree_path: str
    source_repo_path: str
    base_head_sha: str
    changed_files: tuple[str, ...]
    evidence_hash: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": INTERRUPTED_RECOVERY_SCHEMA,
            "state": "RETAINED_WORKTREE",
            "effect_id": self.effect_id,
            "previous_intent_id": self.previous_intent_id,
            "worktree_path": self.worktree_path,
            "source_repo_path": self.source_repo_path,
            "base_head_sha": self.base_head_sha,
            "changed_files": list(self.changed_files),
            "evidence_hash": self.evidence_hash,
            "model_invocation_avoided": False,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> RetainedWorktree:
        if (
            payload.get("schema_version") != INTERRUPTED_RECOVERY_SCHEMA
            or payload.get("state") != "RETAINED_WORKTREE"
        ):
            raise ValueError("unsupported interrupted recovery payload")
        return cls(
            effect_id=str(payload["effect_id"]),
            previous_intent_id=str(payload["previous_intent_id"]),
            worktree_path=str(payload["worktree_path"]),
            source_repo_path=str(payload["source_repo_path"]),
            base_head_sha=str(payload["base_head_sha"]),
            changed_files=tuple(str(item) for item in payload.get("changed_files", ())),
            evidence_hash=str(payload["evidence_hash"]),
        )


@dataclass(frozen=True)
class InterruptedEffectsConflict:
    previous_intent_id: str
    reason: str


type InterruptedRecovery = NoInterruptedEffects | RetainedWorktree | InterruptedEffectsConflict


class InterruptedRecoveryRefused(RuntimeError):
    code = "interrupted_effects_conflict"

    def __init__(self, conflict: InterruptedEffectsConflict) -> None:
        self.conflict = conflict
        super().__init__(conflict.reason)


def _run_artifacts(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    run_result = payload.get("run_result")
    if not isinstance(run_result, Mapping):
        return ()
    artifacts: list[Mapping[str, Any]] = []
    for task in run_result.get("tasks") or ():
        if not isinstance(task, Mapping):
            continue
        for artifact in task.get("artifacts") or ():
            if isinstance(artifact, Mapping):
                artifacts.append(artifact)
    return tuple(artifacts)


def _retained_candidates(result: str) -> tuple[dict[str, Any], ...]:
    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return ()
    if not isinstance(payload, Mapping):
        return ()
    candidates: list[dict[str, Any]] = []
    for artifact in _run_artifacts(payload):
        if artifact.get("artifact_type") != "cli_agent_run":
            continue
        content = artifact.get("content")
        if not isinstance(content, Mapping):
            continue
        supervisor = content.get("streaming_supervisor")
        worktree = content.get("worktree")
        changed = tuple(sorted(str(item) for item in content.get("changed_files") or ()))
        if (
            not isinstance(supervisor, Mapping)
            or not supervisor.get("preserve_worktree")
            or not isinstance(worktree, Mapping)
            or not changed
        ):
            continue
        candidates.append(
            {
                "worktree_path": str(worktree.get("worktree_path") or ""),
                "source_repo_path": str(worktree.get("source_repo_path") or ""),
                "base_head_sha": str(worktree.get("head_sha") or ""),
                "changed_files": changed,
            }
        )
    return tuple(candidates)


def inspect_interrupted_attempt(
    work_unit_id: str,
    milestone_key: str,
    attempt: int,
) -> InterruptedRecovery:
    """Classify the predecessor before a successor is allowed to submit."""

    if attempt <= 1:
        return NoInterruptedEffects()
    source = f"work_unit:{work_unit_id}:milestone_execution:{milestone_key}"
    with tx() as c:
        raw = c.execute(
            "SELECT intent_id, result FROM dispatch_intents "
            "WHERE source=? AND status='FAILED' AND outcome=? "
            "ORDER BY completed_at DESC LIMIT 1",
            (source, TerminalOutcome.TRANSPORT_INTERRUPTED.value),
        ).fetchone()
    if raw is None:
        return NoInterruptedEffects()
    row = rowdict(raw)
    previous_intent_id = str(row["intent_id"])
    candidates = _retained_candidates(str(row.get("result") or ""))
    if not candidates:
        return NoInterruptedEffects(previous_intent_id=previous_intent_id)
    canonical = {json.dumps(candidate, sort_keys=True) for candidate in candidates}
    if len(canonical) != 1:
        return InterruptedEffectsConflict(
            previous_intent_id=previous_intent_id,
            reason=(
                f"transport-interrupted intent {previous_intent_id} retained conflicting "
                "worktrees; choose the exact effect to continue"
            ),
        )
    candidate = candidates[0]
    worktree_path = str(candidate["worktree_path"])
    source_repo_path = str(candidate["source_repo_path"])
    base_head_sha = str(candidate["base_head_sha"])
    if not worktree_path or not source_repo_path or not base_head_sha:
        return InterruptedEffectsConflict(
            previous_intent_id=previous_intent_id,
            reason=f"transport-interrupted intent {previous_intent_id} retained incomplete paths",
        )
    if not Path(worktree_path).is_dir():
        return InterruptedEffectsConflict(
            previous_intent_id=previous_intent_id,
            reason=(
                f"transport-interrupted intent {previous_intent_id} recorded retained work at "
                f"{worktree_path}, but that worktree is missing"
            ),
        )
    evidence = json.dumps(candidate, sort_keys=True)
    effect_identity = f"{work_unit_id}:{milestone_key}:{attempt - 1}:retained_worktree"
    return RetainedWorktree(
        effect_id=f"ie_{sha256_text(effect_identity)[:24]}",
        previous_intent_id=previous_intent_id,
        worktree_path=worktree_path,
        source_repo_path=source_repo_path,
        base_head_sha=base_head_sha,
        changed_files=tuple(candidate["changed_files"]),
        evidence_hash=sha256_text(evidence),
    )


def record_interrupted_recovery(
    c: ConnectionLike,
    intent_id: str,
    recovery: RetainedWorktree,
    *,
    recorded_at: float,
) -> None:
    event_id = f"interrupted-recovery:{intent_id}"
    encoded = json.dumps(recovery.to_payload(), sort_keys=True)
    c.execute(
        "INSERT INTO ledger_events(event_id, event_type, aggregate_type, aggregate_id, "
        "payload_json, status, attempts, created_at) "
        "VALUES (?, ?, 'dispatch_intent', ?, ?, 'PROCESSED', 0, ?) "
        "ON CONFLICT (event_id) DO NOTHING",
        (event_id, INTERRUPTED_RECOVERY_EVENT, intent_id, encoded, recorded_at),
    )
    raw = c.execute(
        "SELECT payload_json FROM ledger_events WHERE event_id=?", (event_id,)
    ).fetchone()
    if raw is None or str(rowdict(raw)["payload_json"]) != encoded:
        raise RuntimeError(f"dispatch intent {intent_id} carries conflicting recovery effects")


def recovery_for_intent(intent_id: str) -> RetainedWorktree | None:
    with tx() as c:
        raw = c.execute(
            "SELECT payload_json FROM ledger_events WHERE event_id=? AND event_type=?",
            (f"interrupted-recovery:{intent_id}", INTERRUPTED_RECOVERY_EVENT),
        ).fetchone()
    if raw is None:
        return None
    return RetainedWorktree.from_payload(json.loads(str(rowdict(raw)["payload_json"])))


__all__ = [
    "INTERRUPTED_RECOVERY_EVENT",
    "InterruptedEffectsConflict",
    "InterruptedRecoveryRefused",
    "NoInterruptedEffects",
    "RetainedWorktree",
    "inspect_interrupted_attempt",
    "record_interrupted_recovery",
    "recovery_for_intent",
]
