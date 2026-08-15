# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import pytest

from local_first_agent_os.contracts import ModelRole, SourceType, WorkflowStatus, WorkspaceId
from local_first_agent_os.directives import DirectiveParser, parse_month_day
from local_first_agent_os.ingress import normalize_prompt_event, normalize_scheduled_event
from local_first_agent_os.pi_channel import normalize_terminal_text, plan_terminal_actions
from local_first_agent_os.workflow import WorkflowEngine


def _directive_event(directive: str):
    return normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": directive},
    )


def test_parse_month_day_normalizes_zero_pad() -> None:
    assert parse_month_day("4/8") == "04/08"
    assert parse_month_day("04/28") == "04/28"
    assert parse_month_day("12/31") == "12/31"
    assert parse_month_day("13/01") is None
    assert parse_month_day("00/01") is None
    assert parse_month_day("garbage") is None


def test_directive_parser_recognizes_send_to_wf_and_done(runtime) -> None:
    parser = DirectiveParser(runtime.settings)
    spec = parser.parse("/send-to-wf /tmp/note.txt 04/28")
    assert spec.action == "send_to_wf"
    assert spec.month_day == "04/28"
    assert spec.path is not None
    assert spec.path.name == "note.txt"

    done_spec = parser.parse("/done what owns workflow truth?")
    assert done_spec.action == "done"
    assert done_spec.query == "what owns workflow truth?"


def test_directive_parser_rejects_send_to_wf_without_date(runtime) -> None:
    parser = DirectiveParser(runtime.settings)
    with pytest.raises(ValueError):
        parser.parse("/send-to-wf /tmp/note.txt")


def test_send_to_wf_text_file_inserts_to_workflowy(runtime, tmp_path: Path) -> None:
    note = tmp_path / "today.txt"
    note.write_text("Pi keeps daily ledger entries.", encoding="utf-8")
    event = _directive_event(f"/send-to-wf {note} 04/28")
    result = WorkflowEngine(runtime).model_directive(event)
    assert result.workflow_type.value == "send_to_workflowy"
    assert result.status == WorkflowStatus.COMPLETED
    artifact_roles = [str(artifact.role) for artifact in result.artifacts]
    assert "send_to_wf_payload" in artifact_roles


def test_send_to_wf_with_missing_file_returns_help(runtime, tmp_path: Path) -> None:
    missing = tmp_path / "missing.txt"
    event = _directive_event(f"/send-to-wf {missing} 04/28")
    result = WorkflowEngine(runtime).model_directive(event)
    assert result.workflow_type.value == "send_to_workflowy"
    assert result.status == WorkflowStatus.FAILED_PERMANENT
    assert result.help is not None


def test_send_to_wf_image_routes_through_classifier(runtime, tmp_path: Path) -> None:
    image = tmp_path / "screen.png"
    image.write_bytes(b"PNG-bytes-but-mock-models-dont-care")
    event = _directive_event(f"/send-to-wf {image} 11/15")
    result = WorkflowEngine(runtime).model_directive(event)
    assert result.workflow_type.value == "send_to_workflowy"
    assert result.status == WorkflowStatus.COMPLETED


def test_send_to_wf_audio_returns_stub_payload(runtime, tmp_path: Path) -> None:
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"id3-mock")
    event = _directive_event(f"/send-to-wf {audio} 12/03")
    result = WorkflowEngine(runtime).model_directive(event)
    assert result.workflow_type.value == "send_to_workflowy"
    assert result.status == WorkflowStatus.COMPLETED


def test_done_directive_returns_aggregated_answer(runtime, tmp_path: Path) -> None:
    note_dir = tmp_path / "notes"
    note_dir.mkdir()
    (note_dir / "a.md").write_text("DBOS owns durable workflow boundaries.", encoding="utf-8")
    (note_dir / "b.md").write_text("Pi orchestrates local agent workflows.", encoding="utf-8")
    WorkflowEngine(runtime).model_directive(_directive_event(f"/store {note_dir}"))
    event = _directive_event("/done workflow boundaries")
    result = WorkflowEngine(runtime).model_directive(event)
    assert result.workflow_type.value == "done_recall"
    assert result.status == WorkflowStatus.COMPLETED


def test_done_directive_without_query_returns_help(runtime) -> None:
    result = WorkflowEngine(runtime).model_directive(_directive_event("/done"))
    assert result.workflow_type.value == "done_recall"
    assert result.status == WorkflowStatus.FAILED_PERMANENT
    assert result.help is not None


def test_terminal_planner_treats_send_to_wf_as_directive(tmp_path: Path) -> None:
    note = tmp_path / "today.txt"
    note.write_text("hi", encoding="utf-8")
    actions = plan_terminal_actions(f"/send-to-wf {note} 04/28")
    assert [action.kind for action in actions] == ["directive"]
    assert "/send-to-wf" in actions[0].text


def test_terminal_planner_treats_done_as_directive() -> None:
    actions = plan_terminal_actions("/done what owns workflow truth?")
    assert [action.kind for action in actions] == ["directive"]
    assert actions[0].text.startswith("/done")


def test_normalize_terminal_text_handles_send_to_wf_and_done() -> None:
    assert normalize_terminal_text("send-to-wf /tmp/x 04/28") == "/send-to-wf /tmp/x 04/28"
    assert normalize_terminal_text("done embeddings") == "/done embeddings"


def test_general_questions_uses_fallback_when_default_unavailable(runtime, tmp_path: Path) -> None:
    note_dir = tmp_path / "notes"
    note_dir.mkdir()
    (note_dir / "a.md").write_text("Pi requires DBOS for durability.", encoding="utf-8")
    WorkflowEngine(runtime).model_directive(_directive_event(f"/store {note_dir}"))
    runtime.model_manager.activate_default_fallback(
        ModelRole.GENERAL_FALLBACK,
        "default model offline; fallback covers queries",
    )
    event = normalize_prompt_event(
        "what does pi require for durability?", workspace_id=WorkspaceId.GENERAL.value
    )
    result = WorkflowEngine(runtime).general_questions(event)
    assert result.status == WorkflowStatus.COMPLETED
    assert result.manual_review_reason is not None
    answer_artifacts = [artifact for artifact in result.artifacts if str(artifact.role) == "answer"]
    assert answer_artifacts


def test_default_fallback_clears_after_general_start(runtime) -> None:
    runtime.model_manager.activate_default_fallback(
        ModelRole.GENERAL_FALLBACK,
        "test",
    )
    assert runtime.model_manager.is_default_fallback_active()
    event = _directive_event("/start /gemma4")
    WorkflowEngine(runtime).model_directive(event)
    assert not runtime.model_manager.is_default_fallback_active()
