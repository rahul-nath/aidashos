# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import pytest

from local_first_agent_os.contracts import WorkflowStatus, WorkflowType, WorkspaceId
from local_first_agent_os.ids import build_workflow_id, sha256_text
from local_first_agent_os.ingress import BoundsError, normalize_file_event, normalize_prompt_event


def test_prompt_event_is_stable() -> None:
    first = normalize_prompt_event("same prompt")
    second = normalize_prompt_event("same prompt")
    assert first.content_sha256 == second.content_sha256
    assert first.event_id == second.event_id


def test_workflow_id_version_boundary() -> None:
    digest = sha256_text("abc")
    first = build_workflow_id(
        "whiteboard_ocr",
        WorkspaceId.WHITEBOARD_OCR.value,
        "file",
        digest,
        "v1",
    )
    second = build_workflow_id(
        "whiteboard_ocr",
        WorkspaceId.WHITEBOARD_OCR.value,
        "file",
        digest,
        "v2",
    )
    assert first != second
    assert first.endswith(":v1")


def test_bounds_reject_unsupported_file(tmp_path: Path) -> None:
    source = tmp_path / "bad.txt"
    source.write_text("not an image", encoding="utf-8")
    with pytest.raises(BoundsError) as exc:
        normalize_file_event(
            path=source,
            workspace_id=WorkspaceId.GENERAL.value,
            workflow_type=WorkflowType.WHITEBOARD_OCR,
        )
    assert exc.value.terminal_status == WorkflowStatus.FAILED_PERMANENT
