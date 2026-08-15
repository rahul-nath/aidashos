# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from local_first_agent_os.capability_gate import SystemWorkflow
from local_first_agent_os.contracts import SourceType, WorkflowType, WorkspaceId
from local_first_agent_os.ingress import normalize_scheduled_event
from local_first_agent_os.tools import AppleNotesFetchTool, ChromeDevToolsTool
from local_first_agent_os.workflow import WorkflowEngine


def _use_chrome_cli_transport(runtime) -> None:
    runtime.settings.chrome_devtools_transport = "cli"
    runtime.settings.chrome_devtools_command = "chrome-devtools"
    runtime.settings.chrome_devtools_command_args = []


def test_workflowy_parent_allow_list_is_enforced(runtime) -> None:
    policy = runtime.policy_store.get(WorkspaceId.WORKFLOWY.value)
    runtime.policy_store._policies[WorkspaceId.WORKFLOWY.value] = policy.model_copy(  # type: ignore[attr-defined]
        update={"write_enabled": True, "approved_workflowy_parent_ids": ["allowed-1"]}
    )
    runtime.policy_store.ensure_workflowy_parent_allowed(WorkspaceId.WORKFLOWY.value, "allowed-1")
    with pytest.raises(PermissionError):
        runtime.policy_store.ensure_workflowy_parent_allowed(
            WorkspaceId.WORKFLOWY.value,
            "not-allowed",
        )


def test_path_containment_allows_root_eq_home(runtime, tmp_path: Path) -> None:
    """The default 'general' policy uses Path.home() as root, which short-circuits
    containment by design. Make the assertion explicit."""

    runtime.policy_store.ensure_path_in_workspace(
        WorkspaceId.GENERAL.value,
        tmp_path / "anywhere.txt",
    )


def test_path_containment_blocks_outside_workspace(runtime, tmp_path: Path) -> None:
    workspace_root = tmp_path / "constrained"
    workspace_root.mkdir()
    policy = runtime.policy_store.get(WorkspaceId.WHITEBOARD_OCR.value)
    runtime.policy_store._policies[WorkspaceId.WHITEBOARD_OCR.value] = policy.model_copy(  # type: ignore[attr-defined]
        update={"root_path": workspace_root}
    )
    inside = workspace_root / "inside.png"
    inside.write_bytes(b"png")
    runtime.policy_store.ensure_path_in_workspace(WorkspaceId.WHITEBOARD_OCR.value, inside)
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"png")
    with pytest.raises(PermissionError):
        runtime.policy_store.ensure_path_in_workspace(WorkspaceId.WHITEBOARD_OCR.value, outside)


def test_day_bullet_tool_is_allowed_in_workflowy_workspace(runtime) -> None:
    runtime.policy_store.ensure_tool_allowed(
        WorkspaceId.WORKFLOWY.value,
        "workflowy_day_bullet_insert",
    )


def test_day_bullet_tool_is_denied_in_general_workspace(runtime) -> None:
    with pytest.raises(PermissionError):
        runtime.policy_store.ensure_tool_allowed(
            WorkspaceId.GENERAL.value,
            "workflowy_day_bullet_insert",
        )


def test_chrome_tool_is_allowed_only_in_chrome_workspace(runtime) -> None:
    runtime.policy_store.ensure_tool_allowed(
        WorkspaceId.CHROME.value,
        "chrome_devtools",
    )
    with pytest.raises(PermissionError):
        runtime.policy_store.ensure_tool_allowed(
            WorkspaceId.GENERAL.value,
            "chrome_devtools",
        )


def test_apple_notes_tool_dry_run(runtime) -> None:
    tool = AppleNotesFetchTool(runtime.settings)
    output = tool.run("wf-1", {})
    assert output["schema_version"] == "apple_notes_snapshot.v1"
    assert output.get("dry_run") is True


def test_apple_notes_tool_with_export_path(runtime, tmp_path: Path) -> None:
    export = tmp_path / "notes.md"
    export.write_text("Pi imports notes durably.", encoding="utf-8")
    tool = AppleNotesFetchTool(runtime.settings)
    output = tool.run("wf-1", {"export_path": str(export)})
    assert output["schema_version"] == "apple_notes_snapshot.v1"
    assert output["notes"]
    assert "Pi imports notes durably." in output["notes"][0]["body"]


def test_chrome_tool_builds_page_scoped_commands(runtime, monkeypatch) -> None:
    _use_chrome_cli_transport(runtime)
    runtime.settings.chrome_devtools_auto_start = False
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    output = ChromeDevToolsTool(runtime.settings).run(
        "wf-1",
        {"action": "navigate", "args": ["--page", "2", "https://example.com"]},
    )
    assert output["schema_version"] == "chrome_control_result.v1"
    assert calls == [
        ["chrome-devtools", "select_page", "2", "--bringToFront"],
        [
            "chrome-devtools",
            "navigate_page",
            "--type",
            "url",
            "--url",
            "https://example.com",
        ],
    ]


def test_chrome_tool_gathers_tabs_by_category(runtime, monkeypatch) -> None:
    _use_chrome_cli_transport(runtime)
    runtime.settings.chrome_devtools_auto_start = False

    def fake_run(command, **_kwargs):
        if command[1] == "list_pages":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "## Pages\n"
                    "1: OpenAI Platform Docs https://platform.openai.com/docs [selected]\n"
                    "2: Mail https://mail.google.com\n"
                    "3: Local App http://localhost:5173\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    output = ChromeDevToolsTool(runtime.settings).run(
        "wf-1",
        {"action": "gather", "args": ["openai", "docs"]},
    )
    assert output["match_count"] == 1
    assert output["matched_pages"][0]["page_id"] == "1"


def test_chrome_tool_close_category_requires_confirmation(runtime, monkeypatch) -> None:
    _use_chrome_cli_transport(runtime)
    runtime.settings.chrome_devtools_auto_start = False
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[1] == "list_pages":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=(
                    "## Pages\n"
                    "1: OpenAI Platform Docs https://platform.openai.com/docs\n"
                    "2: Mail https://mail.google.com\n"
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    dry_run = ChromeDevToolsTool(runtime.settings).run(
        "wf-1",
        {"action": "close_category", "args": ["openai"]},
    )
    assert dry_run["dry_run"] is True
    assert dry_run["closed_page_ids"] == []
    assert all(call[1] != "close_page" for call in calls)

    confirmed = ChromeDevToolsTool(runtime.settings).run(
        "wf-1",
        {"action": "close_category", "args": ["openai", "--yes"]},
    )
    assert confirmed["dry_run"] is False
    assert confirmed["closed_page_ids"] == ["1"]
    assert ["chrome-devtools", "close_page", "1"] in calls


def test_chrome_tool_summarize_collects_snapshots(runtime, monkeypatch) -> None:
    _use_chrome_cli_transport(runtime)
    runtime.settings.chrome_devtools_auto_start = False
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[1] == "list_pages":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="## Pages\n1: Docs https://example.com/docs\n",
                stderr="",
            )
        if command[1] == "take_snapshot":
            return subprocess.CompletedProcess(command, 0, stdout="Docs page content", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    output = ChromeDevToolsTool(runtime.settings).run(
        "wf-1",
        {"action": "summarize", "args": ["docs"]},
    )
    assert output["snapshot_count"] == 1
    assert output["page_snapshots"][0]["snapshot"] == "Docs page content"
    assert ["chrome-devtools", "select_page", "1"] in calls


def test_chrome_tool_read_can_capture_snapshot_and_ocr_screenshot(runtime, monkeypatch) -> None:
    _use_chrome_cli_transport(runtime)
    runtime.settings.chrome_devtools_auto_start = False
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[1] == "list_pages":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="## Pages\n1: Docs https://example.com/docs\n",
                stderr="",
            )
        if command[1] == "take_snapshot":
            return subprocess.CompletedProcess(command, 0, stdout="Docs page content", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    output = ChromeDevToolsTool(runtime.settings).run(
        "wf-1",
        {"action": "read", "args": ["docs", "--ocr"]},
    )
    assert output["snapshot_count"] == 1
    assert output["screenshot_count"] == 1
    assert output["page_snapshots"][0]["snapshot"] == "Docs page content"
    assert any(call[1] == "take_screenshot" for call in calls)


def test_chrome_tool_decide_keeps_prompt_separate_from_category(runtime, monkeypatch) -> None:
    _use_chrome_cli_transport(runtime)
    runtime.settings.chrome_devtools_auto_start = False

    def fake_run(command, **_kwargs):
        if command[1] == "list_pages":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout="## Pages\n1: OpenAI Docs https://platform.openai.com/docs\n",
                stderr="",
            )
        if command[1] == "take_snapshot":
            return subprocess.CompletedProcess(command, 0, stdout="API reference text", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    output = ChromeDevToolsTool(runtime.settings).run(
        "wf-1",
        {
            "action": "decide",
            "args": ["openai", "--prompt", "which tabs can I close?"],
        },
    )
    assert output["category"] == "openai"
    assert output["decision_prompt"] == "which tabs can I close?"
    assert output["match_count"] == 1


def test_chrome_tool_restarts_daemon_when_connection_flags_are_missing(
    runtime,
    monkeypatch,
) -> None:
    _use_chrome_cli_transport(runtime)
    runtime.settings.chrome_devtools_auto_start = True
    runtime.settings.chrome_devtools_attach_mode = "auto_connect"
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[1] == "status":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout='chrome-devtools-mcp daemon is running.\nargs=["--viaCli"]',
                stderr="",
            )
        if command[1] == "list_pages":
            return subprocess.CompletedProcess(command, 0, stdout="## Pages\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ChromeDevToolsTool(runtime.settings).run("wf-1", {"action": "gather", "args": ["docs"]})
    assert [
        "chrome-devtools",
        "start",
        "--auto-connect",
        "--no-usage-statistics",
    ] in calls


def test_tool_call_records_success_and_failure_rows(runtime) -> None:
    runtime.tool_registry.run(
        workflow_id="wf-success",
        workspace_id=WorkspaceId.APPLE_NOTES.value,
        caller=SystemWorkflow(source="test"),
        tool_name="apple_notes_fetch",
        payload={},
        enforce_policy=False,
    )

    class _BoomTool:
        name = "boom"
        writes_external_state = False

        def run(self, workflow_id, payload):
            raise RuntimeError("simulated failure")

    runtime.tool_registry.tools["boom"] = _BoomTool()
    with pytest.raises(RuntimeError):
        runtime.tool_registry.run(
            workflow_id="wf-fail",
            workspace_id=WorkspaceId.APPLE_NOTES.value,
            caller=SystemWorkflow(source="test"),
            tool_name="boom",
            payload={"x": 1},
            enforce_policy=False,
        )

    from sqlalchemy import select

    from local_first_agent_os.db import ToolCallRow

    with runtime.database.session() as session:
        rows = list(session.scalars(select(ToolCallRow)).all())
    statuses = {row.status for row in rows}
    assert "completed" in statuses
    assert "failed" in statuses


def test_workflowy_write_records_egress_dedupe_status(runtime, monkeypatch) -> None:
    monkeypatch.delenv("WF_API_KEY", raising=False)
    policy = runtime.policy_store.get(WorkspaceId.WORKFLOWY.value)
    runtime.policy_store._policies[WorkspaceId.WORKFLOWY.value] = policy.model_copy(  # type: ignore[attr-defined]
        update={"write_enabled": True, "approved_workflowy_parent_ids": ["p-1"]}
    )
    event = normalize_scheduled_event(
        source_type=SourceType.WORKFLOWY,
        workspace_id=WorkspaceId.WORKFLOWY.value,
        event_type="workflowy.write_request",
        payload={"parent_node_id": "p-1", "content": "Idempotent egress check"},
    )
    WorkflowEngine(runtime).workflowy_write(event)
    event_2 = normalize_scheduled_event(
        source_type=SourceType.WORKFLOWY,
        workspace_id=WorkspaceId.WORKFLOWY.value,
        event_type="workflowy.write_request",
        payload={"parent_node_id": "p-1", "content": "Idempotent egress check"},
    )
    WorkflowEngine(runtime).workflowy_write(event_2)
    summary = runtime.repository.dashboard_summary()
    assert summary["deduped_egress_count"] >= 1
    assert summary["egress_write_count"] >= 1


def test_engine_routes_done_directive(runtime) -> None:
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": "/done embeddings"},
    )
    result = WorkflowEngine(runtime).model_directive(event)
    assert result.workflow_type == WorkflowType.DONE_RECALL
