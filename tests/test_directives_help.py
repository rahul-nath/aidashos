# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from pathlib import Path

from local_first_agent_os.contracts import SourceType, WorkflowStatus, WorkspaceId
from local_first_agent_os.directives import DirectiveParser
from local_first_agent_os.directives_help import explain_failure, help_payload
from local_first_agent_os.ingress import normalize_scheduled_event
from local_first_agent_os.workflow import WorkflowEngine


def test_help_for_unknown_directive_suggests_known(runtime) -> None:
    parser = DirectiveParser(runtime.settings)
    block = explain_failure(parser, "/strt /ocr", "Unsupported directive: /strt")
    assert any("/start" in suggestion for suggestion in block.suggestions)
    assert "/start" in block.canonical_examples[0]


def test_help_for_missing_store_path_explains_grammar(runtime) -> None:
    parser = DirectiveParser(runtime.settings)
    block = explain_failure(parser, "/store", "/store requires a local file or directory path.")
    assert "path" in block.summary.lower()
    assert any("/store" in example for example in block.canonical_examples)


def test_help_for_unknown_alias_suggests_local_models(runtime, tmp_path: Path) -> None:
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "qwen3.8-27b-mtp").mkdir()
    (models_dir / "chandra-ocr-2").mkdir()
    runtime.settings.llama_models_dir = models_dir
    parser = DirectiveParser(runtime.settings)
    block = explain_failure(parser, "/start /unknown", "Unknown model alias")
    suggestions = block.suggestions
    assert any(
        "qwen3.8-27b-mtp" in suggestion or "chandra-ocr-2" in suggestion
        for suggestion in suggestions
    )


def test_help_payload_returns_jsonable_dict(runtime) -> None:
    parser = DirectiveParser(runtime.settings)
    payload = help_payload(parser, "/strt", "Unsupported directive: /strt")
    assert isinstance(payload, dict)
    assert "summary" in payload
    json.dumps(payload)


def test_invalid_directive_workflow_returns_help_field(runtime) -> None:
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/frobnicate"},
    )
    result = WorkflowEngine(runtime).model_directive(event)
    assert result.status == WorkflowStatus.FAILED_PERMANENT
    assert result.help is not None
    assert "summary" in result.help
    assert "/start" in (result.help.get("canonical_examples") or [""])[0]


def test_missing_store_path_returns_help_field(runtime) -> None:
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/store"},
    )
    result = WorkflowEngine(runtime).model_directive(event)
    assert result.status == WorkflowStatus.FAILED_PERMANENT
    assert result.help is not None
    assert "path" in result.help["summary"].lower()


def test_store_with_nonexistent_path_returns_help_field(runtime, tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": f"/store {missing}"},
    )
    result = WorkflowEngine(runtime).model_directive(event)
    assert result.status == WorkflowStatus.FAILED_PERMANENT
    assert result.help is not None
    suggestions = result.help.get("suggestions", [])
    assert any(
        "path" in s.lower() or "directory" in s.lower() or "file" in s.lower() for s in suggestions
    )


def test_store_empty_directory_returns_help_field(runtime, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": f"/store {empty}"},
    )
    result = WorkflowEngine(runtime).model_directive(event)
    assert result.status == WorkflowStatus.FAILED_PERMANENT
    assert result.help is not None


def test_help_for_get_without_query(runtime) -> None:
    parser = DirectiveParser(runtime.settings)
    block = explain_failure(parser, "/get", "/get expects a query string after the directive.")
    assert any("/get" in example for example in block.canonical_examples)


def test_help_for_fetch_mentions_workflowy(runtime) -> None:
    parser = DirectiveParser(runtime.settings)
    block = explain_failure(
        parser,
        "/fetch",
        "/fetch currently requires the /workflowy source selector.",
    )
    assert any("/fetch /workflowy" in suggestion for suggestion in block.suggestions)


def test_help_for_chrome_mentions_page_crud(runtime) -> None:
    parser = DirectiveParser(runtime.settings)
    block = explain_failure(parser, "/chrome nope", "unknown chrome action")
    assert "/chrome" in block.summary
    assert any("/chrome open" in suggestion for suggestion in block.suggestions)
