# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import hashlib
import json

import pytest

from local_first_agent_os.agent_execution_supervisor import normalize_jsonl_line
from local_first_agent_os.coordination.checkpoints import append_execution_event
from local_first_agent_os.coordination.execution import (
    complete_execution_lease,
    open_execution_lease,
)
from local_first_agent_os.coordination.frontier_usage import (
    WEIGHT_POLICY,
    ClaudeUsage,
    CodexUsage,
    FrontierTurnUsage,
    UsageUnverifiable,
    UsageUnverifiableReason,
    find_compatible_agent_continuation,
    list_frontier_usage_records,
    parse_frontier_usage,
)
from local_first_agent_os.coordination.pow_wows import claim_task, create_pow_wow
from local_first_agent_os.coordination.projects import create_saga
from local_first_agent_os.coordination.store import connect


def _append(
    lease_id: str,
    sequence: int,
    kind: str,
    payload: dict[str, object],
) -> dict[str, object]:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return append_execution_event(
        lease_id,
        sequence,
        float(sequence),
        "stdout",
        kind,
        payload,
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _reader_lease(
    *,
    harness: str = "codex",
    model: str = "gpt-5.6-terra",
) -> tuple[str, str, str, str]:
    saga_id = str(create_saga(f"preserve useful {harness} context")["saga_id"])
    pow_wow_id = str(
        create_pow_wow(saga_id, "IMPLEMENT", "reuse the senior reading", "verified")["pow_wow_id"]
    )
    task_id = str(claim_task(pow_wow_id, "senior_read", "read independently")["task_id"])
    opened = open_execution_lease(
        "reader-lease",
        "test-worker",
        task_id=task_id,
        agent_tier="senior",
        agent_name=harness,
        task_role="independent_reader",
        model=model,
        target_project_id="local_first_agent_os",
        planning_phase="senior_independent_reading",
        source_revision="a" * 40,
        permission_envelope_sha256="b" * 64,
        timeout_seconds=60,
    )
    return saga_id, pow_wow_id, task_id, str(opened["lease"]["lease_id"])


def test_codex_events_project_continuation_and_usage_once() -> None:
    saga_id, pow_wow_id, task_id, lease_id = _reader_lease()
    thread_id = "01a00bac-e60b-7321-8d47-50ee11829924"

    _append(lease_id, 1, "thread.started", {"type": "thread.started", "thread_id": thread_id})
    usage_payload = {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 1_000,
            "cached_input_tokens": 800,
            "output_tokens": 20,
        },
    }
    first = _append(lease_id, 2, "turn.completed", usage_payload)
    replay = _append(lease_id, 2, "turn.completed", usage_payload)
    assert first["created"] is True
    assert replay["created"] is False

    records = list_frontier_usage_records(lease_id)["usage_records"]
    assert len(records) == 1
    assert records[0] == {
        **records[0],
        "thread_id": thread_id,
        "task_id": task_id,
        "pow_wow_id": pow_wow_id,
        "saga_id": saga_id,
        "task_role": "independent_reader",
        "harness": "codex",
        "model": "gpt-5.6-terra",
        "input_tokens": 1_000,
        "cached_input_tokens": 800,
        "uncached_input_tokens": 200,
        "cache_write_tokens": 0,
        "output_tokens": 20,
        "effective_units_milli": 400_000,
        "weight_policy": WEIGHT_POLICY,
        "measured": True,
    }
    with connect() as c:
        pow_wow = c.execute(
            "SELECT consumed_tokens FROM pow_wows WHERE pow_wow_id=?", (pow_wow_id,)
        ).fetchone()
        saga = c.execute("SELECT consumed_tokens FROM sagas WHERE saga_id=?", (saga_id,)).fetchone()
    assert pow_wow["consumed_tokens"] == 1_020
    assert saga["consumed_tokens"] == 1_020

    complete_execution_lease(lease_id, "COMPLETED")
    continuation = find_compatible_agent_continuation(
        task_id,
        pow_wow_id=pow_wow_id,
        harness="codex",
        source_model="gpt-5.6-terra",
        target_project_id="local_first_agent_os",
        source_revision="a" * 40,
    )
    assert continuation["compatible"] is True
    assert continuation["continuation"]["thread_id"] == thread_id


def test_claude_result_projects_cumulative_usage_once() -> None:
    saga_id, pow_wow_id, task_id, lease_id = _reader_lease(
        harness="claude",
        model="claude-opus-5",
    )
    payload = {
        "type": "result",
        "subtype": "success",
        "session_id": "fixture-claude",
        "usage": {
            "input_tokens": 11,
            "cache_creation_input_tokens": 40,
            "cache_read_input_tokens": 100,
            "output_tokens": 7,
        },
        "modelUsage": {
            "claude-opus-5-20260901": {
                "inputTokens": 11,
                "cacheCreationInputTokens": 40,
                "cacheReadInputTokens": 100,
                "outputTokens": 7,
            }
        },
    }

    kind, normalized = normalize_jsonl_line(
        harness="claude",
        source="stdout",
        line=json.dumps(payload).encode(),
    )
    assert kind == "result"

    first = _append(lease_id, 1, kind, normalized)
    replay = _append(lease_id, 1, kind, normalized)
    assert first["created"] is True
    assert replay["created"] is False

    records = list_frontier_usage_records(lease_id)["usage_records"]
    assert len(records) == 1
    assert records[0] == {
        **records[0],
        "thread_id": None,
        "task_id": task_id,
        "pow_wow_id": pow_wow_id,
        "saga_id": saga_id,
        "task_role": "independent_reader",
        "harness": "claude",
        "model": "claude-opus-5-20260901",
        "input_tokens": 111,
        "cached_input_tokens": 100,
        "uncached_input_tokens": 11,
        "cache_write_tokens": 40,
        "output_tokens": 7,
        "effective_units_milli": 113_000,
        "weight_policy": WEIGHT_POLICY,
        "measured": True,
    }
    with connect() as c:
        pow_wow = c.execute(
            "SELECT consumed_tokens FROM pow_wows WHERE pow_wow_id=?", (pow_wow_id,)
        ).fetchone()
        saga = c.execute("SELECT consumed_tokens FROM sagas WHERE saga_id=?", (saga_id,)).fetchone()
    assert pow_wow["consumed_tokens"] == 158
    assert saga["consumed_tokens"] == 158


def test_frontier_usage_evidence_is_provider_typed_and_never_estimated() -> None:
    codex = parse_frontier_usage(
        harness="codex",
        kind="turn.completed",
        payload={
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 4,
                "output_tokens": 3,
            }
        },
    )
    assert isinstance(codex, CodexUsage)
    assert codex.tokens.total_tokens == 13

    claude = parse_frontier_usage(
        harness="claude",
        kind="result",
        payload={
            "usage": {
                "input_tokens": 6,
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 4,
                "output_tokens": 3,
            }
        },
    )
    assert isinstance(claude, ClaudeUsage)
    assert claude.tokens.total_tokens == 18
    assert claude.reported_model is None

    mixed_models = parse_frontier_usage(
        harness="claude",
        kind="result",
        payload={
            "usage": {
                "input_tokens": 6,
                "cache_creation_input_tokens": 5,
                "cache_read_input_tokens": 4,
                "output_tokens": 3,
            },
            "modelUsage": {
                "claude-opus-5": {},
                "claude-sonnet-5": {},
            },
        },
    )
    assert isinstance(mixed_models, ClaudeUsage)
    assert mixed_models.reported_model is None

    absent = parse_frontier_usage(harness="claude", kind="result", payload={})
    assert absent == UsageUnverifiable(
        harness="claude",
        kind="result",
        reason=UsageUnverifiableReason.MISSING_USAGE,
    )
    partial = parse_frontier_usage(
        harness="claude",
        kind="result",
        payload={
            "usage": {
                "input_tokens": 6,
                "cache_creation_input_tokens": 5,
                "output_tokens": 3,
            }
        },
    )
    assert partial == UsageUnverifiable(
        harness="claude",
        kind="result",
        reason=UsageUnverifiableReason.MISSING_REQUIRED_FIELDS,
        missing_fields=("cache_read_input_tokens",),
    )
    with pytest.raises(ValueError, match="cannot exceed"):
        FrontierTurnUsage(
            input_tokens=2,
            cached_input_tokens=3,
            cache_write_tokens=0,
            output_tokens=1,
        )
    with pytest.raises(ValueError, match="requires field names"):
        UsageUnverifiable(
            harness="claude",
            kind="result",
            reason=UsageUnverifiableReason.MISSING_REQUIRED_FIELDS,
        )


def test_unverifiable_claude_usage_is_reported_without_counting_zero() -> None:
    _, pow_wow_id, _, lease_id = _reader_lease(
        harness="claude",
        model="claude-opus-5",
    )
    result = _append(
        lease_id,
        1,
        "result",
        {
            "type": "result",
            "usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 0,
                "output_tokens": 1,
            },
        },
    )

    with connect() as c:
        event_count = c.execute(
            "SELECT COUNT(*) AS count FROM agent_execution_events WHERE lease_id=?",
            (lease_id,),
        ).fetchone()["count"]
        consumed = c.execute(
            "SELECT consumed_tokens FROM pow_wows WHERE pow_wow_id=?",
            (pow_wow_id,),
        ).fetchone()["consumed_tokens"]
    assert "missing_required_fields" in str(result["projection_error"])
    assert "cache_read_input_tokens" in str(result["projection_error"])
    assert event_count == 1
    assert consumed == 0
    assert list_frontier_usage_records(lease_id)["usage_records"] == []


def test_invalid_usage_preserves_the_raw_event_and_reports_projection_failure() -> None:
    _, _, _, lease_id = _reader_lease()

    result = _append(
        lease_id,
        1,
        "turn.completed",
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 2,
                "cached_input_tokens": 3,
                "output_tokens": 1,
            },
        },
    )

    with connect() as c:
        event_count = c.execute(
            "SELECT COUNT(*) AS count FROM agent_execution_events WHERE lease_id=?",
            (lease_id,),
        ).fetchone()["count"]
    assert event_count == 1
    assert "cannot exceed" in str(result["projection_error"])
    assert list_frontier_usage_records(lease_id)["usage_records"] == []


def test_continuation_lookup_fails_closed_on_revision_mismatch() -> None:
    _, pow_wow_id, task_id, lease_id = _reader_lease()
    _append(
        lease_id,
        1,
        "thread.started",
        {"type": "thread.started", "thread_id": "01a00bac-e60b-7321-8d47-50ee11829924"},
    )
    complete_execution_lease(lease_id, "COMPLETED")

    result = find_compatible_agent_continuation(
        task_id,
        pow_wow_id=pow_wow_id,
        harness="codex",
        source_model="gpt-5.6-terra",
        target_project_id="local_first_agent_os",
        source_revision="c" * 40,
    )

    assert result["compatible"] is False
    assert result["reason"] == "source_revision_mismatch"
