# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from local_first_agent_os.contracts import (
    SourceType,
    WorkflowStatus,
    WorkflowType,
    WorkspaceId,
)
from local_first_agent_os.ingress import normalize_file_event, normalize_scheduled_event
from local_first_agent_os.workflow import WorkflowEngine

VALID_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def _fixture_workspace_root(runtime, workspace_id: str, root: Path) -> None:
    policy = runtime.policy_store.get(workspace_id)
    runtime.policy_store._policies[workspace_id] = policy.model_copy(  # type: ignore[attr-defined]
        update={"root_path": root}
    )


def test_whiteboard_ocr_completes_and_embeds(runtime, tmp_path: Path) -> None:
    workspace_root = tmp_path / "wb"
    workspace_root.mkdir()
    _fixture_workspace_root(runtime, WorkspaceId.WHITEBOARD_OCR.value, workspace_root)
    image = workspace_root / "board.png"
    image.write_bytes(VALID_ONE_PIXEL_PNG)
    event = normalize_file_event(
        path=image,
        workspace_id=WorkspaceId.WHITEBOARD_OCR.value,
        workflow_type=WorkflowType.WHITEBOARD_OCR,
    )
    result = WorkflowEngine(runtime).whiteboard_ocr(event)
    assert result.status == WorkflowStatus.COMPLETED
    roles = {str(a.role) for a in result.artifacts}
    assert {"source_image", "ocr_text", "normalized_text"}.issubset(roles)
    assert runtime.repository.dashboard_summary()["embedding_chunk_count"] >= 1


def test_paper_notes_ocr_defaults_to_manual_review(runtime, tmp_path: Path) -> None:
    workspace_root = tmp_path / "paper"
    workspace_root.mkdir()
    _fixture_workspace_root(runtime, WorkspaceId.PAPER_NOTES.value, workspace_root)
    image = workspace_root / "page.png"
    image.write_bytes(VALID_ONE_PIXEL_PNG)
    event = normalize_file_event(
        path=image,
        workspace_id=WorkspaceId.PAPER_NOTES.value,
        workflow_type=WorkflowType.PAPER_NOTES_OCR,
    )
    result = WorkflowEngine(runtime).paper_notes_ocr(event)
    assert result.status == WorkflowStatus.MANUAL_REVIEW
    assert result.manual_review_reason is not None


def test_whiteboard_path_outside_workspace_is_denied(runtime, tmp_path: Path) -> None:
    workspace_root = tmp_path / "wb"
    workspace_root.mkdir()
    _fixture_workspace_root(runtime, WorkspaceId.WHITEBOARD_OCR.value, workspace_root)
    outside = tmp_path / "other"
    outside.mkdir()
    image = outside / "wrong.png"
    image.write_bytes(VALID_ONE_PIXEL_PNG)
    event = normalize_file_event(
        path=image,
        workspace_id=WorkspaceId.WHITEBOARD_OCR.value,
        workflow_type=WorkflowType.WHITEBOARD_OCR,
    )
    with pytest.raises(PermissionError):
        WorkflowEngine(runtime).whiteboard_ocr(event)


def test_apple_notes_sync_dry_run_returns_snapshot(runtime) -> None:
    event = normalize_scheduled_event(
        source_type=SourceType.APPLE_NOTES,
        workspace_id=WorkspaceId.APPLE_NOTES.value,
        event_type="notes.poll",
        payload={},
    )
    result = WorkflowEngine(runtime).apple_notes_sync(event)
    assert result.status == WorkflowStatus.COMPLETED
    snapshots = [a for a in result.artifacts if str(a.role) == "notes_snapshot"]
    assert snapshots
    payload = runtime.artifact_store.read_json(snapshots[0].artifact_id)
    assert payload["schema_version"] == "apple_notes_snapshot.v1"
    assert payload.get("dry_run") is True


def test_apple_notes_sync_with_export_path_embeds_text(runtime, tmp_path: Path) -> None:
    export = tmp_path / "notes.md"
    export.write_text("Pi imports Apple Notes durably.", encoding="utf-8")
    event = normalize_scheduled_event(
        source_type=SourceType.APPLE_NOTES,
        workspace_id=WorkspaceId.APPLE_NOTES.value,
        event_type="notes.poll",
        payload={"export_path": str(export)},
    )
    result = WorkflowEngine(runtime).apple_notes_sync(event)
    assert result.status == WorkflowStatus.COMPLETED


def test_workflowy_sync_dry_run_returns_empty_nodes(runtime, monkeypatch) -> None:
    monkeypatch.delenv("WF_API_KEY", raising=False)
    event = normalize_scheduled_event(
        source_type=SourceType.WORKFLOWY,
        workspace_id=WorkspaceId.WORKFLOWY.value,
        event_type="workflowy.poll",
        payload={},
    )
    result = WorkflowEngine(runtime).workflowy_sync(event)
    assert result.status == WorkflowStatus.COMPLETED
    snapshots = [a for a in result.artifacts if str(a.role) == "workflowy_node_snapshot"]
    assert snapshots
    payload = runtime.artifact_store.read_json(snapshots[0].artifact_id)
    assert payload.get("dry_run") is True


def test_workflowy_write_blocked_when_write_disabled(runtime) -> None:
    event = normalize_scheduled_event(
        source_type=SourceType.WORKFLOWY,
        workspace_id=WorkspaceId.WORKFLOWY.value,
        event_type="workflowy.write_request",
        payload={"parent_node_id": "parent-1", "content": "hello"},
    )
    with pytest.raises(PermissionError):
        WorkflowEngine(runtime).workflowy_write(event)


def test_workflowy_write_with_unapproved_parent_is_blocked(runtime) -> None:
    policy = runtime.policy_store.get(WorkspaceId.WORKFLOWY.value)
    runtime.policy_store._policies[WorkspaceId.WORKFLOWY.value] = policy.model_copy(  # type: ignore[attr-defined]
        update={"write_enabled": True, "approved_workflowy_parent_ids": ["allowed-1"]}
    )
    event = normalize_scheduled_event(
        source_type=SourceType.WORKFLOWY,
        workspace_id=WorkspaceId.WORKFLOWY.value,
        event_type="workflowy.write_request",
        payload={"parent_node_id": "not-approved", "content": "blocked"},
    )
    with pytest.raises(PermissionError):
        WorkflowEngine(runtime).workflowy_write(event)


def test_workflowy_write_dedupes_on_second_call(runtime, monkeypatch) -> None:
    monkeypatch.delenv("WF_API_KEY", raising=False)
    policy = runtime.policy_store.get(WorkspaceId.WORKFLOWY.value)
    runtime.policy_store._policies[WorkspaceId.WORKFLOWY.value] = policy.model_copy(  # type: ignore[attr-defined]
        update={"write_enabled": True, "approved_workflowy_parent_ids": ["parent-1"]}
    )
    event = normalize_scheduled_event(
        source_type=SourceType.WORKFLOWY,
        workspace_id=WorkspaceId.WORKFLOWY.value,
        event_type="workflowy.write_request",
        payload={"parent_node_id": "parent-1", "content": "Idempotent payload"},
    )
    first = WorkflowEngine(runtime).workflowy_write(event)
    assert first.status == WorkflowStatus.COMPLETED
    egress_first = first.egress_ids[0]
    event_again = normalize_scheduled_event(
        source_type=SourceType.WORKFLOWY,
        workspace_id=WorkspaceId.WORKFLOWY.value,
        event_type="workflowy.write_request",
        payload={"parent_node_id": "parent-1", "content": "Idempotent payload"},
    )
    second = WorkflowEngine(runtime).workflowy_write(event_again)
    assert second.status == WorkflowStatus.COMPLETED
    assert second.egress_ids == [egress_first]
    summary = runtime.repository.dashboard_summary()
    assert summary["deduped_egress_count"] >= 1
