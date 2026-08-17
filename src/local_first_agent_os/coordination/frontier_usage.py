# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Hot-path projections for frontier continuation identity and token usage."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from .store import connect, iso, ok, rowdict

WEIGHT_POLICY: Final = "openai_api_relative.v1"
_MILLI_PER_UNCACHED_INPUT: Final = 1_000
_MILLI_PER_CACHED_INPUT: Final = 100
_MILLI_PER_CACHE_WRITE: Final = 1_250
_MILLI_PER_OUTPUT: Final = 6_000
_USAGE_NAMESPACE = uuid.UUID("f40060ca-8a41-43b7-9fb1-23bb8c2a8d63")


@dataclass(frozen=True)
class FrontierTurnUsage:
    """One valid provider-reported turn, separated by billing-relevant kind."""

    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int

    @property
    def uncached_input_tokens(self) -> int:
        return self.input_tokens - self.cached_input_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def effective_units_milli(self) -> int:
        return (
            self.uncached_input_tokens * _MILLI_PER_UNCACHED_INPUT
            + self.cached_input_tokens * _MILLI_PER_CACHED_INPUT
            + self.cache_write_tokens * _MILLI_PER_CACHE_WRITE
            + self.output_tokens * _MILLI_PER_OUTPUT
        )


def _token_count(usage: Mapping[str, object], field: str, *, default: int | None = None) -> int:
    value = usage.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"frontier usage requires non-negative integer {field!r}")
    return value


def parse_frontier_turn_usage(payload: Mapping[str, object]) -> FrontierTurnUsage | None:
    """Parse a normalized ``turn.completed`` payload without guessing missing usage."""

    raw_usage = payload.get("usage")
    if raw_usage is None:
        return None
    if not isinstance(raw_usage, Mapping):
        raise ValueError("turn.completed usage must be an object")
    input_tokens = _token_count(raw_usage, "input_tokens")
    cached_input_tokens = _token_count(raw_usage, "cached_input_tokens", default=0)
    if cached_input_tokens > input_tokens:
        raise ValueError("cached_input_tokens cannot exceed input_tokens")
    return FrontierTurnUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        cache_write_tokens=_token_count(raw_usage, "cache_write_tokens", default=0),
        output_tokens=_token_count(raw_usage, "output_tokens"),
    )


def _continuation_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    value = rowdict(row)
    value["created_at"] = iso(value["created_at"])
    value["updated_at"] = iso(value["updated_at"])
    return value


def _usage_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    value = rowdict(row)
    value["created_at"] = iso(value["created_at"])
    return value


def project_frontier_event(
    c: Any,
    *,
    lease: Mapping[str, Any],
    sequence: int,
    kind: str,
    payload: Mapping[str, object],
    created_at: float,
) -> None:
    """Project one newly appended provider event in the event transaction."""

    harness = str(lease.get("agent_name") or "")
    if harness != "codex":
        return
    task_id = lease.get("task_id")
    task_row = (
        c.execute(
            "SELECT pow_wow_id, saga_id FROM saga_tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if task_id
        else None
    )
    pow_wow_id = task_row["pow_wow_id"] if task_row else None
    saga_id = task_row["saga_id"] if task_row else None

    if kind == "thread.started":
        raw_thread_id = payload.get("thread_id")
        if not isinstance(raw_thread_id, str) or not raw_thread_id.strip():
            raise ValueError("thread.started requires a non-empty thread_id")
        thread_id = raw_thread_id.strip()
        c.execute(
            """
            INSERT INTO agent_continuations(
                thread_id, latest_lease_id, latest_task_id, pow_wow_id,
                harness, model, task_role, agent_tier, target_project_id,
                planning_phase, source_revision, permission_envelope_sha256,
                source_sequence, resume_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                latest_lease_id=excluded.latest_lease_id,
                latest_task_id=excluded.latest_task_id,
                pow_wow_id=excluded.pow_wow_id,
                harness=excluded.harness,
                model=excluded.model,
                task_role=excluded.task_role,
                agent_tier=excluded.agent_tier,
                target_project_id=excluded.target_project_id,
                planning_phase=excluded.planning_phase,
                source_revision=excluded.source_revision,
                permission_envelope_sha256=excluded.permission_envelope_sha256,
                source_sequence=excluded.source_sequence,
                resume_count=agent_continuations.resume_count
                    + CASE WHEN agent_continuations.latest_lease_id <> excluded.latest_lease_id
                           THEN 1 ELSE 0 END,
                updated_at=excluded.updated_at
            """,
            (
                thread_id,
                lease["lease_id"],
                task_id,
                pow_wow_id,
                harness,
                lease.get("model"),
                lease.get("task_role"),
                lease.get("agent_tier"),
                lease.get("target_project_id"),
                lease.get("planning_phase"),
                lease.get("source_revision"),
                lease.get("permission_envelope_sha256"),
                sequence,
                created_at,
                created_at,
            ),
        )
        return

    if kind != "turn.completed":
        return
    usage = parse_frontier_turn_usage(payload)
    if usage is None:
        return
    continuation = c.execute(
        "SELECT thread_id FROM agent_continuations WHERE latest_lease_id=?",
        (lease["lease_id"],),
    ).fetchone()
    usage_record_id = str(
        uuid.uuid5(_USAGE_NAMESPACE, f"frontier-usage:{lease['lease_id']}:{sequence}")
    )
    inserted = c.execute(
        """
        INSERT INTO frontier_usage_records(
            usage_record_id, lease_id, event_sequence, thread_id, task_id,
            pow_wow_id, saga_id, task_role, agent_tier, harness, model,
            input_tokens, cached_input_tokens, uncached_input_tokens,
            cache_write_tokens, output_tokens, effective_units_milli,
            weight_policy, measured, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TRUE, ?)
        ON CONFLICT(lease_id, event_sequence) DO NOTHING
        """,
        (
            usage_record_id,
            lease["lease_id"],
            sequence,
            continuation["thread_id"] if continuation else None,
            task_id,
            pow_wow_id,
            saga_id,
            lease.get("task_role"),
            lease.get("agent_tier"),
            harness,
            lease.get("model"),
            usage.input_tokens,
            usage.cached_input_tokens,
            usage.uncached_input_tokens,
            usage.cache_write_tokens,
            usage.output_tokens,
            usage.effective_units_milli,
            WEIGHT_POLICY,
            created_at,
        ),
    )
    if inserted.rowcount != 1:
        return
    if pow_wow_id is not None:
        c.execute(
            "UPDATE pow_wows SET consumed_tokens=consumed_tokens+?, updated_at=? "
            "WHERE pow_wow_id=?",
            (usage.total_tokens, created_at, pow_wow_id),
        )
    if saga_id is not None:
        c.execute(
            "UPDATE sagas SET consumed_tokens=consumed_tokens+?, updated_at=? WHERE saga_id=?",
            (usage.total_tokens, created_at, saga_id),
        )


def find_compatible_agent_continuation(
    source_task_id: str,
    *,
    pow_wow_id: str,
    harness: str,
    source_model: str | None,
    target_project_id: str,
    source_revision: str,
) -> dict[str, Any]:
    """Find the completed source task's exact reusable frontier conversation."""

    with connect() as c:
        row = c.execute(
            """
            SELECT continuation.*, lease.status AS lease_status
            FROM agent_continuations AS continuation
            JOIN agent_execution_leases AS lease
              ON lease.lease_id = continuation.latest_lease_id
            WHERE continuation.latest_task_id=?
            ORDER BY continuation.updated_at DESC
            LIMIT 1
            """,
            (source_task_id,),
        ).fetchone()
    if row is None:
        return ok(compatible=False, reason="not_found", continuation={})
    expected = {
        "pow_wow_id": pow_wow_id,
        "harness": harness,
        "model": source_model,
        "target_project_id": target_project_id,
        "planning_phase": "senior_independent_reading",
        "source_revision": source_revision,
        "lease_status": "COMPLETED",
    }
    for field, value in expected.items():
        if row[field] != value:
            return ok(
                compatible=False,
                reason=f"{field}_mismatch",
                continuation=_continuation_to_dict(row),
            )
    return ok(compatible=True, reason="compatible", continuation=_continuation_to_dict(row))


def list_frontier_usage_records(lease_id: str) -> dict[str, Any]:
    """Read normalized usage for one lease without touching transcript payloads."""

    with connect() as c:
        rows = c.execute(
            "SELECT * FROM frontier_usage_records WHERE lease_id=? ORDER BY event_sequence",
            (lease_id,),
        ).fetchall()
    return ok(usage_records=[_usage_to_dict(row) for row in rows])


__all__ = [
    "FrontierTurnUsage",
    "WEIGHT_POLICY",
    "find_compatible_agent_continuation",
    "list_frontier_usage_records",
    "parse_frontier_turn_usage",
    "project_frontier_event",
]
