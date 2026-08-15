# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import pytest

from local_first_agent_os.contracts import PiTask, WorkflowStatus, WorkflowType, WorkspaceId
from local_first_agent_os.ingress import normalize_file_event
from local_first_agent_os.workflow import WorkflowEngine


def test_policy_denies_forbidden_tool(runtime) -> None:
    with pytest.raises(PermissionError):
        runtime.policy_store.ensure_tool_allowed(WorkspaceId.GENERAL.value, "bash")


def test_pi_requires_manual_review_without_targets(runtime) -> None:
    decision = runtime.pi.run_decision(
        PiTask(
            workflow_id="wf-pi",
            workspace_id=WorkspaceId.WORKFLOWY.value,
            task_type="choose_workflowy_destination",
            prompt="route this",
            allowed_tools=["search_embeddings"],
            output_schema="workflowy_destination_decision.v1",
        )
    )
    assert decision.action == "manual_review"
    assert decision.requires_manual_review is True


def test_medical_report_is_review_required(runtime, tmp_path: Path) -> None:
    source = tmp_path / "image.png"
    source.write_bytes(b"not-real-png-but-content-addressed")
    event = normalize_file_event(
        path=source,
        workspace_id=WorkspaceId.MEDICAL.value,
        workflow_type=WorkflowType.MEDICAL_IMAGE_ANALYZER,
    )
    result = WorkflowEngine(runtime).medical_image_analyzer(event)
    assert result.status == WorkflowStatus.MANUAL_REVIEW
    assert result.manual_review_reason is not None
