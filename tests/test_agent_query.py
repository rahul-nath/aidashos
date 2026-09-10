# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Coverage for `/claude` and `/codex`: direct frontier-CLI queries.

The property under test throughout is that a question is recorded but its answer
is not copied. The record must always say where the transcript is and how sure
that pointer is, because a Codex pointer is a timestamp guess and a Claude
pointer is an exact session id, and a reader cannot tell them apart later
without being told.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from local_first_agent_os.agent_adapters import (
    AgentResult,
    AgentRuntimeKind,
    AgentTask,
    CliRunProvenance,
    runtime_kind_for_harness,
)
from local_first_agent_os.agent_query import (
    AGENT_QUERY_RECORD_SCHEMA,
    TranscriptResolution,
    _resolve_claude_transcript,
    _resolve_codex_transcript,
    agent_query_request,
    build_agent_query_record,
    claude_project_slug,
    configured_agent_query_model,
)
from local_first_agent_os.contracts import AgentHarness, SourceType, WorkflowStatus, WorkspaceId
from local_first_agent_os.directives import DirectiveParser
from local_first_agent_os.ingress import normalize_scheduled_event
from local_first_agent_os.settings import Settings
from local_first_agent_os.workflow import WorkflowEngine

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("directive", "expected_harness", "expected_alias"),
    [
        ("/claude explain the ledger", AgentHarness.CLAUDE_CODE, "/claude"),
        ("/cc explain the ledger", AgentHarness.CLAUDE_CODE, "/cc"),
        ("/codex explain the ledger", AgentHarness.CODEX_CLI, "/codex"),
    ],
)
def test_aliases_route_to_their_harness(
    directive: str,
    expected_harness: AgentHarness,
    expected_alias: str,
) -> None:
    spec = DirectiveParser(Settings()).parse(directive)

    assert spec.action == "agent_query"
    assert spec.agent_harness is expected_harness
    assert spec.alias == expected_alias
    assert spec.query == "explain the ledger"


def test_harness_maps_exhaustively_to_a_runtime_kind() -> None:
    assert runtime_kind_for_harness(AgentHarness.CLAUDE_CODE) is AgentRuntimeKind.CLAUDE_CODE
    assert runtime_kind_for_harness(AgentHarness.CODEX_CLI) is AgentRuntimeKind.CODEX_CLI


def test_direct_query_model_comes_from_the_unique_seated_vendor_slot(tmp_path: Path) -> None:
    (tmp_path / "staffing.toml").write_text(
        'seated_pairing = "normal"\n'
        "[pairings.normal]\n"
        "[pairings.normal.senior]\n"
        'harness = "codex"\nmodel = "codex-pinned"\ncapacity = 1\n'
        "[pairings.normal.staff]\n"
        'harness = "claude"\nmodel = "claude-pinned"\ncapacity = 1\n',
        encoding="utf-8",
    )

    assert (
        configured_agent_query_model(AgentHarness.CODEX_CLI, config_dir=tmp_path) == "codex-pinned"
    )
    assert (
        configured_agent_query_model(AgentHarness.CLAUDE_CODE, config_dir=tmp_path)
        == "claude-pinned"
    )


def test_direct_query_model_refuses_two_different_models_on_one_vendor(tmp_path: Path) -> None:
    (tmp_path / "staffing.toml").write_text(
        'seated_pairing = "claude-only"\n'
        "[pairings.claude-only]\n"
        "[pairings.claude-only.senior]\n"
        'harness = "claude"\nmodel = "claude-one"\ncapacity = 1\n'
        "[pairings.claude-only.staff]\n"
        'harness = "claude"\nmodel = "claude-two"\ncapacity = 1\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="exactly one explicit model"):
        configured_agent_query_model(AgentHarness.CLAUDE_CODE, config_dir=tmp_path)


def test_astra_sol_pair_keeps_an_explicit_direct_query_model() -> None:
    config_dir = Path(__file__).resolve().parents[1] / "configs"
    assert (
        configured_agent_query_model(AgentHarness.CODEX_CLI, config_dir=config_dir) == "gpt-5.6-sol"
    )


@pytest.mark.parametrize("directive", ["/claude", "/cc", "/codex", "/claude    "])
def test_empty_query_is_rejected_at_parse_time(directive: str) -> None:
    with pytest.raises(ValueError, match="requires a query"):
        DirectiveParser(Settings()).parse(directive)


def test_every_alias_is_a_recognized_top_level_directive() -> None:
    """The drift guard: an alias the parser accepts but help calls unknown."""

    from local_first_agent_os.directives import AGENT_QUERY_ALIASES, TOP_LEVEL_DIRECTIVES

    assert set(AGENT_QUERY_ALIASES) <= TOP_LEVEL_DIRECTIVES


@pytest.mark.parametrize("directive", ["/claude", "/cc", "/codex"])
def test_help_for_a_query_less_alias_does_not_disown_it(runtime, directive: str) -> None:
    """Regression: help used to answer `not a recognized directive` and then
    print that same directive's usage in the next sentence."""

    from local_first_agent_os.directives_help import explain_failure

    parser = DirectiveParser(runtime.settings)
    try:
        parser.parse(directive)
    except ValueError as exc:
        block = explain_failure(parser, directive, str(exc))
    else:  # pragma: no cover - the parser must reject a query-less alias
        pytest.fail(f"{directive} should not parse without a query")

    assert "not a recognized directive" not in block.summary
    assert "requires a question" in block.summary
    assert any("/claude" in example for example in block.canonical_examples)


def test_query_keeps_internal_whitespace_and_flags() -> None:
    """A question is prose, not an argv: nothing in the tail is a parser token."""

    spec = DirectiveParser(Settings()).parse("/claude why does --dry-run skip the lease?")

    assert spec.query == "why does --dry-run skip the lease?"


# ---------------------------------------------------------------------------
# Transcript resolution
# ---------------------------------------------------------------------------


def test_claude_project_slug_matches_the_real_layout() -> None:
    """Pinned against a directory that exists on disk for this very repo."""

    assert (
        claude_project_slug("/Users/rahul/ai_projects/local_first_agent_os")
        == "-Users-rahul-ai-projects-local-first-agent-os"
    )


def test_claude_pointer_is_exact_when_the_transcript_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cwd = "/Users/rahul/projects/thing"
    transcript_dir = tmp_path / claude_project_slug(cwd)
    transcript_dir.mkdir(parents=True)
    transcript = transcript_dir / "abc-123.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr("local_first_agent_os.agent_query.CLAUDE_TRANSCRIPT_ROOT", tmp_path)

    pointer = _resolve_claude_transcript(session_id="abc-123", cwd=cwd)

    assert pointer.resolution is TranscriptResolution.EXACT_SESSION_ID
    assert pointer.path == str(transcript)
    assert pointer.session_id == "abc-123"


def test_claude_pointer_is_unresolved_without_a_session_id() -> None:
    pointer = _resolve_claude_transcript(session_id=None, cwd="/anywhere")

    assert pointer.resolution is TranscriptResolution.UNRESOLVED
    assert pointer.path is None


def test_claude_keeps_the_session_id_when_the_file_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An id without a file is still the best lead available, so do not drop it."""

    monkeypatch.setattr("local_first_agent_os.agent_query.CLAUDE_TRANSCRIPT_ROOT", tmp_path)

    pointer = _resolve_claude_transcript(session_id="abc-123", cwd="/anywhere")

    assert pointer.resolution is TranscriptResolution.UNRESOLVED
    assert pointer.path is None
    assert pointer.session_id == "abc-123"


def test_codex_pointer_picks_the_newest_rollout_after_the_run_began(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day = tmp_path / "2026" / "07" / "27"
    day.mkdir(parents=True)
    stale = day / "rollout-2026-07-27T09-00-00-aaaa.jsonl"
    fresh = day / "rollout-2026-07-27T11-00-00-bbbb.jsonl"
    newest = day / "rollout-2026-07-27T12-00-00-cccc.jsonl"
    for path in (stale, fresh, newest):
        path.write_text("{}\n", encoding="utf-8")
    os.utime(stale, (1000.0, 1000.0))
    os.utime(fresh, (3000.0, 3000.0))
    os.utime(newest, (4000.0, 4000.0))
    monkeypatch.setattr("local_first_agent_os.agent_query.CODEX_TRANSCRIPT_ROOT", tmp_path)

    pointer = _resolve_codex_transcript(started_at_epoch=2000.0)

    assert pointer.resolution is TranscriptResolution.NEWEST_AFTER_START
    assert pointer.path == str(newest)
    assert pointer.session_id is None


def test_codex_pointer_is_unresolved_when_nothing_was_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    day = tmp_path / "2026" / "07" / "27"
    day.mkdir(parents=True)
    stale = day / "rollout-2026-07-27T09-00-00-aaaa.jsonl"
    stale.write_text("{}\n", encoding="utf-8")
    os.utime(stale, (1000.0, 1000.0))
    monkeypatch.setattr("local_first_agent_os.agent_query.CODEX_TRANSCRIPT_ROOT", tmp_path)

    pointer = _resolve_codex_transcript(started_at_epoch=2000.0)

    assert pointer.resolution is TranscriptResolution.UNRESOLVED
    assert pointer.path is None


# ---------------------------------------------------------------------------
# Adapter session capture
# ---------------------------------------------------------------------------


def test_claude_adapter_carries_the_session_id_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without this the Claude pointer can never be exact."""

    from local_first_agent_os.agent_adapters import ClaudeCodeAdapter

    payload = json.dumps(
        {"result": "the answer", "session_id": "sess-42", "usage": {"output_tokens": 7}}
    ).encode()
    _stub_subprocess(monkeypatch, stdout=payload, returncode=0)

    result = _run_adapter(ClaudeCodeAdapter(cwd="/tmp", model="claude-test"))

    assert isinstance(result.provenance, CliRunProvenance)
    assert result.provenance.session_id == "sess-42"
    assert result.output == "the answer"
    assert result.tokens_used == 7


def test_claude_adapter_survives_output_that_is_not_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from local_first_agent_os.agent_adapters import ClaudeCodeAdapter

    _stub_subprocess(monkeypatch, stdout=b"plain text answer", returncode=0)

    result = _run_adapter(ClaudeCodeAdapter(cwd="/tmp", model="claude-test"))

    assert result.output == "plain text answer"
    assert isinstance(result.provenance, CliRunProvenance)
    assert result.provenance.session_id is None


def test_codex_adapter_uses_exec_json_and_captures_the_thread_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from local_first_agent_os.agent_adapters import CodexCLIAdapter

    payload = b"\n".join(
        (
            b'{"type":"thread.started","thread_id":"thread-42"}',
            b'{"type":"item.completed","item":{"type":"agent_message","text":"answer"}}',
            b'{"type":"turn.completed","usage":{"output_tokens":9}}',
        )
    )
    _stub_subprocess(monkeypatch, stdout=payload, returncode=0)
    adapter = CodexCLIAdapter(cwd="/tmp", model="codex-test")

    result = _run_adapter(adapter)

    assert adapter.command_for(AgentTask("t", "question"))[:2] == ("codex", "exec")
    assert "--json" in adapter.command_for(AgentTask("t", "question"))
    assert "--quiet" not in adapter.command_for(AgentTask("t", "question"))
    assert result.output == "answer"
    assert result.tokens_used == 9
    assert isinstance(result.provenance, CliRunProvenance)
    assert result.provenance.session_id == "thread-42"


# ---------------------------------------------------------------------------
# Record shape
# ---------------------------------------------------------------------------


def test_record_holds_the_question_and_the_pointer_but_not_the_answer() -> None:
    request = agent_query_request(
        workflow_id="wf-1",
        harness=AgentHarness.CLAUDE_CODE,
        model="claude-test",
        alias="/claude",
        query="explain the ledger",
        cwd="/repo",
    )
    run = {
        "succeeded": True,
        "output": "a long answer body",
        "error": None,
        "tokens_used": 12,
        "provenance": {
            "runtime": "claude_code",
            "model": "claude-test",
            "session_id": "s",
        },
    }
    transcript = {"resolution": "exact_session_id", "path": "/t.jsonl", "session_id": "s"}

    record = build_agent_query_record({**request, **run, "transcript": transcript})

    assert record["schema_version"] == AGENT_QUERY_RECORD_SCHEMA
    assert record["harness"] == "claude_code"
    assert record["model"] == "claude-test"
    assert record["alias"] == "/claude"
    assert record["query"] == "explain the ledger"
    assert record["succeeded"] is True
    assert record["tokens_used"] == 12
    assert record["transcript"] == transcript
    assert "a long answer body" not in json.dumps(record)


# ---------------------------------------------------------------------------
# Workflow, end to end
# ---------------------------------------------------------------------------


def _directive_event(directive: str):
    return normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": directive},
    )


def _stub_claude_adapter(
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_id: str | None = "sess-e2e",
    success: bool = True,
    error: str | None = None,
) -> dict[str, Any]:
    """Replace only the process boundary, so the real steps still run."""

    seen: dict[str, Any] = {}

    class StubAdapter:
        def __init__(self, **kwargs: Any) -> None:
            seen["adapter_kwargs"] = kwargs

        async def run(self, task: AgentTask) -> AgentResult:
            seen["task"] = task
            return AgentResult(
                task_id=task.task_id,
                success=success,
                output="an answer that must not be persisted",
                tokens_used=5,
                error=error,
                provenance=CliRunProvenance(
                    AgentRuntimeKind.CLAUDE_CODE,
                    str(seen["adapter_kwargs"]["model"]),
                    session_id,
                ),
            )

    monkeypatch.setattr("local_first_agent_os.agent_adapters.ClaudeCodeAdapter", StubAdapter)
    monkeypatch.setattr(
        "local_first_agent_os.workflow.models.configured_agent_query_model",
        lambda *_args, **_kwargs: "claude-test",
    )
    return seen


def _stub_codex_adapter(
    monkeypatch: pytest.MonkeyPatch,
    *,
    writes_rollout: Path | None = None,
) -> dict[str, Any]:
    """Codex returns no session id, which is why its pointer is a guess.

    The rollout file is written from inside `run`, because that ordering is the
    thing under test: a file that predates the run is not this run's transcript.
    """

    seen: dict[str, Any] = {}

    class StubAdapter:
        def __init__(self, **kwargs: Any) -> None:
            seen["adapter_kwargs"] = kwargs

        async def run(self, task: AgentTask) -> AgentResult:
            seen["task"] = task
            if writes_rollout is not None:
                writes_rollout.parent.mkdir(parents=True, exist_ok=True)
                writes_rollout.write_text("{}\n", encoding="utf-8")
            return AgentResult(
                task_id=task.task_id,
                success=True,
                output="an answer that must not be persisted",
                tokens_used=3,
                provenance=CliRunProvenance(
                    AgentRuntimeKind.CODEX_CLI,
                    str(seen["adapter_kwargs"]["model"]),
                ),
            )

    monkeypatch.setattr("local_first_agent_os.agent_adapters.CodexCLIAdapter", StubAdapter)
    monkeypatch.setattr(
        "local_first_agent_os.workflow.models.configured_agent_query_model",
        lambda *_args, **_kwargs: "codex-test",
    )
    return seen


def test_build_adapter_routes_each_harness_to_its_own_cli() -> None:
    """The whole point of the enum: no second lookup table to fall out of sync."""

    from local_first_agent_os.agent_adapters import ClaudeCodeAdapter, CodexCLIAdapter
    from local_first_agent_os.agent_query import _build_adapter

    claude = _build_adapter(AgentHarness.CLAUDE_CODE, cwd="/repo", model="claude-test")
    codex = _build_adapter(AgentHarness.CODEX_CLI, cwd="/repo", model="codex-test")

    assert isinstance(claude, ClaudeCodeAdapter)
    assert isinstance(codex, CodexCLIAdapter)
    assert claude.cwd == Path("/repo")
    assert codex.cwd == Path("/repo")
    assert claude.model == "claude-test"
    assert codex.model == "codex-test"


def test_codex_query_records_a_guessed_pointer_as_a_guess(
    runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end for `/codex`, including the step that picks the rollout file."""

    rollout = tmp_path / "2026" / "07" / "27" / "rollout-2026-07-27T12-00-00-cccc.jsonl"
    stale = tmp_path / "2026" / "07" / "26" / "rollout-2026-07-26T09-00-00-aaaa.jsonl"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}\n", encoding="utf-8")
    os.utime(stale, (1000.0, 1000.0))
    seen = _stub_codex_adapter(monkeypatch, writes_rollout=rollout)
    monkeypatch.setattr("local_first_agent_os.agent_query.CODEX_TRANSCRIPT_ROOT", tmp_path)

    result = WorkflowEngine(runtime).agent_query(_directive_event("/codex explain the ledger"))

    assert result.status == WorkflowStatus.COMPLETED
    assert seen["task"].prompt == "explain the ledger"
    record_ref = next(
        artifact for artifact in result.artifacts if str(artifact.role) == "agent_query_record"
    )
    record = runtime.artifact_store.read_json(record_ref.artifact_id)
    assert record["harness"] == "codex_cli"
    assert record["alias"] == "/codex"
    assert record["transcript"] == {
        "resolution": "newest_after_start",
        "path": str(rollout),
        "session_id": None,
    }
    assert "an answer that must not be persisted" not in json.dumps(record)


def test_agent_query_records_the_pointer_and_never_the_answer(
    runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _stub_claude_adapter(monkeypatch)
    transcript_root = tmp_path / "claude-projects"
    transcript_dir = transcript_root / claude_project_slug(os.getcwd())
    transcript_dir.mkdir(parents=True)
    transcript = transcript_dir / "sess-e2e.jsonl"
    transcript.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr("local_first_agent_os.agent_query.CLAUDE_TRANSCRIPT_ROOT", transcript_root)

    result = WorkflowEngine(runtime).agent_query(_directive_event("/claude explain the ledger"))

    assert result.status == WorkflowStatus.COMPLETED
    assert seen["task"].prompt == "explain the ledger"
    record_ref = next(
        artifact for artifact in result.artifacts if str(artifact.role) == "agent_query_record"
    )
    record = runtime.artifact_store.read_json(record_ref.artifact_id)
    assert record["schema_version"] == AGENT_QUERY_RECORD_SCHEMA
    assert record["harness"] == "claude_code"
    assert record["query"] == "explain the ledger"
    assert record["succeeded"] is True
    assert record["transcript"] == {
        "resolution": "exact_session_id",
        "path": str(transcript),
        "session_id": "sess-e2e",
    }
    assert "an answer that must not be persisted" not in json.dumps(record)


def test_agent_query_completes_with_an_unresolved_pointer(
    runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing transcript is not a failed query; the question still happened."""

    _stub_claude_adapter(monkeypatch, session_id=None)
    monkeypatch.setattr("local_first_agent_os.agent_query.CLAUDE_TRANSCRIPT_ROOT", tmp_path)

    result = WorkflowEngine(runtime).agent_query(_directive_event("/cc explain the ledger"))

    assert result.status == WorkflowStatus.COMPLETED
    record_ref = next(
        artifact for artifact in result.artifacts if str(artifact.role) == "agent_query_record"
    )
    record = runtime.artifact_store.read_json(record_ref.artifact_id)
    assert record["transcript"]["resolution"] == "unresolved"
    assert record["transcript"]["path"] is None
    assert record["alias"] == "/cc"


def test_failed_harness_run_is_recorded_as_a_permanent_failure(
    runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_claude_adapter(monkeypatch, success=False, error="claude exited 1")
    monkeypatch.setattr("local_first_agent_os.agent_query.CLAUDE_TRANSCRIPT_ROOT", tmp_path)

    result = WorkflowEngine(runtime).agent_query(_directive_event("/claude explain the ledger"))

    assert result.status == WorkflowStatus.FAILED_PERMANENT
    record_ref = next(
        artifact for artifact in result.artifacts if str(artifact.role) == "agent_query_record"
    )
    record = runtime.artifact_store.read_json(record_ref.artifact_id)
    assert record["succeeded"] is False
    assert record["error"] == "claude exited 1"


def test_unparseable_directive_still_leaves_a_record(runtime) -> None:
    """The failure record is the only evidence the operator asked anything."""

    result = WorkflowEngine(runtime).agent_query(_directive_event("/claude"))

    assert result.status == WorkflowStatus.FAILED_PERMANENT
    record_ref = next(
        artifact for artifact in result.artifacts if str(artifact.role) == "agent_query_record"
    )
    record = runtime.artifact_store.read_json(record_ref.artifact_id)
    assert record["status"] == "failed"
    assert record["directive"] == "/claude"
    assert "requires a query" in record["error"]


def test_directive_dispatch_reaches_agent_query(
    runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The alias must survive the engine's action dispatch, not just the parser."""

    _stub_claude_adapter(monkeypatch, session_id=None)
    monkeypatch.setattr("local_first_agent_os.agent_query.CLAUDE_TRANSCRIPT_ROOT", tmp_path)

    result = WorkflowEngine(runtime).model_directive(_directive_event("/claude explain the ledger"))

    assert result.status == WorkflowStatus.COMPLETED
    assert any(str(artifact.role) == "agent_query_record" for artifact in result.artifacts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_subprocess(monkeypatch: pytest.MonkeyPatch, *, stdout: bytes, returncode: int) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = returncode

        async def communicate(self) -> tuple[bytes, bytes]:
            return stdout, b""

    async def fake_exec(*_args: Any, **_kwargs: Any) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)


def _run_adapter(adapter: Any) -> AgentResult:
    import asyncio

    task = AgentTask(
        task_id="t-1",
        prompt="explain the ledger",
        timeout_seconds=30,
    )
    return asyncio.run(adapter.run(task))
