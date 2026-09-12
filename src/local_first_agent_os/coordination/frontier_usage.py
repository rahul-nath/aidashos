# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Hot-path projections for frontier continuation identity and token usage."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, assert_never

from ..staffing import FrontierHarness
from .store import connect, iso, ok, rowdict

WEIGHT_POLICY: Final = "openai_api_relative.v1"
_MILLI_PER_UNCACHED_INPUT: Final = 1_000
_MILLI_PER_CACHED_INPUT: Final = 100
_MILLI_PER_CACHE_WRITE: Final = 1_250
_MILLI_PER_OUTPUT: Final = 6_000
_USAGE_NAMESPACE = uuid.UUID("f40060ca-8a41-43b7-9fb1-23bb8c2a8d63")


@dataclass(frozen=True)
class FrontierTurnUsage:
    """One measured invocation normalized to the existing usage schema."""

    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        counts = (
            self.input_tokens,
            self.cached_input_tokens,
            self.cache_write_tokens,
            self.output_tokens,
        )
        if any(
            isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in counts
        ):
            raise ValueError("frontier usage token counts must be non-negative integers")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens cannot exceed input_tokens")

    @property
    def uncached_input_tokens(self) -> int:
        return self.input_tokens - self.cached_input_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.cache_write_tokens + self.output_tokens

    @property
    def effective_units_milli(self) -> int:
        return (
            self.uncached_input_tokens * _MILLI_PER_UNCACHED_INPUT
            + self.cached_input_tokens * _MILLI_PER_CACHED_INPUT
            + self.cache_write_tokens * _MILLI_PER_CACHE_WRITE
            + self.output_tokens * _MILLI_PER_OUTPUT
        )


@dataclass(frozen=True)
class CodexUsage:
    """Measured cumulative usage from one completed Codex invocation."""

    tokens: FrontierTurnUsage


@dataclass(frozen=True)
class ClaudeUsage:
    """Measured cumulative usage from one Claude ``result`` event."""

    tokens: FrontierTurnUsage
    reported_model: str | None


class UsageUnverifiableReason(StrEnum):
    """Why a provider event cannot truthfully become a measured usage row."""

    MISSING_USAGE = "missing_usage"
    NOT_REPORTED = "not_reported"
    MISSING_REQUIRED_FIELDS = "missing_required_fields"
    UNSUPPORTED_EVENT = "unsupported_event"
    UNSUPPORTED_HARNESS = "unsupported_harness"


@dataclass(frozen=True)
class UsageUnverifiable:
    """Provider evidence exists, but it does not prove numeric token usage."""

    harness: str
    kind: str
    reason: UsageUnverifiableReason
    missing_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.reason, UsageUnverifiableReason):
            raise ValueError("reason must be a UsageUnverifiableReason")
        fields = self.missing_fields
        if (
            not isinstance(fields, tuple)
            or any(not isinstance(field, str) or not field.strip() for field in fields)
            or len(set(fields)) != len(fields)
        ):
            raise ValueError("missing_fields must contain unique, non-empty field names")
        requires_fields = self.reason is UsageUnverifiableReason.MISSING_REQUIRED_FIELDS
        if requires_fields != bool(fields):
            raise ValueError(
                "missing_required_fields requires field names and other reasons forbid them"
            )


type FrontierUsageEvidence = CodexUsage | ClaudeUsage | UsageUnverifiable


def _token_count(usage: Mapping[str, object], field: str, *, provider: str) -> int:
    value = usage.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{provider} usage requires non-negative integer {field!r}")
    return value


def _unverifiable(
    *,
    harness: str,
    kind: str,
    reason: UsageUnverifiableReason,
    missing_fields: tuple[str, ...] = (),
) -> UsageUnverifiable:
    return UsageUnverifiable(
        harness=harness,
        kind=kind,
        reason=reason,
        missing_fields=missing_fields,
    )


def _usage_mapping(
    payload: Mapping[str, object],
    *,
    harness: str,
    kind: str,
) -> Mapping[str, object] | UsageUnverifiable:
    raw_usage = payload.get("usage")
    if raw_usage is None:
        return _unverifiable(
            harness=harness,
            kind=kind,
            reason=UsageUnverifiableReason.MISSING_USAGE,
        )
    if not isinstance(raw_usage, Mapping):
        raise ValueError(f"{harness} {kind} usage must be an object")
    return raw_usage


def _missing_required_fields(
    usage: Mapping[str, object],
    fields: tuple[str, ...],
) -> tuple[str, ...]:
    return tuple(field for field in fields if usage.get(field) is None)


def _parse_codex_usage(
    payload: Mapping[str, object],
    *,
    kind: str,
) -> CodexUsage | UsageUnverifiable:
    if kind == "codex.app_server.turn.completed":
        return _parse_codex_app_server_usage(payload, kind=kind)
    if kind != "turn.completed":
        return _unverifiable(
            harness=FrontierHarness.CODEX.value,
            kind=kind,
            reason=UsageUnverifiableReason.UNSUPPORTED_EVENT,
        )
    raw_usage = _usage_mapping(payload, harness=FrontierHarness.CODEX.value, kind=kind)
    if isinstance(raw_usage, UsageUnverifiable):
        return raw_usage
    required = ("input_tokens", "cached_input_tokens", "output_tokens")
    missing = _missing_required_fields(raw_usage, required)
    if missing:
        return _unverifiable(
            harness=FrontierHarness.CODEX.value,
            kind=kind,
            reason=UsageUnverifiableReason.MISSING_REQUIRED_FIELDS,
            missing_fields=missing,
        )
    input_tokens = _token_count(raw_usage, "input_tokens", provider="codex")
    cached_input_tokens = _token_count(raw_usage, "cached_input_tokens", provider="codex")
    if cached_input_tokens > input_tokens:
        raise ValueError("cached_input_tokens cannot exceed input_tokens")
    cache_write_tokens = raw_usage.get("cache_write_tokens", 0)
    if cache_write_tokens is None:
        return _unverifiable(
            harness=FrontierHarness.CODEX.value,
            kind=kind,
            reason=UsageUnverifiableReason.MISSING_REQUIRED_FIELDS,
            missing_fields=("cache_write_tokens",),
        )
    return CodexUsage(
        tokens=FrontierTurnUsage(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_write_tokens=(
                0
                if "cache_write_tokens" not in raw_usage
                else _token_count(raw_usage, "cache_write_tokens", provider="codex")
            ),
            output_tokens=_token_count(raw_usage, "output_tokens", provider="codex"),
        )
    )


def _parse_codex_app_server_usage(
    payload: Mapping[str, object], *, kind: str
) -> CodexUsage | UsageUnverifiable:
    """Decode the last notification of one fresh, single-turn review thread.

    App-server completion has no usage contract. Only its separate, matching
    tokenUsage notification proves measurement; absence is ordinary unknown
    usage, while malformed reported counters remain a projection diagnostic.
    """

    thread_id, turn_id = payload.get("threadId"), payload.get("turnId")
    if any(not isinstance(value, str) or not value for value in (thread_id, turn_id)):
        raise ValueError("Codex app-server completion requires thread and turn identities")
    notification = payload.get("usage_notification")
    if notification is None:
        return _unverifiable(
            harness=FrontierHarness.CODEX.value,
            kind=kind,
            reason=UsageUnverifiableReason.NOT_REPORTED,
        )
    if not isinstance(notification, Mapping):
        raise ValueError("Codex app-server usage notification must be an object")
    if notification.get("threadId") != thread_id or notification.get("turnId") != turn_id:
        raise ValueError("Codex app-server usage identity differs from completion")
    token_usage = notification.get("tokenUsage")
    if not isinstance(token_usage, Mapping) or not isinstance(token_usage.get("total"), Mapping):
        raise ValueError("Codex app-server usage requires a total object")
    total = token_usage["total"]
    required = ("inputTokens", "cachedInputTokens", "cacheWriteInputTokens", "outputTokens")
    missing = _missing_required_fields(total, required)
    if missing:
        return _unverifiable(
            harness=FrontierHarness.CODEX.value,
            kind=kind,
            reason=UsageUnverifiableReason.MISSING_REQUIRED_FIELDS,
            missing_fields=missing,
        )
    input_tokens, cached_tokens, cache_write_tokens, output_tokens = (
        _token_count(total, field, provider="codex app-server") for field in required
    )
    if cached_tokens + cache_write_tokens > input_tokens:
        raise ValueError("Codex cached and cache-write input cannot exceed total input")
    # Codex reports cache writes inside inputTokens. Our shared schema has a
    # separate cache-write bucket, so split it out rather than count it twice.
    return CodexUsage(
        FrontierTurnUsage(
            input_tokens=input_tokens - cache_write_tokens,
            cached_input_tokens=cached_tokens,
            cache_write_tokens=cache_write_tokens,
            output_tokens=output_tokens,
        )
    )


def _parse_claude_usage(
    payload: Mapping[str, object],
    *,
    kind: str,
) -> ClaudeUsage | UsageUnverifiable:
    if kind != "result":
        return _unverifiable(
            harness=FrontierHarness.CLAUDE.value,
            kind=kind,
            reason=UsageUnverifiableReason.UNSUPPORTED_EVENT,
        )
    raw_usage = _usage_mapping(payload, harness=FrontierHarness.CLAUDE.value, kind=kind)
    if isinstance(raw_usage, UsageUnverifiable):
        return raw_usage
    required = (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
    )
    missing = _missing_required_fields(raw_usage, required)
    if missing:
        return _unverifiable(
            harness=FrontierHarness.CLAUDE.value,
            kind=kind,
            reason=UsageUnverifiableReason.MISSING_REQUIRED_FIELDS,
            missing_fields=missing,
        )
    return ClaudeUsage(
        tokens=FrontierTurnUsage(
            input_tokens=(
                _token_count(raw_usage, "input_tokens", provider="claude")
                + _token_count(raw_usage, "cache_read_input_tokens", provider="claude")
            ),
            cached_input_tokens=_token_count(
                raw_usage, "cache_read_input_tokens", provider="claude"
            ),
            cache_write_tokens=_token_count(
                raw_usage, "cache_creation_input_tokens", provider="claude"
            ),
            output_tokens=_token_count(raw_usage, "output_tokens", provider="claude"),
        ),
        reported_model=_single_reported_claude_model(payload),
    )


def _single_reported_claude_model(payload: Mapping[str, object]) -> str | None:
    raw_model_usage = payload.get("modelUsage")
    if not isinstance(raw_model_usage, Mapping) or len(raw_model_usage) != 1:
        return None
    model, usage = next(iter(raw_model_usage.items()))
    if not isinstance(model, str) or not model.strip() or not isinstance(usage, Mapping):
        return None
    return model


def parse_frontier_usage(
    *,
    harness: str,
    kind: str,
    payload: Mapping[str, object],
) -> FrontierUsageEvidence:
    """Normalize one provider event without estimating absent usage."""

    try:
        provider = FrontierHarness(harness)
    except ValueError:
        return _unverifiable(
            harness=harness,
            kind=kind,
            reason=UsageUnverifiableReason.UNSUPPORTED_HARNESS,
        )
    match provider:
        case FrontierHarness.CODEX:
            return _parse_codex_usage(payload, kind=kind)
        case FrontierHarness.CLAUDE:
            return _parse_claude_usage(payload, kind=kind)
    assert_never(provider)


def parse_frontier_turn_usage(payload: Mapping[str, object]) -> FrontierTurnUsage | None:
    """Preserve the Codex parser contract while using the typed evidence model."""

    evidence = parse_frontier_usage(
        harness=FrontierHarness.CODEX.value,
        kind="turn.completed",
        payload=payload,
    )
    match evidence:
        case CodexUsage(tokens=tokens):
            return tokens
        case UsageUnverifiable():
            return None
        case ClaudeUsage():
            raise AssertionError("Codex parsing produced Claude usage")


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
    if harness not in {member.value for member in FrontierHarness}:
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

    if harness == FrontierHarness.CODEX.value and kind == "thread.started":
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

    evidence = parse_frontier_usage(harness=harness, kind=kind, payload=payload)
    match evidence:
        case UsageUnverifiable(
            reason=(
                UsageUnverifiableReason.MISSING_USAGE
                | UsageUnverifiableReason.MISSING_REQUIRED_FIELDS
            ) as reason,
            missing_fields=missing_fields,
        ):
            detail = f"{harness} {kind} usage is unverifiable: {reason.value}"
            if missing_fields:
                detail += f" ({', '.join(missing_fields)})"
            raise ValueError(detail)
        case UsageUnverifiable():
            return
        case CodexUsage(tokens=usage):
            usage_model = lease.get("model")
        case ClaudeUsage(tokens=usage, reported_model=usage_model):
            pass
        case _ as unreachable:
            assert_never(unreachable)
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
            usage_model,
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
    "ClaudeUsage",
    "CodexUsage",
    "FrontierUsageEvidence",
    "FrontierTurnUsage",
    "UsageUnverifiable",
    "UsageUnverifiableReason",
    "WEIGHT_POLICY",
    "find_compatible_agent_continuation",
    "list_frontier_usage_records",
    "parse_frontier_usage",
    "parse_frontier_turn_usage",
    "project_frontier_event",
]
