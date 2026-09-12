# SPDX-License-Identifier: AGPL-3.0-or-later
"""Real JSONL/RPC transport into usage projection, without a model or sandbox claim."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from local_first_agent_os import codex_review_client
from local_first_agent_os.capabilities import Capability
from local_first_agent_os.codex_review_client import LocalFixtureModel, run_read_only_review
from local_first_agent_os.codex_stdio_relay import CodeModeEndpoint
from local_first_agent_os.codex_tool_worker import CodexToolWorker
from local_first_agent_os.coordination.checkpoints import (
    append_execution_event,
    list_execution_events,
)
from local_first_agent_os.coordination.execution import open_execution_lease
from local_first_agent_os.coordination.frontier_usage import (
    CodexUsage,
    UsageUnverifiable,
    UsageUnverifiableReason,
    list_frontier_usage_records,
    parse_frontier_usage,
)
from local_first_agent_os.execution_events import execution_payload_hash, normalize_jsonl_line
from local_first_agent_os.pow_wow.process import extract_agent_cli_output
from local_first_agent_os.process_containment import ProcessContainmentUnavailable
from local_first_agent_os.spawn_authority import SpawnAuthority

_COMPLETION = "codex.app_server.turn.completed"
_THREAD = "review-thread"
_TURN = "review-turn"


def _usage(input_tokens: int, cached: int, writes: int, output: int) -> dict[str, Any]:
    # Generated codex-cli 0.153.4 app-server declarations: TokenUsageBreakdown,
    # ThreadTokenUsage and ThreadTokenUsageUpdatedNotification.
    total = {
        "inputTokens": input_tokens,
        "cachedInputTokens": cached,
        "cacheWriteInputTokens": writes,
        "outputTokens": output,
        "reasoningOutputTokens": 0,
        "totalTokens": input_tokens + output,
    }
    return {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": _THREAD,
            "turnId": _TURN,
            "tokenUsage": {
                "total": total,
                # `last` is one model request, deliberately not the total.
                "last": {key: 0 for key in total},
                "modelContextWindow": None,
            },
        },
    }


def _complete(**overrides: Any) -> dict[str, Any]:
    return {
        "method": "turn/completed",
        "params": {
            "threadId": _THREAD,
            "turn": {"id": _TURN, "status": "completed", "items": []},
            **overrides,
        },
    }


@pytest.fixture
def protocol_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep actual client RPC, process reap and JSONL; substitute only external peers."""

    state = SimpleNamespace(host_closed=False, emitted=[])
    peer = tmp_path / "app_server_peer.py"
    transcript = tmp_path / "notifications.json"
    peer.write_text(
        "import json, sys\n"
        "messages = json.load(open(sys.argv[1]))\n"
        "def send(value):\n"
        "    print(json.dumps(value), flush=True)\n"
        "for line in sys.stdin:\n"
        "    request = json.loads(line)\n"
        "    if 'id' not in request: continue\n"
        "    method = request['method']\n"
        "    result = {}\n"
        "    if method == 'thread/start':\n"
        "        assert request['params']['ephemeral'] is True\n"
        f"        result = {{'thread': {{'id': {_THREAD!r}}}}}\n"
        "    if method == 'turn/start':\n"
        f"        assert request['params']['threadId'] == {_THREAD!r}\n"
        f"        result = {{'turn': {{'id': {_TURN!r}, 'status': 'inProgress'}}}}\n"
        "    send({'id': request['id'], 'result': result})\n"
        "    if method == 'turn/start':\n"
        "        for message in messages: send(message)\n"
    )

    class FixtureCodeHost:
        endpoint = CodeModeEndpoint(Path("/tmp/unused-code-host"), "x" * 43)

        def __init__(self, *args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            state.host_closed = True

        def require_alive(self):
            assert not state.host_closed

    async def ready():
        assert not state.host_closed

    worker = cast(
        CodexToolWorker,
        SimpleNamespace(
            boundary=SimpleNamespace(repository=tmp_path),
            policy=SimpleNamespace(
                authority=SpawnAuthority.of((Capability.INVOKE_MODEL, Capability.READ_REPOSITORY))
            ),
            url="ws://unused-read-worker",
            require_ready=ready,
            assert_alive=lambda: None,
        ),
    )
    monkeypatch.setattr(codex_review_client, "CodexCodeModeHost", FixtureCodeHost)
    monkeypatch.setattr(
        codex_review_client,
        "_client_command",
        lambda *args, **kwargs: [sys.executable, "-I", "-u", str(peer), str(transcript)],
    )

    def emit(event: Mapping[str, Any]):
        if event["type"] == _COMPLETION:
            assert state.host_closed, "completion preceded client and interpreter cleanup"
        state.emitted.append(dict(event))

    def run(notifications: list[dict[str, Any]]) -> list[dict[str, Any]]:
        transcript.write_text(
            json.dumps(
                [
                    {
                        "method": "item/completed",
                        "params": {"item": {"type": "agentMessage", "text": "APPROVE"}},
                    },
                    *notifications,
                ]
            )
        )
        assert (
            asyncio.run(
                run_read_only_review(
                    worker=worker,
                    codex_bin=sys.executable,
                    repository=tmp_path,
                    model=LocalFixtureModel("fixture", "http://127.0.0.1:1"),
                    prompt="Review the supplied evidence.",
                    emit=emit,
                )
            )
            == "APPROVE"
        )
        assert state.host_closed
        return state.emitted

    return run, state


@pytest.mark.parametrize("reported", ["cumulative", "absent", "explicit-zero", "partial"])
def test_review_protocol_completion_projects_only_reported_usage(protocol_review, reported: str):
    run, _state = protocol_review
    updates = []
    if reported == "cumulative":
        updates = [_usage(60, 40, 5, 3), _usage(120, 80, 10, 7), _usage(120, 80, 10, 7)]
    elif reported == "explicit-zero":
        updates = [_usage(0, 0, 0, 0)]
    elif reported == "partial":
        updates = [_usage(120, 80, 10, 7)]
        del updates[0]["params"]["tokenUsage"]["total"]["cacheWriteInputTokens"]
    events = run([*updates, _complete()])
    assert [event["type"] for event in events] == [
        "codex.local_stdio.prepared",
        "thread.started",
        "item.completed",
        _COMPLETION,
    ]
    assert extract_agent_cli_output("\n".join(map(json.dumps, events))) == "APPROVE"
    completion = events[-1]
    assert completion["usage_notification"] == (updates[-1]["params"] if updates else None)
    evidence = parse_frontier_usage(harness="codex", kind=_COMPLETION, payload=completion)
    lease_id = open_execution_lease(
        "review-usage", "fixture", agent_name="codex", model="fixture", timeout_seconds=60
    )["lease"]["lease_id"]
    results = []
    for sequence, event in enumerate(events, 1):
        kind, normalized = normalize_jsonl_line(
            harness="codex", source="stdout", line=json.dumps(event).encode()
        )
        results.append(
            append_execution_event(
                lease_id,
                sequence,
                float(sequence),
                "stdout",
                kind,
                normalized,
                execution_payload_hash(normalized),
            )
        )
    records = list_frontier_usage_records(lease_id)["usage_records"]
    if reported in {"absent", "partial"}:
        assert isinstance(evidence, UsageUnverifiable)
        assert not records
        if reported == "absent":
            assert evidence.reason is UsageUnverifiableReason.NOT_REPORTED
            assert all(result["projection_error"] is None for result in results)
        else:
            assert evidence.reason is UsageUnverifiableReason.MISSING_REQUIRED_FIELDS
            assert "cacheWriteInputTokens" in results[-1]["projection_error"]
    else:
        assert isinstance(evidence, CodexUsage)
        assert all(result["projection_error"] is None for result in results)
        assert len(records) == 1
        row = records[0]
        assert row["measured"] is True
        assert row["thread_id"] == _THREAD
        assert row["input_tokens"] == (110 if reported == "cumulative" else 0)
        assert row["cached_input_tokens"] == (80 if reported == "cumulative" else 0)
        assert row["cache_write_tokens"] == (10 if reported == "cumulative" else 0)
        assert row["output_tokens"] == (7 if reported == "cumulative" else 0)
        assert evidence.tokens.total_tokens == (127 if reported == "cumulative" else 0)
    retained = list_execution_events(lease_id)["events"]
    assert retained[-1]["kind"] == _COMPLETION
    assert retained[-1]["payload"] == completion


@pytest.mark.parametrize(
    "event,field",
    [
        ("usage", "threadId"),
        ("usage", "turnId"),
        ("completion", "threadId"),
        ("completion", "turn"),
    ],
)
def test_review_rejects_foreign_usage_or_completion_identity(
    protocol_review, event: str, field: str
):
    run, state = protocol_review
    notification = _usage(120, 80, 10, 7) if event == "usage" else _complete()
    notification["params"][field] = (
        {"id": "foreign", "status": "completed"} if field == "turn" else "foreign"
    )
    with pytest.raises(ProcessContainmentUnavailable, match="identity differs"):
        run([notification, _complete()])
    assert state.host_closed
    assert not any(event["type"] == _COMPLETION for event in state.emitted)


def test_cli_completion_still_requires_its_promised_usage():
    evidence = parse_frontier_usage(harness="codex", kind="turn.completed", payload={})
    assert isinstance(evidence, UsageUnverifiable)
    assert evidence.reason is UsageUnverifiableReason.MISSING_USAGE


@pytest.mark.parametrize("value", [True, -1, "120"])
def test_reported_app_server_counters_are_not_coerced(value: object):
    notification = _usage(120, 80, 10, 7)["params"]
    notification["tokenUsage"]["total"]["inputTokens"] = value
    with pytest.raises(ValueError, match="non-negative integer"):
        parse_frontier_usage(
            harness="codex",
            kind=_COMPLETION,
            payload={"threadId": _THREAD, "turnId": _TURN, "usage_notification": notification},
        )
