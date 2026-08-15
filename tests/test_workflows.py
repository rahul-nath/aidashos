# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import base64
import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from local_first_agent_os import pi_channel
from local_first_agent_os.contracts import (
    ModelRole,
    SourceType,
    WorkflowResult,
    WorkflowStatus,
    WorkspaceId,
)
from local_first_agent_os.coordination import AmendSagaMilestone
from local_first_agent_os.coordination.integration_queue import read_integration_requests
from local_first_agent_os.coordination.store import connect, tx
from local_first_agent_os.db import ModelInvocationRow
from local_first_agent_os.directives import DirectiveParser
from local_first_agent_os.engineering_doctrine import CURRENT_ENGINEERING_DOCTRINE
from local_first_agent_os.ingress import normalize_prompt_event, normalize_scheduled_event
from local_first_agent_os.model_manager import ModelNotLoadedError
from local_first_agent_os.new_project_intake import create_sparse_gawd_draft_file
from local_first_agent_os.pi_channel import (
    normalize_terminal_text,
    plan_terminal_actions,
    render_terminal_result,
    run_terminal_query,
)
from local_first_agent_os.pow_wow import run_coordination_command
from local_first_agent_os.session_memory import SessionMemoryStore
from local_first_agent_os.settings import get_settings
from local_first_agent_os.workflow import WorkflowEngine
from local_first_agent_os.workflow.engine import _build_no_ready_milestone_guidance
from local_first_agent_os.workflow.knowledge import KnowledgeWorkflowMixin
from local_first_agent_os.workflow.saga_support import (
    build_approved_gawd_dispatch_prompt,
    find_existing_dispatch_intent_for_source,
)


def stub_session_memory(monkeypatch, context: str = "") -> None:
    class FakeSessionClient:
        def __init__(self, _settings):
            pass

        def get_context(self, *, session_id, model_id):
            return context

        def begin_turn(self, **kwargs):
            return {"turn_id": "turn:test", "created_at": "2026-01-01T00:00:00+00:00"}

        def complete_turn(self, **kwargs):
            return None

        def set_context(self, **kwargs):
            return None

    monkeypatch.setattr(pi_channel, "ensure_session_daemon", lambda _settings: None)
    monkeypatch.setattr(pi_channel, "SessionDaemonClient", FakeSessionClient)


def write_linked_projects_config(config_dir: Path, target_path: Path) -> None:
    (config_dir / "linked_projects.toml").write_text(
        f"""
[center]
id = "local_first_agent_os"
description = "test center"
control_plane_project = "target"
default_saga_project = "target"
default_memory_project = "target"

[[projects]]
id = "target"
kind = "test_repo"
path = "{target_path}"
status = "active"
read_only = false
description = "test target"
primary_interfaces = ["pytest"]
owns = ["tests"]
avoid = []
verification_commands = ["true"]
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_pi_workflow_runner_uses_durable_runner_when_dbos_enabled(monkeypatch) -> None:
    from local_first_agent_os import dbos_app

    event = normalize_prompt_event("hello")
    captured: dict[str, Any] = {}

    class FakeSettings:
        use_dbos = True

    def fake_run_workflow_durably(workflow_type, event_arg):
        captured["workflow_type"] = workflow_type
        captured["event"] = event_arg
        return {"status": "durable"}

    def fail_direct_run(*_args, **_kwargs):
        raise AssertionError("Pi should route DBOS-enabled workflows through run_workflow_durably")

    monkeypatch.setattr(pi_channel, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(dbos_app, "run_workflow_durably", fake_run_workflow_durably)
    monkeypatch.setattr(pi_channel, "run_workflow", fail_direct_run)

    result = pi_channel._run_workflow_durably_or_direct(
        pi_channel.WorkflowType.GENERAL_QUESTIONS,
        event,
    )

    assert result == {"status": "durable"}
    assert captured == {
        "workflow_type": pi_channel.WorkflowType.GENERAL_QUESTIONS,
        "event": event,
    }


def test_pi_workflow_runner_uses_direct_runner_when_dbos_disabled(monkeypatch) -> None:
    event = normalize_prompt_event("hello")
    captured: dict[str, Any] = {}

    class FakeSettings:
        use_dbos = False

    class FakeWorkflowResult:
        def model_dump(self, *, mode):
            captured["dump_mode"] = mode
            return {"status": "direct"}

    def fake_run_workflow(workflow_type, event_arg):
        captured["workflow_type"] = workflow_type
        captured["event"] = event_arg
        return FakeWorkflowResult()

    monkeypatch.setattr(pi_channel, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(pi_channel, "run_workflow", fake_run_workflow)

    result = pi_channel._run_workflow_durably_or_direct(
        pi_channel.WorkflowType.GENERAL_QUESTIONS,
        event,
    )

    assert result == {"status": "direct"}
    assert captured == {
        "workflow_type": pi_channel.WorkflowType.GENERAL_QUESTIONS,
        "event": event,
        "dump_mode": "json",
    }


def test_general_question_workflow(runtime) -> None:
    event = normalize_prompt_event(
        "What owns workflow truth?",
        workspace_id=WorkspaceId.GENERAL.value,
    )
    result = WorkflowEngine(runtime).general_questions(event)
    assert result.status == WorkflowStatus.COMPLETED
    assert any(str(artifact.role) == "answer" for artifact in result.artifacts)


def test_general_question_streaming_records_same_answer(runtime) -> None:
    event = normalize_prompt_event(
        "What owns workflow truth?",
        workspace_id=WorkspaceId.GENERAL.value,
    )
    streamed: list[str] = []
    result: WorkflowResult | None = None
    for item in WorkflowEngine(runtime).stream_general_questions(event):
        if isinstance(item, str):
            streamed.append(item)
        else:
            result = item
    assert result is not None
    answer_artifact = next(
        artifact for artifact in result.artifacts if str(artifact.role) == "answer"
    )
    answer = runtime.artifact_store.read_json(answer_artifact.artifact_id)

    assert result.status == WorkflowStatus.COMPLETED
    assert "Mock local answer generated" in "".join(streamed)
    assert answer["answer"] == "".join(streamed)


def test_general_gemma_uses_registry_sampling_defaults(runtime) -> None:
    spec = runtime.model_registry.resolve_model(ModelRole.GENERAL)
    assert spec.default_params == {"temperature": 1.0, "top_k": 64, "top_p": 0.95}

    event = normalize_prompt_event(
        "What owns workflow truth?",
        workspace_id=WorkspaceId.GENERAL.value,
    )
    result = WorkflowEngine(runtime).general_questions(event, use_retrieval=False)
    assert result.status == WorkflowStatus.COMPLETED

    with runtime.database.session() as session:
        row = session.scalars(select(ModelInvocationRow)).one()

    assert row.params_json == {
        "temperature": 1.0,
        "top_k": 64,
        "top_p": 0.95,
        "max_tokens": 2048,
        "stream": True,
    }


def test_fallback_and_compactor_use_registry_sampling_defaults(runtime) -> None:
    assert runtime.model_registry.resolve_model(ModelRole.GENERAL_FALLBACK).default_params == {
        "temperature": 0.6,
        "top_k": 20,
        "top_p": 0.95,
    }
    assert runtime.model_registry.resolve_model(ModelRole.COMPACTOR).default_params == {
        "temperature": 1.0,
        "top_k": 64,
        "top_p": 0.95,
    }


def test_general_question_degrades_when_retrieval_embeddings_fail(runtime, monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("embeddings unavailable")

    monkeypatch.setattr(runtime.retrieval, "search", boom)
    event = normalize_prompt_event(
        "What owns workflow truth?",
        workspace_id=WorkspaceId.GENERAL.value,
    )
    result = WorkflowEngine(runtime).general_questions(event)
    assert result.status == WorkflowStatus.COMPLETED
    assert result.embedding_degraded is True
    assert any(str(artifact.role) == "answer" for artifact in result.artifacts)


def test_terminal_result_renderer_returns_answer(runtime, monkeypatch) -> None:
    event = normalize_prompt_event(
        "What owns workflow truth?",
        workspace_id=WorkspaceId.GENERAL.value,
    )
    result = WorkflowEngine(runtime).general_questions(event).model_dump(mode="json")

    class FakeRuntime:
        artifact_store = runtime.artifact_store

    monkeypatch.setattr(pi_channel, "get_runtime", lambda: FakeRuntime())

    rendered = render_terminal_result(result)
    assert "Mock local answer generated" in rendered


def test_audio_workflow_fails_when_source_missing(runtime) -> None:
    event = normalize_scheduled_event(
        source_type=SourceType.FILE,
        workspace_id=WorkspaceId.AUDIO.value,
        event_type="file.created",
        payload={"source_uri": "file:///tmp/this-audio-file-does-not-exist.m4a"},
    )
    event = event.model_copy(
        update={"source_uri": "file:///tmp/this-audio-file-does-not-exist.m4a"}
    )
    result = WorkflowEngine(runtime).audio_transcription(event)
    assert result.status == WorkflowStatus.FAILED_PERMANENT


def test_training_export_produces_manifest_only(runtime) -> None:
    q = normalize_prompt_event("persist something useful", workspace_id=WorkspaceId.GENERAL.value)
    WorkflowEngine(runtime).general_questions(q)
    event = normalize_scheduled_event(
        source_type=SourceType.SCHEDULED,
        workspace_id=WorkspaceId.TRAINING.value,
        event_type="training.export.requested",
        payload={},
    )
    result = WorkflowEngine(runtime).training_export_stub(event)
    assert result.status == WorkflowStatus.COMPLETED
    assert any(str(artifact.role) == "training_manifest" for artifact in result.artifacts)


def test_directive_aliases_are_semantic(runtime) -> None:
    parser = DirectiveParser(runtime.settings)
    start_general = parser.parse("/start /general").model_role
    start_chandra = parser.parse("/start /chandra").model_role
    start_asr = parser.parse("/start /asr").model_role
    start_audio = parser.parse("/start /audio").model_role
    start_gemma4 = parser.parse("/start /gemma4").model_role
    start_fallback = parser.parse("/start /fallback").model_role
    # The canonical role value must always parse, so error-message hints like
    # "pi /start /general_fallback" can never point at a command that fails.
    start_role_value = parser.parse("/start /general_fallback").model_role
    assert start_role_value is not None and start_role_value.value == "general_fallback"
    start_compactor = parser.parse("/start /compactor").model_role
    stop_med = parser.parse("/stop /med").model_role
    assert start_general is not None and start_general.value == "general"
    assert start_chandra is not None and start_chandra.value == "hard_ocr"
    assert start_asr is not None and start_asr.value == "asr"
    assert start_audio is not None and start_audio.value == "asr"
    assert start_gemma4 is not None and start_gemma4.value == "general"
    assert start_fallback is not None and start_fallback.value == "general_fallback"
    assert start_compactor is not None and start_compactor.value == "compactor"
    assert stop_med is not None and stop_med.value == "medical"
    assert normalize_terminal_text("what owns workflow truth?") == "what owns workflow truth?"


def test_bare_start_directive_hard_fails(runtime) -> None:
    parser = DirectiveParser(runtime.settings)
    with pytest.raises(ValueError, match="requires a model alias"):
        parser.parse("/start")


def test_start_new_project_directive_parses(runtime) -> None:
    parser = DirectiveParser(runtime.settings)
    create = parser.parse("/start /new-project")
    ingest = parser.parse("/start /new-project /tmp/gawd_doc_test.txt")
    targeted_create = parser.parse("/start /new-project --target-project-id ai_business_portfolio")
    targeted_ingest = parser.parse(
        "/start /new-project /tmp/gawd_doc_test.txt --target-project ai_business_portfolio"
    )
    scaffolded = parser.parse("/start /new-project --walkthru --create-target public_repo_creator")
    actions = plan_terminal_actions("/start /new-project /tmp/gawd_doc_test.txt")
    approve = parser.parse("/start /approved-gawd final-doc-id --target-project target")
    approve_scaffold = parser.parse(
        "/start /approved-gawd final-doc-id --create-target public_repo_creator"
    )
    approve_actions = plan_terminal_actions(
        "/start /approved-gawd final-doc-id --target-project target"
    )

    assert create.action == "new_project"
    assert create.alias == "/new-project"
    assert create.path is None
    assert ingest.action == "new_project"
    assert ingest.path == Path("/tmp/gawd_doc_test.txt")
    assert targeted_create.path is None
    assert targeted_create.target_project_id == "ai_business_portfolio"
    assert targeted_ingest.path == Path("/tmp/gawd_doc_test.txt")
    assert targeted_ingest.target_project_id == "ai_business_portfolio"
    assert scaffolded.create_target_id == "public_repo_creator"
    assert scaffolded.walkthru_action == "start"
    assert [action.kind for action in actions] == ["directive"]
    assert actions[0].text == "/start /new-project /tmp/gawd_doc_test.txt"
    assert approve.action == "approved_gawd"
    assert approve.alias == "/approved-gawd"
    assert approve.query == "final-doc-id"
    assert approve.target_project_id == "target"
    assert approve_scaffold.create_target_id == "public_repo_creator"
    assert [action.kind for action in approve_actions] == ["directive"]
    assert approve_actions[0].text == "/start /approved-gawd final-doc-id --target-project target"


def test_start_new_project_walkthru_directive_parses(runtime) -> None:
    parser = DirectiveParser(runtime.settings)
    start = parser.parse("/start /new-project --walkthru")
    answer = parser.parse(
        "/start /new-project --walkthru gawd-walkthru-123456abcdef "
        "--answer I want a clean public snapshot"
    )
    accept = parser.parse("/start /new-project --walkthru gawd-walkthru-123456abcdef --accept")
    edit = parser.parse(
        "/start /new-project --walkthru gawd-walkthru-123456abcdef "
        "--edit theory A corrected system theory"
    )

    assert start.walkthru_action == "start"
    assert start.walkthru_id is None
    assert answer.walkthru_action == "answer"
    assert answer.walkthru_id == "gawd-walkthru-123456abcdef"
    assert answer.walkthru_text == "I want a clean public snapshot"
    assert accept.walkthru_action == "accept"
    assert edit.walkthru_action == "edit"
    assert edit.walkthru_section_id == "theory"
    assert edit.walkthru_text == "A corrected system theory"


def test_new_project_walkthru_requires_an_id_for_continuations(runtime) -> None:
    parser = DirectiveParser(runtime.settings)

    with pytest.raises(ValueError, match="requires the walkthru id"):
        parser.parse("/start /new-project --walkthru --accept")


def test_new_project_without_a_draft_path_starts_a_walkthru(runtime) -> None:
    """The default flipped: a pathless /new-project now interviews you."""

    parser = DirectiveParser(runtime.settings)

    bare = parser.parse("/start /new-project")
    targeted = parser.parse("/start /new-project --target-project-id proj_1")

    assert bare.walkthru_action == "start"
    assert bare.path is None
    assert targeted.walkthru_action == "start"
    assert targeted.target_project_id == "proj_1"


def test_no_walkthru_restores_the_blank_draft_template(runtime) -> None:
    parser = DirectiveParser(runtime.settings)

    opted_out = parser.parse("/start /new-project --no-walkthru")
    targeted = parser.parse("/start /new-project --no-walkthru --target-project-id proj_1")

    assert opted_out.walkthru_action is None
    assert opted_out.path is None
    assert targeted.walkthru_action is None
    assert targeted.target_project_id == "proj_1"


def test_a_draft_path_still_means_ingest_not_walkthru(runtime, tmp_path: Path) -> None:
    """A draft that exists is nothing to walk through, flag or no flag."""

    draft = tmp_path / "draft.txt"
    draft.write_text("draft body", encoding="utf-8")
    parser = DirectiveParser(runtime.settings)

    parsed = parser.parse(f"/start /new-project {draft}")

    assert parsed.walkthru_action is None
    assert parsed.path == draft


def test_new_project_rejects_both_walkthru_flags(runtime) -> None:
    parser = DirectiveParser(runtime.settings)

    with pytest.raises(ValueError, match="not both"):
        parser.parse("/start /new-project --walkthru --no-walkthru")


def test_walkthru_resume_commands_survive_the_new_default(runtime) -> None:
    """The resume forms pi_command builds must keep parsing unchanged."""

    parser = DirectiveParser(runtime.settings)

    accept = parser.parse("/start /new-project --walkthru gawd-walkthru-123456abcdef --accept")
    answer = parser.parse(
        "/start /new-project --walkthru gawd-walkthru-123456abcdef --answer a clean snapshot"
    )

    assert accept.walkthru_action == "accept"
    assert accept.walkthru_id == "gawd-walkthru-123456abcdef"
    assert answer.walkthru_action == "answer"
    assert answer.walkthru_text == "a clean snapshot"


def test_terminal_routes_the_bare_form_to_the_interview(runtime, monkeypatch) -> None:
    """Regression: the predicate keyed on the --walkthru token, so the new
    default would have started a walkthru with no interview attached."""

    from local_first_agent_os import pi_command

    monkeypatch.setattr(pi_command, "get_settings", lambda: runtime.settings)

    assert pi_command._is_walkthru_command("/start /new-project") is True
    assert pi_command._is_walkthru_command("/start /new-project --walkthru") is True
    assert pi_command._is_walkthru_command("/start /new-project --no-walkthru") is False
    assert pi_command._is_walkthru_command("/start /new-project /tmp/draft.txt") is False
    assert pi_command._is_walkthru_command("/status") is False


def test_fetch_workflowy_directive_parses_as_one_action(runtime) -> None:
    parser = DirectiveParser(runtime.settings)
    directive = "/fetch /workflowy give me the first idea bullet under /ideas"

    parsed = parser.parse(directive)
    actions = plan_terminal_actions(directive)

    assert parsed.action == "fetch"
    assert parsed.retrieval_source == "workflowy"
    assert parsed.query == "give me the first idea bullet under /ideas"
    assert [action.text for action in actions] == [directive]


def test_chrome_directive_parses_as_top_level_workflow(runtime) -> None:
    parser = DirectiveParser(runtime.settings)
    bare = parser.parse("/chrome")
    opened = parser.parse("/chrome open https://example.com")
    assert bare.action == "chrome"
    assert bare.chrome_action == "list"
    assert opened.chrome_action == "open"
    assert opened.chrome_args == ("https://example.com",)
    assert [action.kind for action in plan_terminal_actions("chrome list")] == ["directive"]


def test_ledger_directive_parses_as_read_alias(runtime) -> None:
    parser = DirectiveParser(runtime.settings)
    bare = parser.parse("/ledger --limit 2")
    read_alias = parser.parse("/read /ledger --limit 2")
    bare_actions = plan_terminal_actions("ledger --limit 2")
    read_actions = plan_terminal_actions("read /ledger --limit 2")

    assert bare.action == "ledger"
    assert bare.alias == "/ledger"
    assert bare.query_tail == "--limit 2"
    assert read_alias.action == "ledger"
    assert read_alias.alias == "/read /ledger"
    assert read_alias.query_tail == "--limit 2"
    assert [action.text for action in bare_actions] == ["/ledger --limit 2"]
    assert [action.text for action in read_actions] == ["/read /ledger --limit 2"]


def test_timer_directive_parses_as_local_directive(runtime) -> None:
    parser = DirectiveParser(runtime.settings)
    spec = parser.parse("/timer 50")
    actions = plan_terminal_actions("timer 50")
    assert spec.action == "timer"
    assert spec.query == "50"
    assert normalize_terminal_text("timer 50") == "/timer 50"
    assert [action.kind for action in actions] == ["directive"]
    assert actions[0].text == "/timer 50"


def test_timer_directive_does_not_start_session_daemon(monkeypatch) -> None:
    monkeypatch.setattr(
        pi_channel,
        "ensure_session_daemon",
        lambda _settings: pytest.fail("timer should not start session daemon"),
    )
    monkeypatch.setattr(
        pi_channel,
        "run_timer_directive",
        lambda text: {
            "workflow_type": "timer",
            "status": "scheduled",
            "terminal_message": f"scheduled {text}",
        },
    )
    results = run_terminal_query("/timer 50")
    assert render_terminal_result(results[0]) == "scheduled /timer 50"


def test_terminal_query_plans_general_and_directive_chains(tmp_path) -> None:
    apostrophe_actions = plan_terminal_actions("what's up doc")
    assert [action.kind for action in apostrophe_actions] == ["query"]
    assert apostrophe_actions[0].text == "what's up doc"

    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"not really an image")
    actions = plan_terminal_actions(
        "/start /ocr && /start /asr && /start && what owns workflow truth?"
    )
    assert [action.kind for action in actions] == ["directive", "directive", "directive", "query"]
    assert actions[-1].text == "what owns workflow truth?"

    image_actions = plan_terminal_actions(f"/start /ocr {image_path} summarize this")
    assert [action.kind for action in image_actions] == ["directive", "directive", "query"]
    assert image_actions[1].text.startswith("/store ")

    screenshot_actions = plan_terminal_actions(f"/screenshot {image_path} explain")
    assert [action.kind for action in screenshot_actions] == ["directive", "query"]

    ocr_actions = plan_terminal_actions(f"/ocr {image_path}")
    assert [action.kind for action in ocr_actions] == ["directive"]
    assert ocr_actions[0].text == f"/ocr {image_path}"

    hard_ocr_actions = plan_terminal_actions(f"/hard-ocr {image_path}")
    assert [action.kind for action in hard_ocr_actions] == ["directive"]
    assert hard_ocr_actions[0].text == f"/hard-ocr {image_path}"


def test_ocr_directive_requires_one_absolute_path(runtime) -> None:
    parser = DirectiveParser(runtime.settings)

    spec = parser.parse("/ocr /tmp/images")

    assert spec.action == "ocr_capture"
    assert spec.model_role == ModelRole.OCR
    assert spec.path == Path("/tmp/images")
    with pytest.raises(ValueError, match="absolute path"):
        parser.parse("/ocr relative/images")
    with pytest.raises(ValueError, match="exactly one"):
        parser.parse("/ocr /tmp/one /tmp/two")

    hard_spec = parser.parse("/hard-ocr /tmp/images")
    assert hard_spec.action == "ocr_capture"
    assert hard_spec.model_role == ModelRole.HARD_OCR
    assert hard_spec.path == Path("/tmp/images")


def test_hard_ocr_discards_only_an_incomplete_trailing_spatial_block() -> None:
    complete = '<div data-bbox="1 2 3 4"><p>keep me</p></div>'
    broken_tail = '<div data-bbox="5 6 7 8"><p>repeated repeated'

    trimmed, removed = KnowledgeWorkflowMixin._trim_incomplete_trailing_div(
        f"{complete}\n{broken_tail}"
    )

    assert trimmed == complete
    assert removed is True
    assert KnowledgeWorkflowMixin._trim_incomplete_trailing_div(complete) == (
        complete,
        False,
    )


def test_ocr_capture_recurses_images_without_loading_embedder(
    runtime,
    tmp_path,
    monkeypatch,
) -> None:
    image_bytes = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
        "+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    image_root = tmp_path / "image-repository"
    nested = image_root / "nested"
    nested.mkdir(parents=True)
    (image_root / "board.png").write_bytes(image_bytes)
    (nested / "notes.png").write_bytes(image_bytes)
    (nested / "ignore.txt").write_text("not an image", encoding="utf-8")
    monkeypatch.setattr(
        runtime.model_manager,
        "_mock_text_for",
        lambda _request, _input_text: {
            "text": ('<div data-bbox="10 20 300 80" data-label="Text"><p>Call Mike</p></div>')
        },
    )
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": f"/ocr {image_root}"},
    )

    result = WorkflowEngine(runtime).model_directive(event)

    assert result.status == WorkflowStatus.COMPLETED
    assert ModelRole.OCR in runtime.model_manager.loaded_roles
    assert ModelRole.EMBEDDER not in runtime.model_manager.loaded_roles
    manifest_ref = next(
        artifact for artifact in result.artifacts if str(artifact.role) == "ocr_batch_manifest"
    )
    manifest = runtime.artifact_store.read_json(manifest_ref.artifact_id)
    assert manifest["root"] == str(image_root)
    assert manifest["model_role"] == ModelRole.OCR.value
    assert manifest["prompt_version"] == "surya_high_accuracy_bbox_with_block_fallback.v1"
    assert manifest["retrieval_indexed"] is False
    assert manifest["semantic_interpretation_performed"] is False
    assert [Path(item["source_path"]).name for item in manifest["images_transcribed"]] == [
        "board.png",
        "notes.png",
    ]
    assert manifest["images_failed"] == []
    assert manifest["images_skipped"] == []
    monkeypatch.setattr(pi_channel, "get_runtime", lambda: runtime)
    assert render_terminal_result(result.model_dump(mode="json")) == (
        f"OCR persisted 2 image(s) from {image_root}; 0 failed, 0 skipped, not indexed."
    )
    ocr_refs = [artifact for artifact in result.artifacts if str(artifact.role) == "ocr_text"]
    assert len(ocr_refs) == 2
    for ocr_ref in ocr_refs:
        payload = runtime.artifact_store.read_json(ocr_ref.artifact_id)
        assert payload["schema_version"] == "ocr_capture_item.v1"
        assert payload["model_role"] == ModelRole.OCR.value
        assert payload["prompt_version"] == "surya_high_accuracy_bbox_with_block_fallback.v1"
        assert "Call Mike" in payload["transcription"]
        assert payload["model_output_artifact_id"]
        assert payload["spatial_regions"] == [
            {
                "bbox": [10, 20, 300, 80],
                "confidence": None,
                "depth": None,
                "label": "Text",
                "source_artifact_id": payload["source_artifact_id"],
                "text": "Call Mike",
                "timestamp": payload["spatial_regions"][0]["timestamp"],
            }
        ]


def test_start_tail_query_targets_selected_model(monkeypatch) -> None:
    stub_session_memory(monkeypatch)
    actions = plan_terminal_actions("/start /gemma4 answer directly")
    assert [action.kind for action in actions] == ["directive", "query"]
    assert actions[0].text == "/start /gemma4"
    assert actions[1].text == "answer directly"
    assert actions[1].model_role == ModelRole.GENERAL
    assert actions[1].model_selector == "/gemma4"

    captured = {}

    def fake_directive(text, **kwargs):
        captured["directive"] = text
        return {"status": "loaded"}

    def fake_general_query(text, *, workspace_id, context, model_role=None, model_selector=None):
        captured["query"] = text
        captured["model_role"] = model_role
        captured["model_selector"] = model_selector
        return {
            "status": "answered",
            "model_role": model_role.value if model_role else None,
            "model_selector": model_selector,
        }

    monkeypatch.setattr(pi_channel, "run_pi_directive", fake_directive)
    monkeypatch.setattr(pi_channel, "run_general_query", fake_general_query)

    results = pi_channel.run_terminal_query("/start /gemma4 answer directly")

    assert results[-1]["model_role"] == ModelRole.GENERAL.value
    assert captured == {
        "directive": "/start /gemma4",
        "query": "answer directly",
        "model_role": ModelRole.GENERAL,
        "model_selector": "/gemma4",
    }


def test_model_selector_query_targets_named_alias() -> None:
    actions = plan_terminal_actions("/chandra read this text prompt")
    assert [action.kind for action in actions] == ["query"]
    assert actions[0].text == "read this text prompt"
    assert actions[0].model_role == ModelRole.HARD_OCR
    assert actions[0].model_selector == "/chandra"


def test_terminal_query_chains_on_new_directive_tokens_without_ampersands(tmp_path) -> None:
    store_dir = tmp_path / "store"
    store_dir.mkdir()

    actions = plan_terminal_actions("/start /ocr /start /asr /start what owns workflow truth?")
    assert [action.kind for action in actions] == ["directive", "directive", "directive", "query"]
    assert [action.text for action in actions] == [
        "/start /ocr",
        "/start /asr",
        "/start",
        "what owns workflow truth?",
    ]

    start_then_query = plan_terminal_actions("/start what owns workflow truth?")
    assert [action.kind for action in start_then_query] == ["directive", "query"]
    assert start_then_query[0].text == "/start"
    assert start_then_query[1].text == "what owns workflow truth?"

    store_then_get = plan_terminal_actions(f"/store {store_dir} /get workflow truth")
    assert [action.kind for action in store_then_get] == ["directive", "directive"]
    assert store_then_get[0].text == f"/store {store_dir}"
    assert store_then_get[1].text == "/get workflow truth"

    start_store = plan_terminal_actions(f"/start /store {store_dir}")
    assert [action.kind for action in start_store] == ["directive"]
    assert start_store[0].text == f"/start /store {store_dir}"


def test_terminal_query_replaces_context_after_auto_compaction(monkeypatch) -> None:
    stub_session_memory(monkeypatch)
    compacted_context = "COMPACTED CONTEXT"
    original_context = "important context " * 20
    general_calls = []

    def fake_compaction(**kwargs):
        assert kwargs["context"] == original_context
        return {
            "artifacts": [
                {
                    "role": "context_compaction",
                    "artifact_id": "context_compaction:test",
                }
            ]
        }

    class FakeArtifactStore:
        def read_json(self, artifact_id):
            assert artifact_id == "context_compaction:test"
            return {
                "schema_version": "context_compaction.v1",
                "status": "compacted",
                "compacted_context": compacted_context,
            }

    class FakeRuntime:
        artifact_store = FakeArtifactStore()

    def fake_general_query(text, *, workspace_id, context):
        general_calls.append({"text": text, "workspace_id": workspace_id, "context": context})
        return {"answer": "ok", "context": context}

    monkeypatch.setattr(pi_channel, "run_context_compaction", fake_compaction)
    monkeypatch.setattr(pi_channel, "get_runtime", lambda: FakeRuntime())
    monkeypatch.setattr(pi_channel, "run_general_query", fake_general_query)

    results = pi_channel.run_terminal_query(
        "answer using context",
        context=original_context,
        max_window_tokens=10,
    )

    assert results[-1]["context"] == compacted_context
    assert general_calls[0]["context"] == compacted_context


def test_plain_pi_query_disables_retrieval(monkeypatch) -> None:
    stub_session_memory(monkeypatch)
    captured = {}

    def fake_run_workflow(workflow_type, event):
        captured["workflow_type"] = workflow_type
        captured["payload"] = event.payload
        return {"status": "ok"}

    monkeypatch.setattr(pi_channel, "_run_workflow_durably_or_direct", fake_run_workflow)

    results = pi_channel.run_terminal_query("tell me something cool")

    assert results == [{"status": "ok"}]
    assert captured["payload"]["use_retrieval"] is False


def test_terminal_query_allocates_session_when_missing(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class FakeSessionClient:
        def __init__(self, _settings):
            pass

        def get_context(self, *, session_id, model_id):
            captured["session_id"] = session_id
            captured["model_id"] = model_id
            return ""

        def begin_turn(self, **kwargs):
            captured["begin"] = kwargs
            return {"turn_id": "turn:test", "created_at": "2026-01-01T00:00:00+00:00"}

        def complete_turn(self, **kwargs):
            captured["complete"] = kwargs

        def set_context(self, **kwargs):
            return None

    def fake_run_workflow(_workflow_type, _event):
        return {"answer": "ok"}

    monkeypatch.delenv("LOCAL_AGENT_SHELL_SESSION_ID", raising=False)
    monkeypatch.setattr(
        pi_channel,
        "ensure_session_daemon",
        lambda _settings: captured.setdefault("ensured", True),
    )
    monkeypatch.setattr(pi_channel, "SessionDaemonClient", FakeSessionClient)
    monkeypatch.setattr(pi_channel, "_run_workflow_durably_or_direct", fake_run_workflow)

    results = pi_channel.run_terminal_query("hello")

    assert results == [{"answer": "ok"}]
    assert captured["ensured"] is True
    assert str(captured["session_id"]).startswith("shell-")
    assert captured["begin"]["session_id"] == captured["session_id"]
    assert captured["complete"]["session_id"] == captured["session_id"]


def test_terminal_query_persists_user_item_before_model_failure(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    class FakeSessionClient:
        def __init__(self, _settings):
            pass

        def get_context(self, **_kwargs):
            return ""

        def begin_turn(self, **kwargs):
            captured["begin"] = kwargs
            return {"turn_id": "turn:failed", "created_at": "2026-01-01T00:00:00+00:00"}

        def complete_turn(self, **kwargs):
            captured["complete"] = kwargs

    monkeypatch.setattr(pi_channel, "ensure_session_daemon", lambda _settings: None)
    monkeypatch.setattr(pi_channel, "SessionDaemonClient", FakeSessionClient)
    monkeypatch.setattr(
        pi_channel,
        "_run_workflow_durably_or_direct",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("model failed")),
    )

    with pytest.raises(RuntimeError, match="model failed"):
        pi_channel.run_terminal_query("remember failed request")

    assert captured["begin"]["user_text"] == "remember failed request"
    assert "complete" not in captured


def test_shell_session_context_is_keyed_by_model_id(runtime, monkeypatch) -> None:
    monkeypatch.setattr(pi_channel, "ensure_session_daemon", lambda _settings: None)
    captured: dict[str, Any] = {}

    class FakeRuntime:
        model_registry = runtime.model_registry
        artifact_store = runtime.artifact_store

    class FakeSessionClient:
        def __init__(self, _settings):
            pass

        def get_context(self, *, session_id, model_id):
            captured["get"] = {"session_id": session_id, "model_id": model_id}
            return "previous /gemma4 turn"

        def begin_turn(self, **kwargs):
            captured["begin"] = kwargs
            return {"turn_id": "turn:test", "created_at": "2026-01-01T00:00:00+00:00"}

        def complete_turn(self, **kwargs):
            captured["complete"] = kwargs

        def set_context(self, **kwargs):
            captured["set"] = kwargs

    def fake_general_query(text, *, workspace_id, context, model_role=None, model_selector=None):
        captured["query"] = {
            "text": text,
            "workspace_id": workspace_id,
            "context": context,
            "model_role": model_role,
            "model_selector": model_selector,
        }
        return {"answer": "fresh answer"}

    monkeypatch.setattr(pi_channel, "get_runtime", lambda: FakeRuntime())
    monkeypatch.setattr(pi_channel, "SessionDaemonClient", FakeSessionClient)
    monkeypatch.setattr(pi_channel, "run_general_query", fake_general_query)

    results = pi_channel.run_terminal_query(
        "/gemma4 what happened before?",
        shell_session_id="shell-123",
    )

    model_id = runtime.model_registry.resolve_model(ModelRole.GENERAL).model_id
    assert results == [{"answer": "fresh answer"}]
    assert captured["get"] == {"session_id": "shell-123", "model_id": model_id}
    assert captured["query"] == {
        "text": "what happened before?",
        "workspace_id": WorkspaceId.GENERAL.value,
        "context": "previous /gemma4 turn",
        "model_role": ModelRole.GENERAL,
        "model_selector": "/gemma4",
    }
    assert captured["begin"]["session_id"] == "shell-123"
    assert captured["begin"]["model_id"] == model_id
    assert captured["begin"]["user_text"] == "what happened before?"
    assert captured["begin"]["model_selector"] == "/gemma4"
    assert captured["complete"]["answer"] == "fresh answer"
    assert captured["complete"]["turn_id"] == "turn:test"


def test_session_memory_append_persists_items_and_rebuilds_cache(runtime) -> None:
    store = SessionMemoryStore(runtime)
    model_id = runtime.model_registry.resolve_model(ModelRole.GENERAL_FALLBACK).model_id

    state = store.append_turn(
        session_id="shell-456",
        model_id=model_id,
        user_text="remember this",
        answer="stored answer",
        model_selector="/gemma4",
        max_window_tokens=2048,
        source_workspace_id=WorkspaceId.GENERAL.value,
    )

    items = runtime.repository.list_session_items("shell-456", model_id)
    assert [item["role"] for item in items] == ["user", "assistant"]
    assert items[0]["content"] == "remember this"
    assert items[0]["metadata"]["model_selector"] == "/gemma4"
    assert items[1]["content"] == "stored answer"
    assert runtime.repository.get_session_context("shell-456", model_id) is None

    reloaded = SessionMemoryStore(runtime).get_context("shell-456", model_id)
    assert "model_selector=/gemma4" in reloaded.context
    assert "remember this" in reloaded.context
    assert "stored answer" in reloaded.context
    export_path = runtime.settings.session_context_export_dir / "shell-456" / f"{model_id}.md"
    assert export_path.exists()
    assert state.dirty is False
    assert store.flush(session_id="shell-456") == []


def test_session_turn_retry_is_idempotent(runtime) -> None:
    store = SessionMemoryStore(runtime)
    model_id = runtime.model_registry.resolve_model(ModelRole.GENERAL_FALLBACK).model_id

    for _ in range(2):
        store.append_turn(
            session_id="shell-idempotent",
            model_id=model_id,
            turn_id="turn:fixed",
            user_text="once",
            answer="only once",
        )

    items = runtime.repository.list_session_items("shell-idempotent", model_id)
    assert [(item["role"], item["content"]) for item in items] == [
        ("user", "once"),
        ("assistant", "only once"),
    ]


def test_session_snapshot_loads_only_item_tail(runtime) -> None:
    store = SessionMemoryStore(runtime)
    model_id = runtime.model_registry.resolve_model(ModelRole.GENERAL_FALLBACK).model_id
    store.append_turn(
        session_id="shell-snapshot",
        model_id=model_id,
        user_text="old question",
        answer="old answer",
    )
    store.set_context(
        session_id="shell-snapshot",
        model_id=model_id,
        context="# Compact Memory\nold facts",
    )
    store.append_turn(
        session_id="shell-snapshot",
        model_id=model_id,
        user_text="new question",
        answer="new answer",
    )

    reloaded = SessionMemoryStore(runtime).get_context("shell-snapshot", model_id)
    assert "# Compact Memory" in reloaded.context
    assert "new question" in reloaded.context
    assert "new answer" in reloaded.context
    assert "old question" not in reloaded.context


def test_direct_model_query_uses_requested_role(runtime, monkeypatch) -> None:
    def fail_unload(role=None):
        pytest.fail(f"direct model query unexpectedly unloaded {role}")

    monkeypatch.setattr(runtime.model_manager, "unload", fail_unload)
    event = normalize_prompt_event(
        "answer with the selected model",
        workspace_id=WorkspaceId.GENERAL.value,
    )
    event = event.model_copy(
        update={
            "payload": {
                **event.payload,
                "use_retrieval": False,
                "model_role": ModelRole.GENERAL_FALLBACK.value,
                "model_selector": "/qwen",
            }
        }
    )

    result = WorkflowEngine(runtime).general_questions(event)

    assert result.status == WorkflowStatus.COMPLETED
    model_outputs = [
        artifact for artifact in result.artifacts if str(artifact.role) == "model_output"
    ]
    assert model_outputs
    payload = runtime.artifact_store.read_json(model_outputs[0].artifact_id)
    assert payload["model_role"] == ModelRole.GENERAL_FALLBACK.value
    assert payload["output"]["text"]
    answer_artifacts = [artifact for artifact in result.artifacts if str(artifact.role) == "answer"]
    answer_payload = runtime.artifact_store.read_json(answer_artifacts[0].artifact_id)
    assert answer_payload["model_selector"] == "/qwen"


def test_start_gemma4_becomes_default_general_model(runtime) -> None:
    start_event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/start /gemma4"},
    )
    start_result = WorkflowEngine(runtime).model_directive(start_event)
    assert start_result.status == WorkflowStatus.COMPLETED
    assert runtime.model_manager.effective_general_role() == ModelRole.GENERAL

    query_event = normalize_prompt_event(
        "answer with the active default model",
        workspace_id=WorkspaceId.GENERAL.value,
    )
    result = WorkflowEngine(runtime).general_questions(query_event, use_retrieval=False)

    assert result.status == WorkflowStatus.COMPLETED
    model_output = next(
        artifact for artifact in result.artifacts if str(artifact.role) == "model_output"
    )
    payload = runtime.artifact_store.read_json(model_output.artifact_id)
    assert payload["model_role"] == ModelRole.GENERAL.value


def test_stopping_active_general_role_resets_default(runtime) -> None:
    engine = WorkflowEngine(runtime)
    start_event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/start /qwen"},
    )
    stop_event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/stop /qwen"},
    )

    assert engine.model_directive(start_event).status == WorkflowStatus.COMPLETED
    assert runtime.model_manager.effective_general_role() == ModelRole.GENERAL_FALLBACK
    assert engine.model_directive(stop_event).status == WorkflowStatus.COMPLETED
    assert runtime.model_manager.effective_general_role() == ModelRole.GENERAL


def test_stop_asr_shuts_down_whisper_service(runtime) -> None:
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/stop /asr"},
    )

    result = WorkflowEngine(runtime).model_directive(event)

    assert result.status == WorkflowStatus.COMPLETED
    artifact = next(a for a in result.artifacts if str(a.role) == "directive_result")
    payload = runtime.artifact_store.read_json(artifact.artifact_id)
    assert payload["result"] == {"status": "mock_stopped"}


def test_ledger_directive_inspects_coordination_state(runtime, tmp_path) -> None:
    runtime.settings.coordination_root = tmp_path / "coordination-root"
    saga = run_coordination_command(
        ["create_saga", "Inspect ledger test saga"],
        settings=runtime.settings,
    )
    pow_wow = run_coordination_command(
        [
            "create_pow_wow",
            saga["saga_id"],
            "IMPLEMENTATION",
            "Inspect ledger test pow-wow",
        ],
        settings=runtime.settings,
    )
    task = run_coordination_command(
        [
            "claim_task",
            pow_wow["pow_wow_id"],
            "inspect_ledger_task",
            "write one inspection artifact",
        ],
        settings=runtime.settings,
    )
    run_coordination_command(
        [
            "submit_artifact",
            pow_wow["pow_wow_id"],
            "inspection_test_artifact",
            '{"ok": true}',
            "--task-id",
            task["task_id"],
            "--schema-version",
            "inspection_test_artifact.v1",
        ],
        settings=runtime.settings,
    )
    run_coordination_command(
        ["complete_task", task["task_id"]],
        settings=runtime.settings,
    )
    run_coordination_command(
        [
            "submit_approval_request",
            saga["saga_id"],
            "CODE_MERGE",
            "--requested-by",
            "test",
            "--payload",
            '{"changed_files":["README.md"]}',
        ],
        settings=runtime.settings,
    )
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": f"/read /ledger --saga-id {saga['saga_id']}"},
    )

    result = WorkflowEngine(runtime).model_directive(event)

    assert result.status == WorkflowStatus.COMPLETED
    artifact = next(a for a in result.artifacts if str(a.role) == "directive_result")
    payload = runtime.artifact_store.read_json(artifact.artifact_id)
    inspection = payload["inspection"]
    assert payload["action"] == "ledger"
    assert inspection["status"] == "ok"
    assert inspection["filters"]["saga_id"] == saga["saga_id"]
    assert inspection["pow_wows"][0]["pow_wow_id"] == pow_wow["pow_wow_id"]
    assert inspection["tasks_by_pow_wow"][pow_wow["pow_wow_id"]][0]["status"] == "COMPLETED"
    assert inspection["artifact_counts_by_pow_wow"][pow_wow["pow_wow_id"]] == {
        "inspection_test_artifact": 1
    }
    assert inspection["approval_requests"][0]["request_type"] == "CODE_MERGE"
    assert "Approvals: 1" in payload["report"]


def test_saga_milestones_gate_dependencies_and_record_evidence(runtime, tmp_path) -> None:
    runtime.settings.coordination_root = tmp_path / "coordination-root"
    saga = run_coordination_command(
        ["create_saga", "Milestone-gated build"],
        settings=runtime.settings,
    )
    first = run_coordination_command(
        [
            "create_saga_milestone",
            saga["saga_id"],
            "Scaffold app",
            "--sequence",
            "1",
            "--milestone-id",
            "m1_scaffold",
            "--entry-criteria",
            "Approved GAWD exists.",
            "--exit-criteria",
            "Scaffold tests pass.",
            "--required-artifact",
            "test_log",
        ],
        settings=runtime.settings,
    )
    second = run_coordination_command(
        [
            "create_saga_milestone",
            saga["saga_id"],
            "Core flow",
            "--sequence",
            "2",
            "--milestone-id",
            "m2_core_flow",
            "--depends-on",
            "m1_scaffold",
            "--exit-criteria",
            "Core flow tests pass.",
        ],
        settings=runtime.settings,
    )

    ready = run_coordination_command(
        ["next_ready_saga_milestone", saga["saga_id"]],
        settings=runtime.settings,
    )

    assert first["milestone"]["status"] == "PENDING"
    assert second["milestone"]["depends_on"] == ["m1_scaffold"]
    assert ready["milestone"]["milestone_id"] == "m1_scaffold"

    started = run_coordination_command(
        ["start_saga_milestone", "m1_scaffold", "--dispatch-intent-id", "intent-1"],
        settings=runtime.settings,
    )
    completed = run_coordination_command(
        [
            "complete_saga_milestone",
            "m1_scaffold",
            "--evidence-type",
            "test_log",
            "--evidence-content",
            "pytest passed",
        ],
        settings=runtime.settings,
    )
    next_ready = run_coordination_command(
        ["next_ready_saga_milestone", saga["saga_id"]],
        settings=runtime.settings,
    )
    fetched = run_coordination_command(
        ["get_saga_milestone", "m1_scaffold"],
        settings=runtime.settings,
    )
    saga_after = run_coordination_command(
        ["get_saga", saga["saga_id"]],
        settings=runtime.settings,
    )

    assert started["milestone"]["status"] == "IN_PROGRESS"
    assert started["milestone"]["dispatch_intent_id"] == "intent-1"
    assert completed["milestone"]["status"] == "COMPLETED"
    assert completed["evidence"]["evidence_type"] == "test_log"
    assert next_ready["milestone"]["milestone_id"] == "m2_core_flow"
    assert fetched["milestone"]["evidence"][0]["content"] == "pytest passed"
    assert saga_after["saga"]["milestone_summary"] == {"COMPLETED": 1, "PENDING": 1}


def test_pending_milestone_contract_can_be_amended_with_audit_evidence(
    runtime,
    tmp_path,
) -> None:
    runtime.settings.coordination_root = tmp_path / "coordination-root"
    saga = run_coordination_command(
        ["create_saga", "Amendable milestone"],
        settings=runtime.settings,
    )
    run_coordination_command(
        [
            "create_saga_milestone",
            saga["saga_id"],
            "Generate preview",
            "--sequence",
            "1",
            "--milestone-id",
            "m2_generate",
            "--exit-criteria",
            "Preview builds.",
        ],
        settings=runtime.settings,
    )

    amended = run_coordination_command(
        AmendSagaMilestone(
            milestone_id="m2_generate",
            reason="Add verified claims and bounded-memory evidence.",
            amended_by="rahul",
            entry_criteria=("Verified claims record exists.",),
            exit_criteria=("Preview builds within the Node heap cap.",),
            required_artifacts=("resource_usage_report",),
        ),
        settings=runtime.settings,
    )
    fetched = run_coordination_command(
        ["get_saga_milestone", "m2_generate"],
        settings=runtime.settings,
    )

    assert amended["milestone"]["entry_criteria"] == ["Verified claims record exists."]
    assert amended["milestone"]["exit_criteria"] == ["Preview builds within the Node heap cap."]
    assert amended["milestone"]["required_artifacts"] == ["resource_usage_report"]
    evidence = fetched["milestone"]["evidence"]
    assert evidence[0]["schema_version"] == "milestone_contract_amendment.v1"
    evidence_payload = json.loads(evidence[0]["content"])
    assert evidence_payload["reason"] == ("Add verified claims and bounded-memory evidence.")
    assert evidence_payload["before"]["exit_criteria"] == ["Preview builds."]

    run_coordination_command(
        ["start_saga_milestone", "m2_generate"],
        settings=runtime.settings,
    )
    with pytest.raises(RuntimeError, match="milestone_not_pending"):
        run_coordination_command(
            AmendSagaMilestone(
                milestone_id="m2_generate",
                reason="Too late to rewrite active work.",
                exit_criteria=("Different contract.",),
            ),
            settings=runtime.settings,
        )


def test_retry_saga_milestone_requires_explicit_terminal_recovery(runtime, tmp_path) -> None:
    runtime.settings.coordination_root = tmp_path / "coordination-root"
    saga = run_coordination_command(
        ["create_saga", "Retryable milestone"], settings=runtime.settings
    )
    run_coordination_command(
        [
            "create_saga_milestone",
            saga["saga_id"],
            "Recoverable step",
            "--sequence",
            "1",
            "--milestone-id",
            "recoverable",
        ],
        settings=runtime.settings,
    )
    run_coordination_command(
        ["start_saga_milestone", "recoverable", "--dispatch-intent-id", "failed-intent"],
        settings=runtime.settings,
    )
    run_coordination_command(
        ["fail_saga_milestone", "recoverable", "simulated failure"],
        settings=runtime.settings,
    )

    retried = run_coordination_command(
        ["retry_saga_milestone", "recoverable", "operator approved retry"],
        settings=runtime.settings,
    )

    assert retried["milestone"]["status"] == "PENDING"
    assert retried["milestone"]["dispatch_intent_id"] is None
    assert retried["milestone"]["started_at"] is None
    assert retried["milestone"]["completed_at"] is None


def test_try_milestone_routes_checkpointed_failure_to_recovery(runtime, tmp_path) -> None:
    runtime.settings.coordination_root = tmp_path / "coordination-root"
    saga = run_coordination_command(
        ["create_saga", "Checkpoint-owned retry"], settings=runtime.settings
    )
    milestone_id = "checkpointed-milestone"
    run_coordination_command(
        [
            "create_saga_milestone",
            saga["saga_id"],
            "Checkpointed milestone",
            "--sequence",
            "1",
            "--milestone-id",
            milestone_id,
        ],
        settings=runtime.settings,
    )
    source = f"approved_gawd:doc-1:milestone:{milestone_id}"
    intent = run_coordination_command(
        [
            "submit_dispatch_intent",
            "senior",
            "Implement checkpointed milestone",
            "--kind",
            "code",
            "--target-project-id",
            "target",
            "--source",
            source,
        ],
        settings=runtime.settings,
    )
    claimed = run_coordination_command(
        ["claim_next_dispatch_intent", "--claimed-by", "checkpoint-worker"],
        settings=runtime.settings,
    )["intent"]
    assert claimed["intent_id"] == intent["intent_id"]
    lease = run_coordination_command(
        [
            "open_execution_lease",
            "checkpoint-owned-retry-lease",
            "--worker-id",
            "checkpoint-worker",
            "--intent-id",
            intent["intent_id"],
            "--timeout-seconds",
            "60",
        ],
        settings=runtime.settings,
    )["lease"]
    checkpoint = run_coordination_command(
        [
            "create_execution_checkpoint",
            lease["lease_id"],
            "--reason",
            "supervisor_error",
            "--status",
            "PAUSED",
            "--saga-id",
            saga["saga_id"],
            "--base-head-sha",
            "a" * 40,
        ],
        settings=runtime.settings,
    )["checkpoint"]
    run_coordination_command(
        ["fail_saga_milestone", milestone_id, "simulated checkpointed failure"],
        settings=runtime.settings,
    )

    retained_intent = find_existing_dispatch_intent_for_source(runtime.settings, source)
    assert retained_intent is not None
    assert retained_intent["status"] == "PAUSED"

    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/try-milestone retry after preserved work"},
    )
    result = WorkflowEngine(runtime).model_directive(event)
    artifact = next(a for a in result.artifacts if str(a.role) == "directive_result")
    payload = runtime.artifact_store.read_json(artifact.artifact_id)

    assert result.status == WorkflowStatus.COMPLETED
    assert payload["status"] == "checkpoint_recovery_required"
    assert payload["checkpoint"]["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert payload["execution_enqueued"] is False
    assert payload["next_step"] == "pi /ledger"
    assert "was not reopened" in payload["note"]
    milestone = run_coordination_command(
        ["get_saga_milestone", milestone_id], settings=runtime.settings
    )["milestone"]
    assert milestone["status"] == "FAILED"
    assert milestone["dispatch_intent_id"] == intent["intent_id"]


def test_try_milestone_retries_only_the_most_recent_terminal_milestone(runtime, tmp_path) -> None:
    runtime.settings.coordination_root = tmp_path / "coordination-root"
    saga = run_coordination_command(["create_saga", "Shortcut retry"], settings=runtime.settings)
    for sequence, milestone_id in enumerate(("older-failure", "newer-failure"), 1):
        run_coordination_command(
            [
                "create_saga_milestone",
                saga["saga_id"],
                milestone_id,
                "--sequence",
                str(sequence),
                "--milestone-id",
                milestone_id,
            ],
            settings=runtime.settings,
        )
        run_coordination_command(
            ["fail_saga_milestone", milestone_id, f"{milestone_id} failed"],
            settings=runtime.settings,
        )

    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/try-milestone provider fallback repaired"},
    )
    result = WorkflowEngine(runtime).model_directive(event)

    artifact = next(a for a in result.artifacts if str(a.role) == "directive_result")
    payload = runtime.artifact_store.read_json(artifact.artifact_id)
    assert result.status == WorkflowStatus.COMPLETED
    assert payload["status"] == "retried"
    assert payload["resolution"]["milestone_id"] == "newer-failure"
    assert payload["resolution"]["previous_status"] == "FAILED"
    assert payload["milestone"]["status"] == "PENDING"
    assert payload["next_step"] == "pi /approve-most-recent"

    older = run_coordination_command(
        ["get_saga_milestone", "older-failure"], settings=runtime.settings
    )
    assert older["milestone"]["status"] == "FAILED"


def test_milestone_linked_dispatch_updates_resume_boundary(runtime, tmp_path) -> None:
    runtime.settings.coordination_root = tmp_path / "coordination-root"
    saga = run_coordination_command(
        ["create_saga", "Milestone dispatch build"],
        settings=runtime.settings,
    )
    run_coordination_command(
        [
            "create_saga_milestone",
            saga["saga_id"],
            "First milestone",
            "--sequence",
            "1",
            "--milestone-id",
            "m1",
        ],
        settings=runtime.settings,
    )
    run_coordination_command(
        [
            "create_saga_milestone",
            saga["saga_id"],
            "Second milestone",
            "--sequence",
            "2",
            "--milestone-id",
            "m2",
            "--depends-on",
            "m1",
        ],
        settings=runtime.settings,
    )
    intent = run_coordination_command(
        [
            "submit_dispatch_intent",
            "senior",
            "do m1",
            "--kind",
            "advisory",
            "--source",
            "approved_gawd:doc-1:milestone:m1",
        ],
        settings=runtime.settings,
    )

    claimed = run_coordination_command(
        ["claim_next_dispatch_intent", "--claimed-by", "worker-1"],
        settings=runtime.settings,
    )
    in_progress = run_coordination_command(
        ["get_saga_milestone", "m1"],
        settings=runtime.settings,
    )
    completed_intent = run_coordination_command(
        [
            "complete_dispatch_intent",
            intent["intent_id"],
            "DONE",
            "--result",
            '{"tests":"passed"}',
        ],
        settings=runtime.settings,
    )
    completed = run_coordination_command(
        ["get_saga_milestone", "m1"],
        settings=runtime.settings,
    )
    ready = run_coordination_command(
        ["reconcile_saga_milestones", saga["saga_id"]],
        settings=runtime.settings,
    )

    assert claimed["intent"]["intent_id"] == intent["intent_id"]
    assert in_progress["milestone"]["status"] == "IN_PROGRESS"
    assert in_progress["milestone"]["dispatch_intent_id"] == intent["intent_id"]
    assert completed_intent["milestone_update"]["status"] == "COMPLETED"
    assert completed["milestone"]["status"] == "COMPLETED"
    assert completed["milestone"]["evidence"][0]["evidence_type"] == "summary"
    assert ready["next_ready_milestone"]["milestone_id"] == "m2"


def test_new_project_no_walkthru_creates_sparse_gawd_draft(runtime, tmp_path, monkeypatch) -> None:
    """The blank template is now the opt-out path rather than the default."""

    monkeypatch.setattr(
        "local_first_agent_os.workflow.engine.resolve_project_repo_root",
        lambda: tmp_path,
    )
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/start /new-project --no-walkthru"},
    )

    result = WorkflowEngine(runtime).model_directive(event)

    assert result.status == WorkflowStatus.COMPLETED
    artifact = next(a for a in result.artifacts if str(a.role) == "directive_result")
    payload = runtime.artifact_store.read_json(artifact.artifact_id)
    draft_path = Path(payload["draft"]["path"])
    assert payload["status"] == "draft_created"
    assert payload["execution_started"] is False
    assert draft_path.exists()
    assert draft_path.parent == tmp_path / "docs" / "gawd_drafts"


def test_start_new_project_walkthru_creates_durable_private_session(
    runtime, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "local_first_agent_os.workflow.engine.resolve_project_repo_root",
        lambda: tmp_path,
    )
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/start /new-project --walkthru"},
    )

    result = WorkflowEngine(runtime).model_directive(event)

    assert result.status == WorkflowStatus.COMPLETED
    artifact = next(a for a in result.artifacts if str(a.role) == "directive_result")
    payload = runtime.artifact_store.read_json(artifact.artifact_id)
    session_path = tmp_path / "docs" / "gawd_drafts" / f"{payload['walkthru_id']}.json"
    assert payload["status"] == "walkthru_started"
    assert payload["state"] == "awaiting_answer"
    assert payload["section"]["section_id"] == "project"
    assert payload["execution_started"] is False
    assert session_path.exists()

    class FakeRuntime:
        artifact_store = runtime.artifact_store

    monkeypatch.setattr(pi_channel, "get_runtime", lambda: FakeRuntime())
    rendered = render_terminal_result(result.model_dump(mode="json"))
    assert "What should this project be called?" in rendered
    assert '--answer "YOUR ANSWER"' in rendered


def test_new_project_walkthru_answer_uses_model_without_merging_unparsed_output(
    runtime, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "local_first_agent_os.workflow.engine.resolve_project_repo_root",
        lambda: tmp_path,
    )

    def run(directive: str) -> dict[str, object]:
        event = normalize_scheduled_event(
            source_type=SourceType.MANUAL,
            workspace_id=WorkspaceId.GENERAL.value,
            event_type="pi.directive",
            payload={"directive": directive},
        )
        result = WorkflowEngine(runtime).model_directive(event)
        artifact = next(a for a in result.artifacts if str(a.role) == "directive_result")
        return runtime.artifact_store.read_json(artifact.artifact_id)

    started = run("/start /new-project --walkthru")
    walkthru_id = str(started["walkthru_id"])
    run(f"/start /new-project --walkthru {walkthru_id} --answer Public Copy Project")
    run(f"/start /new-project --walkthru {walkthru_id} --accept")
    exact = "One day to scope and one day to verify."

    proposed = run(f"/start /new-project --walkthru {walkthru_id} --answer {exact}")

    proposal = proposed["proposal"]
    assert isinstance(proposal, dict)
    assert proposal["verbatim"] == exact
    assert proposal["summary"] == exact
    assert proposal["summary_method"] == "fallback_verbatim"
    assert proposed["execution_started"] is False


def test_start_new_project_carries_target_into_next_command(runtime, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "local_first_agent_os.workflow.engine.resolve_project_repo_root",
        lambda: tmp_path,
    )
    target_path = tmp_path / "target"
    target_path.mkdir()
    write_linked_projects_config(runtime.settings.config_dir, target_path)
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/start /new-project --no-walkthru --target-project-id target"},
    )

    result = WorkflowEngine(runtime).model_directive(event)

    artifact = next(a for a in result.artifacts if str(a.role) == "directive_result")
    payload = runtime.artifact_store.read_json(artifact.artifact_id)
    assert payload["target_project_id"] == "target"
    assert payload["target_project"]["path"] == str(target_path)
    assert payload["next_step"].endswith("--target-project-id target")


INTAKE_DRAFT_BODY = """# THE GAWD DOC - Mini

**Draft ID:** test
**Project:** Intake Flow | **Version:** v4-mini | **Status:** SPARSE_DRAFT
**Date:** 2026-07-08

## 1. Theory of the System

Durable GAWD intake feeding a pow-wow finalization stage.

## 2. Why This Exists

Start projects from approved contracts.

## 3. Happy Path / Golden Flow

1. Fill in a sparse draft.
2. Finalize the contract.
3. Wait for operator approval.

## 4. This Version - Scope & Non-Goals

**In scope.**
- Create the file-first intake.

**Cut (non-goals).**
- No implementation execution yet.

## 5. Core Design

**Unit of work.** one new project intake

**Lifecycle.**
- sparse draft -> finalized draft -> approval

**Data model.**
- draft file, GAWD docs, saga, pow-wow, tasks, artifacts

## 6. The Failure That Matters Most

Execution starts before approval.

## 7. Verification

- Ledger records are created.

## 8. Decision Log

- D1 - Use file-first intake.

## 9. If I Had 2 More Weeks

- Add richer approval UX.
"""


def test_ingesting_one_draft_twice_replays_onto_a_single_saga(
    runtime, tmp_path, monkeypatch
) -> None:
    """The bug this branch exists for, at the level where it actually happened.

    Five sagas once shared the goal prefix "New project intake: Two live
    prospects exist" because a repeated ingest created a new saga each time. The
    unit tests drive create_saga directly; this drives the directive, which is
    the path that produced the duplicates.
    """

    monkeypatch.setattr(
        "local_first_agent_os.workflow.engine.resolve_project_repo_root",
        lambda: tmp_path,
    )
    runtime.settings.coordination_root = tmp_path / "coordination-root"
    runtime.settings.saga_executor_backend = "dry_run"
    draft_file = create_sparse_gawd_draft_file(tmp_path)
    draft_file.path.write_text(INTAKE_DRAFT_BODY, encoding="utf-8")
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": f"/start /new-project {draft_file.path}"},
    )

    first = WorkflowEngine(runtime).model_directive(event)
    WorkflowEngine(runtime).model_directive(event)

    assert first.status == WorkflowStatus.COMPLETED
    with tx() as conn:
        rows = conn.execute("SELECT saga_id, goal FROM sagas").fetchall()
    saga_ids = [row["saga_id"] for row in rows]
    goals = [row["goal"] for row in rows]

    assert len(saga_ids) == 1, f"a repeated ingest opened {len(saga_ids)} sagas: {goals}"


def _directive_result_payload(runtime, result: WorkflowResult) -> dict[str, Any]:
    artifact = next(a for a in result.artifacts if str(a.role) == "directive_result")
    return runtime.artifact_store.read_json(artifact.artifact_id)


def test_a_repeated_ingest_reports_already_ingested(runtime, tmp_path, monkeypatch) -> None:
    """Re-running a command and finding the work done is not a failure.

    The second ingest used to die on 'UNIQUE constraint failed:
    saga_milestones.milestone_id' after redoing all of finalization. Intake now
    refuses as soon as create_saga reports the replay, so nothing downstream
    runs and the operator gets the existing saga's standing instead of a crash.
    """

    monkeypatch.setattr(
        "local_first_agent_os.workflow.engine.resolve_project_repo_root",
        lambda: tmp_path,
    )
    runtime.settings.coordination_root = tmp_path / "coordination-root"
    runtime.settings.saga_executor_backend = "dry_run"
    draft_file = create_sparse_gawd_draft_file(tmp_path)
    draft_file.path.write_text(INTAKE_DRAFT_BODY, encoding="utf-8")
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": f"/start /new-project {draft_file.path}"},
    )

    first = WorkflowEngine(runtime).model_directive(event)
    second = WorkflowEngine(runtime).model_directive(event)

    assert first.status == WorkflowStatus.COMPLETED
    assert second.status == WorkflowStatus.COMPLETED
    first_payload = _directive_result_payload(runtime, first)
    second_payload = _directive_result_payload(runtime, second)
    assert first_payload["status"] == "finalized_pending_operator_approval"
    assert second_payload["status"] == "already_ingested"
    assert second_payload["replayed"] is True
    assert second_payload["saga_id"] == first_payload["saga_id"]
    # The read-back is the point: a bare refusal would not say where it stands.
    assert [item["milestone_id"] for item in second_payload["saga_milestones"]] == [
        item["milestone_id"] for item in first_payload["saga_milestones"]
    ]


def test_a_replayed_intake_creates_no_second_pow_wow_or_doc_pair(
    runtime, tmp_path, monkeypatch
) -> None:
    """Refusing late would still leave the saga carrying the repeat work.

    This is what makes the early bail worth more than a guard at the milestone
    step: the ledger must look exactly as it did after the first ingest.
    """

    monkeypatch.setattr(
        "local_first_agent_os.workflow.engine.resolve_project_repo_root",
        lambda: tmp_path,
    )
    runtime.settings.coordination_root = tmp_path / "coordination-root"
    runtime.settings.saga_executor_backend = "dry_run"
    draft_file = create_sparse_gawd_draft_file(tmp_path)
    draft_file.path.write_text(INTAKE_DRAFT_BODY, encoding="utf-8")
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": f"/start /new-project {draft_file.path}"},
    )
    WorkflowEngine(runtime).model_directive(event)
    before = _ledger_row_counts()

    WorkflowEngine(runtime).model_directive(event)
    after = _ledger_row_counts()

    assert after == before


_LEDGER_TABLES = ("sagas", "gawd_docs", "pow_wows", "saga_tasks", "saga_milestones")


def _ledger_row_counts() -> dict[str, int]:
    """What a repeated ingest must not change."""

    with tx() as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
            for table in _LEDGER_TABLES
        }


def test_a_second_distinct_draft_still_opens_its_own_saga(runtime, tmp_path, monkeypatch) -> None:
    """Dedupe must not collapse two genuinely different projects into one."""

    monkeypatch.setattr(
        "local_first_agent_os.workflow.engine.resolve_project_repo_root",
        lambda: tmp_path,
    )
    runtime.settings.coordination_root = tmp_path / "coordination-root"
    runtime.settings.saga_executor_backend = "dry_run"
    engine = WorkflowEngine(runtime)
    for index in range(2):
        draft_file = create_sparse_gawd_draft_file(tmp_path)
        body = INTAKE_DRAFT_BODY.replace(
            "Durable GAWD intake feeding a pow-wow finalization stage.",
            f"Durable GAWD intake number {index}.",
        )
        draft_file.path.write_text(body, encoding="utf-8")
        engine.model_directive(
            normalize_scheduled_event(
                source_type=SourceType.MANUAL,
                workspace_id=WorkspaceId.GENERAL.value,
                event_type="pi.directive",
                payload={"directive": f"/start /new-project {draft_file.path}"},
            )
        )

    with tx() as conn:
        count = conn.execute("SELECT COUNT(*) AS count FROM sagas").fetchone()["count"]

    assert count == 2


def test_start_new_project_ingests_draft_and_finalizes(runtime, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "local_first_agent_os.workflow.engine.resolve_project_repo_root",
        lambda: tmp_path,
    )
    runtime.settings.coordination_root = tmp_path / "coordination-root"
    runtime.settings.saga_executor_backend = "dry_run"
    draft_file = create_sparse_gawd_draft_file(tmp_path)
    draft_file.path.write_text(
        INTAKE_DRAFT_BODY,
        encoding="utf-8",
    )
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": f"/start /new-project {draft_file.path}"},
    )

    result = WorkflowEngine(runtime).model_directive(event)

    assert result.status == WorkflowStatus.COMPLETED
    artifact = next(a for a in result.artifacts if str(a.role) == "directive_result")
    payload = runtime.artifact_store.read_json(artifact.artifact_id)
    finalized_path = Path(payload["finalized_path"])
    permissions_path = Path(payload["permissions_path"])
    assert payload["status"] == "finalized_pending_operator_approval"
    assert payload["execution_started"] is False
    assert payload["approval_required"] is True
    assert payload["durable_workflow_plan"]["schema_version"] == "durable_workflow_plan.v1"
    assert payload["durable_workflow_plan"]["steps"]
    assert payload["workflow_plan_refinement"]["status"] == "scaffold_used"
    assert payload["initial_gawd_doc_id"] != payload["final_gawd_doc_id"]
    assert len(payload["saga_milestones"]) == 3
    assert payload["saga_milestones"][0]["depends_on"] == []
    assert payload["saga_milestones"][1]["depends_on"] == [
        payload["saga_milestones"][0]["milestone_id"]
    ]
    assert [task["task_name"] for task in payload["tasks"]] == [
        "junior_permissions_scan",
        "senior_spec_completion",
        "staff_final_verdict",
    ]
    assert finalized_path.exists()
    assert permissions_path.exists()
    # The finalized document is markdown and says so. It used to be written as
    # `.txt` beside a `.workflow.toml` sidecar that nothing in the repository
    # ever read back, while the document itself compiled with `no_milestones`.
    # The plan survives as data on this payload; what reaches disk is the one
    # file an operator approves and `compile_design_doc` then reads.
    assert finalized_path.suffix == ".md"
    assert "workflow_plan_path" not in payload
    assert not list(finalized_path.parent.glob("*.workflow.toml"))
    finalized_text = finalized_path.read_text(encoding="utf-8")
    assert "FINALIZED_DRAFT" in finalized_text
    assert "### Milestone 0:" in finalized_text
    assert payload["durable_workflow_plan"]["steps"][0]["step_id"].startswith("step_m01_")
    assert "durable_boundary_reason" in payload["durable_workflow_plan"]["steps"][0]

    with tx() as conn:
        saga = conn.execute("SELECT saga_id, gawd_doc_id FROM sagas").fetchone()
        docs = conn.execute(
            "SELECT gawd_doc_id, version, status FROM gawd_docs ORDER BY version"
        ).fetchall()
        pow_wow = conn.execute("SELECT stage, status FROM pow_wows").fetchone()
        task_count = conn.execute("SELECT COUNT(*) AS count FROM saga_tasks").fetchone()["count"]
        milestone_rows = conn.execute(
            """
            SELECT milestone_id, sequence, status, depends_on_json
            FROM saga_milestones
            ORDER BY sequence
            """
        ).fetchall()
        artifact_types = {
            row["artifact_type"]
            for row in conn.execute("SELECT artifact_type FROM task_artifacts").fetchall()
        }

    assert saga["gawd_doc_id"] == payload["final_gawd_doc_id"]
    assert [doc["version"] for doc in docs] == [1, 2]
    assert docs[1]["status"] == "DRAFT"
    assert (pow_wow["stage"], pow_wow["status"]) == ("GAWD_DOC", "COMPLETED")
    assert task_count == 3
    assert [row["sequence"] for row in milestone_rows] == [1, 2, 3]
    assert [row["status"] for row in milestone_rows] == ["PENDING", "PENDING", "PENDING"]
    assert tomllib.loads(f"depends_on = {milestone_rows[1]['depends_on_json']}")["depends_on"] == [
        milestone_rows[0]["milestone_id"]
    ]
    assert {
        "finalized_gawd_draft",
        "permission_envelope",
        "durable_workflow_plan",
        "durable_workflow_plan_model_refinement",
    } <= artifact_types


def test_start_approved_gawd_approves_and_enqueues_once(runtime, tmp_path) -> None:
    runtime.settings.coordination_root = tmp_path / "coordination-root"
    target_path = tmp_path / "target"
    target_path.mkdir()
    write_linked_projects_config(runtime.settings.config_dir, target_path)
    saga = run_coordination_command(
        ["create_saga", "Build from finalized GAWD"],
        settings=runtime.settings,
    )
    doc = run_coordination_command(
        [
            "create_gawd_doc",
            "Build from finalized GAWD",
            "--saga-id",
            saga["saga_id"],
            "--constraints",
            "No deploy.",
            "--success-criteria",
            "Tests pass.",
            "--acceptance-criteria",
            "Implementation stays in scope.",
            "--task-graph-json",
            '{"schema_version":"new_project_task_graph.v1"}',
        ],
        settings=runtime.settings,
    )
    gawd_doc_id = doc["gawd_doc_id"]
    gated_milestone_id = f"{saga['saga_id']}:m01_gated"
    run_coordination_command(
        [
            "create_saga_milestone",
            saga["saga_id"],
            "First gated milestone",
            "--sequence",
            "1",
            "--milestone-id",
            gated_milestone_id,
            "--gawd-doc-id",
            gawd_doc_id,
            "--required-artifact",
            "test_log",
            "--approval-required",
        ],
        settings=runtime.settings,
    )
    directive = f"/start /approved-gawd {gawd_doc_id} --target-project target"
    first_event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": directive},
    )

    first_result = WorkflowEngine(runtime).model_directive(first_event)

    assert first_result.status == WorkflowStatus.COMPLETED
    first_artifact = next(a for a in first_result.artifacts if str(a.role) == "directive_result")
    first_payload = runtime.artifact_store.read_json(first_artifact.artifact_id)
    assert first_payload["status"] == "approved_and_enqueued"
    assert first_payload["execution_enqueued"] is True
    assert first_payload["execution_started"] is False
    ready_milestone_id = first_payload["ready_milestone"]["milestone_id"]
    assert ready_milestone_id == gated_milestone_id
    assert first_payload["milestone_approval"]["resolution"]["status"] == "APPROVED"
    assert first_payload["saga_milestones"][0]["milestone_id"] == ready_milestone_id
    assert first_payload["dispatch_source"] == (
        f"approved_gawd:{gawd_doc_id}:milestone:{ready_milestone_id}"
    )
    assert first_payload["target_project_id"] == "target"
    assert first_payload["dispatch_intent"]["target_project_id"] == "target"
    assert "Dispatch intent:" in first_payload["report"]
    assert "Intent status: PENDING" in first_payload["report"]
    assert "Next: pi /dispatch" in first_payload["report"]

    second_event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": directive},
    )
    second_result = WorkflowEngine(runtime).model_directive(second_event)
    second_artifact = next(a for a in second_result.artifacts if str(a.role) == "directive_result")
    second_payload = runtime.artifact_store.read_json(second_artifact.artifact_id)
    assert second_payload["status"] == "already_enqueued"
    assert second_payload["dispatch_intent_id"] == first_payload["dispatch_intent_id"]

    with tx() as conn:
        doc_status = conn.execute(
            "SELECT status FROM gawd_docs WHERE gawd_doc_id = ?",
            (gawd_doc_id,),
        ).fetchone()["status"]
        dispatch_intents = conn.execute(
            "SELECT tier, kind, source, status, prompt, target_project_id FROM dispatch_intents"
        ).fetchall()
        milestones = conn.execute(
            "SELECT milestone_id, status, required_artifacts_json FROM saga_milestones"
        ).fetchall()
        milestone_approvals = conn.execute(
            """
            SELECT request_type, status, payload_json
            FROM approval_requests
            WHERE saga_id = ?
            """,
            (saga["saga_id"],),
        ).fetchall()

    assert doc_status == "APPROVED"
    assert len(dispatch_intents) == 1
    intent = dispatch_intents[0]
    assert (intent["tier"], intent["kind"], intent["source"], intent["status"]) == (
        "senior",
        "code",
        f"approved_gawd:{gawd_doc_id}:milestone:{ready_milestone_id}",
        "PENDING",
    )
    assert "Implement the next approved saga milestone." in intent["prompt"]
    assert ready_milestone_id in intent["prompt"]
    assert intent["target_project_id"] == "target"
    assert [
        (row["milestone_id"], row["status"], row["required_artifacts_json"]) for row in milestones
    ] == [(ready_milestone_id, "PENDING", '["test_log"]')]
    assert len(milestone_approvals) == 1
    approval = milestone_approvals[0]
    assert (approval["request_type"], approval["status"]) == ("GENERAL", "APPROVED")
    assert ready_milestone_id in approval["payload_json"]


def test_start_approved_gawd_requires_target_project(runtime, tmp_path) -> None:
    runtime.settings.coordination_root = tmp_path / "coordination-root"
    target_path = tmp_path / "target"
    target_path.mkdir()
    write_linked_projects_config(runtime.settings.config_dir, target_path)
    saga = run_coordination_command(
        ["create_saga", "Build from finalized GAWD"],
        settings=runtime.settings,
    )
    doc = run_coordination_command(
        [
            "create_gawd_doc",
            "Build from finalized GAWD",
            "--saga-id",
            saga["saga_id"],
            "--success-criteria",
            "Tests pass.",
            "--task-graph-json",
            '{"schema_version":"new_project_task_graph.v1"}',
        ],
        settings=runtime.settings,
    )
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": f"/start /approved-gawd {doc['gawd_doc_id']}"},
    )

    result = WorkflowEngine(runtime).model_directive(event)

    assert result.status == WorkflowStatus.FAILED_PERMANENT
    artifact = next(a for a in result.artifacts if str(a.role) == "directive_result")
    payload = runtime.artifact_store.read_json(artifact.artifact_id)
    assert "requires an explicit target project" in payload["error"]

    with tx() as conn:
        dispatch_count = conn.execute("SELECT COUNT(*) AS count FROM dispatch_intents").fetchone()[
            "count"
        ]
        doc_status = conn.execute(
            "SELECT status FROM gawd_docs WHERE gawd_doc_id = ?",
            (doc["gawd_doc_id"],),
        ).fetchone()["status"]
    assert dispatch_count == 0
    assert doc_status == "DRAFT"


def test_start_approved_gawd_uses_target_embedded_during_intake(runtime, tmp_path) -> None:
    runtime.settings.coordination_root = tmp_path / "coordination-root"
    target_path = tmp_path / "target"
    target_path.mkdir()
    write_linked_projects_config(runtime.settings.config_dir, target_path)
    saga = run_coordination_command(
        ["create_saga", "Build against intake target"],
        settings=runtime.settings,
    )
    doc = run_coordination_command(
        [
            "create_gawd_doc",
            "Build against intake target",
            "--saga-id",
            saga["saga_id"],
            "--success-criteria",
            "Tests pass.",
            "--task-graph-json",
            '{"schema_version":"new_project_task_graph.v1","target_project_id":"target"}',
        ],
        settings=runtime.settings,
    )
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": f"/start /approved-gawd {doc['gawd_doc_id']}"},
    )

    result = WorkflowEngine(runtime).model_directive(event)

    artifact = next(a for a in result.artifacts if str(a.role) == "directive_result")
    payload = runtime.artifact_store.read_json(artifact.artifact_id)
    assert result.status == WorkflowStatus.COMPLETED
    assert payload["status"] == "approved_and_enqueued"
    assert payload["target_project_id"] == "target"
    assert payload["dispatch_intent"]["target_project_id"] == "target"


def test_approve_most_recent_resolves_gawd_target_and_enqueues(runtime, tmp_path) -> None:
    runtime.settings.coordination_root = tmp_path / "coordination-root"
    target_path = tmp_path / "target"
    target_path.mkdir()
    write_linked_projects_config(runtime.settings.config_dir, target_path)
    saga = run_coordination_command(["create_saga", "Shortcut approval"], settings=runtime.settings)
    doc = run_coordination_command(
        [
            "create_gawd_doc",
            "Shortcut approval",
            "--saga-id",
            saga["saga_id"],
            "--success-criteria",
            "Tests pass.",
            "--task-graph-json",
            '{"schema_version":"new_project_task_graph.v1"}',
        ],
        settings=runtime.settings,
    )
    milestone_id = f"{saga['saga_id']}:m01"
    run_coordination_command(
        [
            "create_saga_milestone",
            saga["saga_id"],
            "First gated milestone",
            "--sequence",
            "1",
            "--milestone-id",
            milestone_id,
            "--gawd-doc-id",
            doc["gawd_doc_id"],
            "--approval-required",
        ],
        settings=runtime.settings,
    )
    prior_intent = run_coordination_command(
        [
            "submit_dispatch_intent",
            "senior",
            "Historical target provenance",
            "--kind",
            "code",
            "--target-project-id",
            "target",
            "--source",
            f"approved_gawd:{doc['gawd_doc_id']}:milestone:completed-prior",
        ],
        settings=runtime.settings,
    )
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/approve-most-recent"},
    )

    result = WorkflowEngine(runtime).model_directive(event)

    artifact = next(a for a in result.artifacts if str(a.role) == "directive_result")
    payload = runtime.artifact_store.read_json(artifact.artifact_id)
    assert result.status == WorkflowStatus.COMPLETED, payload
    assert payload["requested_action"] == "approve_most_recent"
    assert payload["resolution"]["saga_id"] == saga["saga_id"]
    assert payload["resolution"]["milestone_id"] == milestone_id
    assert payload["gawd_doc_id"] == doc["gawd_doc_id"]
    assert payload["target_project_id"] == "target"
    assert payload["resolution"]["target_resolution"] == {
        "target_project_id": "target",
        "source": "prior_gawd_dispatch_intents",
        "intent_ids": [prior_intent["intent_id"]],
    }
    assert payload["status"] == "approved_and_enqueued"
    assert payload["next_step"] == "pi /dispatch"


def test_approve_most_recent_refuses_ambiguous_historical_targets(runtime, tmp_path) -> None:
    runtime.settings.coordination_root = tmp_path / "coordination-root"
    saga = run_coordination_command(
        ["create_saga", "Ambiguous shortcut target"], settings=runtime.settings
    )
    doc = run_coordination_command(
        [
            "create_gawd_doc",
            "Ambiguous shortcut target",
            "--saga-id",
            saga["saga_id"],
            "--task-graph-json",
            "{}",
        ],
        settings=runtime.settings,
    )
    milestone_id = f"{saga['saga_id']}:m01"
    run_coordination_command(
        [
            "create_saga_milestone",
            saga["saga_id"],
            "Ambiguous gated milestone",
            "--sequence",
            "1",
            "--milestone-id",
            milestone_id,
            "--gawd-doc-id",
            doc["gawd_doc_id"],
            "--approval-required",
        ],
        settings=runtime.settings,
    )
    for target in ("target-a", "target-b"):
        run_coordination_command(
            [
                "submit_dispatch_intent",
                "senior",
                f"Historical target {target}",
                "--kind",
                "code",
                "--target-project-id",
                target,
                "--source",
                f"approved_gawd:{doc['gawd_doc_id']}:milestone:prior-{target}",
            ],
            settings=runtime.settings,
        )
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/approve-most-recent"},
    )

    result = WorkflowEngine(runtime).model_directive(event)

    artifact = next(a for a in result.artifacts if str(a.role) == "directive_result")
    payload = runtime.artifact_store.read_json(artifact.artifact_id)
    assert result.status == WorkflowStatus.FAILED_PERMANENT
    assert "multiple target projects" in payload["error"]
    approvals = run_coordination_command(
        ["list_approval_requests", "--saga-id", saga["saga_id"]],
        settings=runtime.settings,
    )
    assert approvals["requests"] == []


def test_dispatcher_directive_reports_completed_poll_count(runtime, monkeypatch) -> None:
    from local_first_agent_os import dispatcher, dispatcher_runner

    class FakeDispatcher:
        def __init__(self, *_args, **_kwargs) -> None:
            self.last_outcomes = [
                dispatcher.Dispatched(
                    intent_id="intent-1",
                    tier="senior",
                    status="DONE",
                    source="approved_gawd:doc-1:milestone:m04",
                    target_project_id="pest_site_factory",
                    milestone_id="saga-1:m04",
                )
            ]

        def dispatch_pending_intents(
            self, *, interval_seconds: float, max_polls: int | None
        ) -> int:
            assert interval_seconds == 2.0
            assert max_polls == 1
            return 1

    monkeypatch.setattr(
        dispatcher_runner,
        "build_dispatcher_runner",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(dispatcher, "LedgerDispatcher", FakeDispatcher)
    spec = DirectiveParser(runtime.settings).parse("/start /dispatcher --max-polls 1")

    result = WorkflowEngine(runtime)._dispatcher_directive(spec, "workflow-test")

    assert result["status"] == "completed"
    assert result["dispatched_count"] == 1
    assert result["report"] == (
        "dispatcher completed: dispatched 1 intent(s) in 1 poll(s)\n"
        "intent intent-1: DONE; tier=senior; target=pest_site_factory; "
        "milestone=saga-1:m04; source=approved_gawd:doc-1:milestone:m04"
    )
    assert result["dispatch_outcomes"] == [
        {
            "intent_id": "intent-1",
            "status": "DONE",
            "tier": "senior",
            "target_project_id": "pest_site_factory",
            "milestone_id": "saga-1:m04",
            "source": "approved_gawd:doc-1:milestone:m04",
        }
    ]


def test_dispatch_once_is_a_one_poll_dispatcher(runtime, monkeypatch) -> None:
    from local_first_agent_os import dispatcher, dispatcher_runner

    class FakeDispatcher:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def dispatch_pending_intents(
            self, *, interval_seconds: float, max_polls: int | None
        ) -> int:
            assert interval_seconds == 2.0
            assert max_polls == 1
            return 0

    monkeypatch.setattr(
        dispatcher_runner,
        "build_dispatcher_runner",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(dispatcher, "LedgerDispatcher", FakeDispatcher)
    spec = DirectiveParser(runtime.settings).parse("/dispatch")

    result = WorkflowEngine(runtime)._dispatcher_directive(spec, "workflow-test")

    assert result["action"] == "dispatch_once"
    assert result["max_polls"] == 1
    assert result["dispatched_count"] == 0
    assert result["resolved_command"] == "/start /dispatcher --max-polls 1"


def test_review_merge_hydrates_legacy_packet_without_approving(
    runtime,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.settings.coordination_root = tmp_path / "coordination-root"
    target = tmp_path / "target"
    target.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.email", "review@example.com"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.name", "Review Test"], cwd=target, check=True)
    (target / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=target, check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(["git", "switch", "-c", "agent/review-test"], cwd=target, check=True)
    (target / "README.md").write_text("base\nreview me\n", encoding="utf-8")
    (target / "feature.py").write_text("ENABLED = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=target, check=True)
    subprocess.run(["git", "commit", "-m", "implement reviewed feature"], cwd=target, check=True)
    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    write_linked_projects_config(runtime.settings.config_dir, target)
    # Approving a CODE_MERGE enqueues it, and the enqueue resolves the project
    # registry from ambient settings rather than from the caller's: a
    # coordination command arrives as argv with no `Settings` attached, and
    # `CoordinationLedgerSelection` carries which ledger to reach, not which
    # configuration directory to read. In production the two are the same
    # process reading the same `.env`. Here they are not, so the environment is
    # pointed at the registry this test just wrote - without it the enqueue would
    # look for "target" in the developer's real linked_projects.toml.
    monkeypatch.setenv("LOCAL_AGENT_CONFIG_DIR", str(runtime.settings.config_dir))
    get_settings.cache_clear()

    saga = run_coordination_command(
        ["create_saga", "Review a completed change"], settings=runtime.settings
    )
    intent = run_coordination_command(
        [
            "submit_dispatch_intent",
            "senior",
            "Implement a reviewed feature",
            "--kind",
            "code",
            "--target-project-id",
            "target",
        ],
        settings=runtime.settings,
    )
    run_coordination_command(
        ["claim_next_dispatch_intent", "--claimed-by", "review-test"],
        settings=runtime.settings,
    )
    run_result = {
        "schema_version": "dispatch_runner_result.v1",
        "run_result": {
            "executor": "CliPowWowExecutor",
            "mode": "cli",
            "pow_wow_id": "pow-review",
            "target_project_id": "target",
            "target_project_path": str(target),
            "status": "COMPLETED",
            "output_summary": "Implemented the reviewed feature.",
            "changed_files": ["README.md", "feature.py"],
            "verification_commands": ["pytest -q"],
            "verification_output": ["pytest -q -> 0\n12 passed"],
            "risks": ["Feature remains undeployed."],
            "tasks": [
                {
                    "task_name": "staff_review",
                    "role": "reviewer",
                    "status": "completed",
                    "summary": "Independent review passed.",
                    "risks": [],
                    "artifacts": [
                        {
                            "artifact_type": "cli_agent_run",
                            "content": {"verdict": "APPROVE\nNo blocking findings."},
                        },
                        {
                            "artifact_type": "review_result",
                            "schema_version": "review_result.v1",
                            "content": {
                                "schema_version": "review_result.v1",
                                "verdict": "approve",
                                "finding_severity": "NON_BLOCKING",
                                "review_origin": "AUTOMATED_STAFF",
                                "reviewer_tier": "STAFF",
                                "harness": "codex",
                                "model": "gpt-5.6-sol",
                                "reasoning_effort": "high",
                                "execution_lease_id": "lease-review",
                                "task_id": "task-review",
                                "reviewed_commit_sha": commit_sha,
                                "base_sha": base_sha,
                                "attempt_number": 1,
                                "completion_status": "COMPLETED",
                                "engineering_doctrine": (
                                    CURRENT_ENGINEERING_DOCTRINE.provenance_payload()
                                ),
                                "provenance_stamped_by": "pow_wow_executor",
                            },
                        },
                    ],
                },
                {
                    "task_name": "implementation",
                    "role": "implementer",
                    "status": "completed",
                    "summary": "Feature implemented.",
                    "risks": [],
                    "artifacts": [
                        {
                            "artifact_type": "worktree_commit_checkpoint",
                            "task_name": "implementation",
                            "content": {
                                "branch_name": "agent/review-test",
                                "base_head_sha": base_sha,
                                "commit_sha": commit_sha,
                                "commit_created": True,
                                "changed_from_base": True,
                                "checkpointed_files": ["README.md", "feature.py"],
                            },
                        }
                    ],
                },
            ],
            "artifacts": [],
            "external_agents_started": True,
            "auto_merge": False,
        },
    }
    run_coordination_command(
        [
            "complete_dispatch_intent",
            intent["intent_id"],
            "DONE",
            "--result",
            json.dumps(run_result),
        ],
        settings=runtime.settings,
    )
    approval = run_coordination_command(
        [
            "submit_approval_request",
            saga["saga_id"],
            "CODE_MERGE",
            "--requested-by",
            "legacy-dispatcher",
            "--payload",
            json.dumps(
                {
                    "intent_id": intent["intent_id"],
                    "pow_wow_id": "pow-review",
                    "target_project_id": "target",
                    "branch": "agent/review-test",
                    "base_sha": base_sha,
                    "commit_sha": commit_sha,
                    "milestone_id": "milestone-3",
                    "changed_files": ["README.md", "feature.py"],
                }
            ),
        ],
        settings=runtime.settings,
    )

    review_event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/review-merge"},
    )
    review_result = WorkflowEngine(runtime).model_directive(review_event)
    review_artifact = next(
        artifact for artifact in review_result.artifacts if str(artifact.role) == "directive_result"
    )
    review = runtime.artifact_store.read_json(review_artifact.artifact_id)

    assert review_result.status == WorkflowStatus.COMPLETED
    assert review["approval_id"] == approval["approval_id"]
    assert review["mutated_approval"] is False
    assert review["review_packet"]["schema_version"] == "merge_review_packet.v1"
    assert "README.md" in review["report"]
    assert "2 files changed, 2 insertions(+)" in review["report"]
    assert "A\tfeature.py" in review["report"]
    assert "APPROVE" in review["report"]
    pending = run_coordination_command(
        ["list_approval_requests", "--status", "PENDING"], settings=runtime.settings
    )
    assert [item["approval_id"] for item in pending["requests"]] == [approval["approval_id"]]

    approve_event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": f"/approve-merge {approval['approval_id']}"},
    )
    approve_result = WorkflowEngine(runtime).model_directive(approve_event)
    approve_artifact = next(
        artifact
        for artifact in approve_result.artifacts
        if str(artifact.role) == "directive_result"
    )
    approved = runtime.artifact_store.read_json(approve_artifact.artifact_id)
    assert approve_result.status == WorkflowStatus.COMPLETED
    assert approved["code_merged"] is False
    assert approved["promotion_state"] == "MERGE_APPROVED"
    assert approved["next_required_state"] == "MERGED"
    assert [action["action"] for action in approved["next_actions"]] == [
        "merge_exact_approved_commit",
        "complete_milestone_after_merge",
        "dispatch_next_ready_milestone",
    ]
    assert "Next required transition: MERGE_APPROVED -> MERGED" in approved["report"]
    # The directive that used to end at "no code was merged by this command" now
    # also says where the commit went. `/approve-merge` is one of three surfaces
    # that resolve an approval and it queues through the same function as the
    # other two, so asserting it here is what proves the enqueue is bound to
    # resolution rather than to this directive.
    assert approved["integration_request_id"]
    assert f"Queued for integration as {approved['integration_request_id']}" in approved["report"]
    with connect() as connection:
        queued = read_integration_requests(connection, target_project_id="target")
    assert [request.subject.request_id for request in queued] == [
        approved["integration_request_id"]
    ]
    assert queued[0].subject.commit_sha == commit_sha
    assert queued[0].subject.approval_id == approval["approval_id"]
    assert f"then merge exact commit {commit_sha}" in approved["report"]
    assert "complete milestone milestone-3" in approved["report"]
    assert "Re-run the approved-GAWD path" in approved["report"]
    requests = run_coordination_command(
        ["list_approval_requests", "--saga-id", saga["saga_id"]],
        settings=runtime.settings,
    )
    assert requests["requests"][0]["status"] == "APPROVED"


def test_no_ready_milestone_reports_merged_dependency_completion_command(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(
        ["git", "config", "user.email", "milestone@example.com"],
        cwd=target,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Milestone Test"],
        cwd=target,
        check=True,
    )
    (target / "feature.py").write_text("READY = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "feature.py"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-qm", "approved change"], cwd=target, check=True)
    commit_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    guidance = _build_no_ready_milestone_guidance(
        gawd_doc_id="gawd-1",
        target_project_id="target",
        target_project_path=target,
        coordination_root=tmp_path / "coordination",
        saga_milestones=[
            {"milestone_id": "m3", "status": "IN_PROGRESS"},
            {"milestone_id": "m4", "status": "PENDING", "depends_on": ["m3"]},
        ],
        blocked_milestones=[
            {
                "milestone_id": "m4",
                "dependency_ready": False,
                "approval_ready": False,
                "depends_on": ["m3"],
            }
        ],
        approved_requests=[
            {
                "approval_id": "approval-3",
                "request_type": "CODE_MERGE",
                "payload": {
                    "milestone_id": "m3",
                    "commit_sha": commit_sha,
                    "manual_recovery": True,
                },
            }
        ],
    )

    assert guidance["blocker_details"][0]["unresolved_dependencies"] == [
        {"milestone_id": "m3", "status": "IN_PROGRESS"}
    ]
    assert [action["action"] for action in guidance["next_actions"]] == [
        "complete_merged_milestone",
        "dispatch_next_ready_milestone",
    ]
    assert guidance["next_step"].startswith("UV_CACHE_DIR=/tmp/uv-cache uv run python")
    assert "complete_saga_milestone m3" in guidance["report"]
    assert f"--root {tmp_path / 'coordination'}" in guidance["report"]
    assert "--outcome MANUAL_RECOVERY_COMPLETION" in guidance["report"]
    assert commit_sha in guidance["report"]
    assert "pi /start /approved-gawd gawd-1 --target-project target" in guidance["report"]


def test_approved_gawd_dispatch_brief_excludes_redundant_full_task_graph() -> None:
    graph_sentinel = "full-planner-graph-must-stay-in-the-ledger" * 5_000
    prompt = build_approved_gawd_dispatch_prompt(
        {
            "gawd_doc_id": "gawd-1",
            "saga_id": "saga-1",
            "goal": "Build the bounded milestone.",
            "constraints": ["Do not deploy."],
            "success_criteria": ["Tests pass."],
            "acceptance_criteria": ["No scope drift."],
            "task_graph": {"large_planner_detail": graph_sentinel},
        },
        {
            "milestone_id": "saga-1:m01",
            "sequence": 1,
            "name": "Bounded milestone",
            "description": "Implement only the first milestone.",
            "entry_criteria": ["Approved worktree."],
            "exit_criteria": ["Evidence recorded."],
            "required_artifacts": ["test_log"],
        },
    )

    assert "Task graph:" not in prompt
    assert graph_sentinel not in prompt
    assert "Durable plan reference:" in prompt
    assert "saga-1:m01" in prompt
    assert len(prompt) < 4_000


def test_bare_stop_shuts_down_whisper_service(runtime, monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        runtime.audio_transcriber,
        "stop_server",
        lambda: calls.append("stop") or {"status": "mock_stopped"},
    )
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/stop"},
    )

    result = WorkflowEngine(runtime).model_directive(event)

    assert result.status == WorkflowStatus.COMPLETED
    assert calls == ["stop"]


def test_compactor_is_not_a_promotable_general_role(runtime) -> None:
    runtime.model_manager.set_active_general_role(ModelRole.COMPACTOR)
    assert runtime.model_manager.effective_general_role() == ModelRole.GENERAL


def test_active_general_is_not_unloaded_after_shared_compactor_use(runtime) -> None:
    runtime.model_manager.set_active_general_role(ModelRole.GENERAL)

    with runtime.model_manager.loaded_session(ModelRole.COMPACTOR):
        assert ModelRole.COMPACTOR in runtime.model_manager.loaded_roles

    assert runtime.model_manager.effective_general_role() == ModelRole.GENERAL
    assert ModelRole.COMPACTOR in runtime.model_manager.loaded_roles


def test_direct_model_query_requires_loaded_role(runtime, monkeypatch) -> None:
    def fail_if_not_loaded(role):
        raise ModelNotLoadedError(role)

    monkeypatch.setattr(runtime.model_manager, "require_loaded", fail_if_not_loaded)
    event = normalize_prompt_event(
        "answer with the selected model",
        workspace_id=WorkspaceId.GENERAL.value,
    )
    event = event.model_copy(
        update={
            "payload": {
                **event.payload,
                "use_retrieval": False,
                "model_role": ModelRole.GENERAL_FALLBACK.value,
            }
        }
    )

    with pytest.raises(ModelNotLoadedError):
        WorkflowEngine(runtime).general_questions(event)


def test_model_directive_start_records_artifact(runtime) -> None:
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/start /ocr"},
    )
    result = WorkflowEngine(runtime).model_directive(event)
    assert result.status == WorkflowStatus.COMPLETED
    assert any(str(artifact.role) == "directive_result" for artifact in result.artifacts)


def test_status_directive_reports_model_load_state(runtime) -> None:
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/status"},
    )
    result = WorkflowEngine(runtime).model_directive(event)
    assert result.status == WorkflowStatus.COMPLETED
    artifact = next(a for a in result.artifacts if str(a.role) == "directive_result")
    payload = runtime.artifact_store.read_json(artifact.artifact_id)
    assert payload["action"] == "status"
    roles = {row["role"] for row in payload["llama"]["roles"]}
    assert {"general", "embedder"} <= roles
    assert "general" in payload["report"]
    assert "reachable" in payload["whisper"]


def test_model_directive_routes_chrome_control(runtime) -> None:
    class _FakeChromeTool:
        name = "chrome_devtools"
        writes_external_state = True

        def run(self, workflow_id, payload):
            return {
                "schema_version": "chrome_control_result.v1",
                "workflow_id": workflow_id,
                "action": payload["action"],
                "args": payload["args"],
                "invocations": [{"stdout": "[]", "returncode": 0}],
            }

    runtime.tool_registry.tools["chrome_devtools"] = _FakeChromeTool()
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/chrome list"},
    )
    result = WorkflowEngine(runtime).model_directive(event)
    assert result.workflow_type.value == "chrome_control"
    assert result.status == WorkflowStatus.COMPLETED
    payload = runtime.artifact_store.read_json(result.artifacts[0].artifact_id)
    assert payload["chrome"]["action"] == "list"


def test_chrome_structured_failure_is_preserved_in_artifact(runtime) -> None:
    from local_first_agent_os.chrome_devtools import ChromeControlFailure

    class _FailingChromeTool:
        name = "chrome_devtools"
        writes_external_state = True

        def run(self, workflow_id, payload):
            raise ChromeControlFailure(
                {
                    "schema_version": "chrome_control_result.v2",
                    "workflow_id": workflow_id,
                    "action": payload["action"],
                    "status": "blocked",
                    "transport": "mcp",
                    "process_generation": 0,
                    "duration_ms": 1,
                    "lifecycle_phase": "attach",
                    "evidence": {},
                    "error": {
                        "code": "browser_attach_failed",
                        "message": "No eligible browser accepted the attach.",
                    },
                    "v1": {
                        "schema_version": "chrome_control_result.v1",
                        "status": "failed",
                    },
                }
            )

    runtime.tool_registry.tools["chrome_devtools"] = _FailingChromeTool()
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/chrome list"},
    )
    result = WorkflowEngine(runtime).model_directive(event)

    assert result.status == WorkflowStatus.FAILED_PERMANENT
    artifact = runtime.artifact_store.read_json(result.artifacts[0].artifact_id)
    assert artifact["schema_version"] == "chrome_control_result.v2"
    assert artifact["status"] == "blocked"
    assert artifact["error"]["code"] == "browser_attach_failed"


def test_chrome_summarize_records_model_summary(runtime) -> None:
    class _FakeChromeTool:
        name = "chrome_devtools"
        writes_external_state = True

        def run(self, workflow_id, payload):
            return {
                "schema_version": "chrome_control_result.v1",
                "workflow_id": workflow_id,
                "action": payload["action"],
                "category": "docs",
                "matched_pages": [
                    {
                        "page_id": "1",
                        "title": "Docs",
                        "url": "https://example.com/docs",
                        "label": "Docs https://example.com/docs",
                    }
                ],
                "page_snapshots": [
                    {
                        "page_id": "1",
                        "title": "Docs",
                        "url": "https://example.com/docs",
                        "snapshot": "Documentation content",
                    }
                ],
                "invocations": [],
            }

    runtime.tool_registry.tools["chrome_devtools"] = _FakeChromeTool()
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/chrome summarize docs"},
    )
    result = WorkflowEngine(runtime).model_directive(event)
    assert result.workflow_type.value == "chrome_control"
    assert result.status == WorkflowStatus.COMPLETED
    chrome_artifact = next(
        artifact for artifact in result.artifacts if str(artifact.role) == "chrome_control_result"
    )
    payload = runtime.artifact_store.read_json(chrome_artifact.artifact_id)
    assert payload["chrome"]["summary"]["matched_page_count"] == 1
    assert "summary" in payload["chrome"]["summary"]


def test_chrome_decide_feeds_page_text_to_base_model(runtime) -> None:
    class _FakeChromeTool:
        name = "chrome_devtools"
        writes_external_state = True

        def run(self, workflow_id, payload):
            return {
                "schema_version": "chrome_control_result.v1",
                "workflow_id": workflow_id,
                "action": payload["action"],
                "category": "docs",
                "decision_prompt": "which tabs can I close?",
                "matched_pages": [
                    {
                        "page_id": "1",
                        "title": "Docs",
                        "url": "https://example.com/docs",
                        "label": "Docs https://example.com/docs",
                    }
                ],
                "page_snapshots": [
                    {
                        "page_id": "1",
                        "title": "Docs",
                        "url": "https://example.com/docs",
                        "snapshot": "Documentation content",
                    }
                ],
                "invocations": [],
            }

    runtime.tool_registry.tools["chrome_devtools"] = _FakeChromeTool()
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/chrome decide docs --prompt 'which tabs can I close?'"},
    )
    result = WorkflowEngine(runtime).model_directive(event)
    assert result.workflow_type.value == "chrome_control"
    assert result.status == WorkflowStatus.COMPLETED
    roles = [str(artifact.role) for artifact in result.artifacts]
    assert "normalized_text" in roles
    chrome_artifact = next(
        artifact for artifact in result.artifacts if str(artifact.role) == "chrome_control_result"
    )
    payload = runtime.artifact_store.read_json(chrome_artifact.artifact_id)
    assert payload["chrome"]["summary"]["matched_page_count"] == 1
    assert "decision" in payload["chrome"]["summary"]


def test_chrome_read_with_ocr_imports_screenshot(runtime, tmp_path) -> None:
    screenshot = tmp_path / "page.png"
    screenshot.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02"
        b"\x00\x00\x00\x90wS\xde"
    )

    class _FakeChromeTool:
        name = "chrome_devtools"
        writes_external_state = True

        def run(self, workflow_id, payload):
            return {
                "schema_version": "chrome_control_result.v1",
                "workflow_id": workflow_id,
                "action": payload["action"],
                "category": "docs",
                "matched_pages": [
                    {
                        "page_id": "1",
                        "title": "Docs",
                        "url": "https://example.com/docs",
                    }
                ],
                "page_snapshots": [],
                "page_screenshots": [
                    {
                        "page_id": "1",
                        "title": "Docs",
                        "url": "https://example.com/docs",
                        "path": str(screenshot),
                    }
                ],
                "invocations": [],
            }

    runtime.tool_registry.tools["chrome_devtools"] = _FakeChromeTool()
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/chrome read docs --ocr"},
    )
    result = WorkflowEngine(runtime).model_directive(event)
    assert result.workflow_type.value == "chrome_control"
    assert result.status == WorkflowStatus.COMPLETED
    roles = [str(artifact.role) for artifact in result.artifacts]
    assert "source_image" in roles
    assert "normalized_text" in roles


def test_start_store_embeds_directory(runtime, tmp_path) -> None:
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    (store_dir / "note.md").write_text("DBOS owns durable workflow state.", encoding="utf-8")
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": f"/start /store {store_dir}"},
    )
    result = WorkflowEngine(runtime).model_directive(event)
    assert result.status == WorkflowStatus.COMPLETED
    assert any(str(artifact.role) == "store_manifest" for artifact in result.artifacts)
    assert runtime.repository.dashboard_summary()["embedding_chunk_count"] >= 1


def test_plain_pi_text_runs_general_question(runtime, monkeypatch) -> None:
    from local_first_agent_os import pi_channel

    stub_session_memory(monkeypatch)

    class FakeRuntime:
        artifact_store = runtime.artifact_store
        model_registry = runtime.model_registry

    monkeypatch.setattr(pi_channel, "get_settings", lambda: runtime.settings)
    monkeypatch.setattr(pi_channel, "get_runtime", lambda: FakeRuntime())
    monkeypatch.setattr(
        pi_channel,
        "run_workflow",
        lambda _workflow_type, event: WorkflowEngine(runtime).general_questions(event),
    )
    results = run_terminal_query(
        "what owns workflow truth?",
        workspace_id=WorkspaceId.GENERAL.value,
    )
    assert results[0]["workflow_type"] == "general_questions"


def test_start_new_project_failed_finalization_fails_closed(runtime, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "local_first_agent_os.workflow.engine.resolve_project_repo_root",
        lambda: tmp_path,
    )
    runtime.settings.coordination_root = tmp_path / "coordination-root"
    draft_file = create_sparse_gawd_draft_file(tmp_path)

    class FailingExecutor:
        def dispatch_pow_wow(self, pow_wow_id, target_project, tasks, context):
            from local_first_agent_os.pow_wow import (
                PowWowRunResult,
                PowWowTaskResult,
            )

            return PowWowRunResult(
                executor="FailingExecutor",
                mode="cli",
                pow_wow_id=pow_wow_id,
                target_project_id=target_project.id,
                target_project_path=str(target_project.expanded_path),
                status="FAILED",
                output_summary="junior delegate failed",
                tasks=(
                    PowWowTaskResult(
                        task_name="junior_permissions_scan",
                        role="junior_permissions_scan",
                        status="failed",
                        summary="model not loaded",
                    ),
                ),
                risks=("Junior delegate failed: model not loaded",),
            )

    monkeypatch.setattr(
        "local_first_agent_os.workflow.engine.build_saga_executor",
        lambda settings, spec, **kwargs: (FailingExecutor(), "runtime_settings", None),
    )
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": f"/start /new-project {draft_file.path}"},
    )

    result = WorkflowEngine(runtime).model_directive(event)

    artifact = next(a for a in result.artifacts if str(a.role) == "directive_result")
    payload = runtime.artifact_store.read_json(artifact.artifact_id)
    assert payload["status"] == "finalization_failed"
    assert payload["approval_required"] is False
    assert payload["final_gawd_doc_id"] is None
    assert payload["saga_milestones"] == []
    assert "re-run" in payload["report"]
    # The failure report must not advertise reviewable artifacts.
    assert "final_gawd_doc_id" not in payload["report"]
    assert "available --target-project ids:" not in payload["report"]
    assert "workflow_plan_path" not in payload["report"]
    assert "failed_task: junior_permissions_scan: failed" in payload["report"]
    assert "cause: Junior delegate failed: model not loaded" in payload["report"]
    assert "failure_report:" in payload["report"]
    # The sidecar must not present a failed run as reviewable.
    finalized_text = Path(payload["finalized_path"]).read_text(encoding="utf-8")
    assert "**Status:** FINALIZATION_FAILED" in finalized_text
    assert "Do not approve this draft." in finalized_text

    with tx() as conn:
        docs = conn.execute("SELECT version FROM gawd_docs").fetchall()
        milestone_count = conn.execute("SELECT COUNT(*) AS count FROM saga_milestones").fetchone()[
            "count"
        ]
    # Only the sparse initial doc exists; nothing approvable was created.
    assert [doc["version"] for doc in docs] == [1]
    assert milestone_count == 0
