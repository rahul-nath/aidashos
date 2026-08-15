# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from refinery_support import build_target_repository, code_merge_payload, write_registry_config

from local_first_agent_os.contracts import ApprovalStatus
from local_first_agent_os.coordination.outcomes import (
    next_approval_statuses,
    require_approval_status_transition,
)
from local_first_agent_os.pow_wow import run_coordination_command
from local_first_agent_os.project_action import (
    ProjectActionKind,
    build_project_action_snapshot,
)
from local_first_agent_os.settings import Settings, get_settings


def _coord(root: Path, args: list[str]) -> dict[str, Any]:
    return run_coordination_command(args, root=root)


def test_approved_request_can_be_revoked_without_erasing_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "LOCAL_AGENT_LEDGER_OUTBOX",
        '{"mode":"configured","consumer":"approval-audit","topic":"coordination"}',
    )
    # Approving a CODE_MERGE now enqueues it for integration, and the enqueue
    # refuses an approval whose commit the target repository does not contain. So
    # this test needs a real repository and a real registry entry: the approval
    # it revokes has to be one that could have been landed, or the revocation it
    # is about would never be reachable.
    repository = build_target_repository(tmp_path / "target")
    write_registry_config(tmp_path / "configs", repository.path, project_id="target")
    monkeypatch.setenv("LOCAL_AGENT_CONFIG_DIR", str(tmp_path / "configs"))
    get_settings.cache_clear()

    root = tmp_path / "coord"
    saga = _coord(root, ["create_saga", "Protect an exact merge"])
    request = _coord(
        root,
        [
            "submit_approval_request",
            saga["saga_id"],
            "CODE_MERGE",
            "--requested-by",
            "staff-reviewer",
            "--payload",
            json.dumps(code_merge_payload(repository, project_id="target")),
        ],
    )
    _coord(
        root,
        [
            "resolve_approval_request",
            request["approval_id"],
            "approve",
            "--resolved-by",
            "operator",
        ],
    )

    revoked = _coord(
        root,
        [
            "revoke_approval_request",
            request["approval_id"],
            "--revoked-by",
            "operator",
            "--reason",
            "The target branch advanced.",
        ],
    )
    repeated = _coord(
        root,
        [
            "revoke_approval_request",
            request["approval_id"],
            "--revoked-by",
            "operator",
            "--reason",
            "The target branch advanced.",
        ],
    )

    assert revoked["status"] == ApprovalStatus.REVOKED
    assert revoked["previous_status"] == ApprovalStatus.APPROVED
    assert revoked["original_resolved_by"] == "operator"
    assert revoked["revoked_by"] == "operator"
    assert revoked["reason"] == "The target branch advanced."
    assert repeated["already_revoked"] is True

    listed = _coord(root, ["list_approval_requests", "--status", "REVOKED"])["requests"]
    assert len(listed) == 1
    assert listed[0]["approval_id"] == request["approval_id"]
    assert listed[0]["resolved_by"] == "operator"
    assert listed[0]["resolved_at"] == revoked["original_resolved_at"]
    assert _coord(root, ["list_approval_requests", "--status", "APPROVED"])["requests"] == []

    events = _coord(root, ["list_ledger_events", "--status", "PENDING"])["events"]
    revocations = [event for event in events if event["event_type"] == "revoke_approval_request"]
    assert len(revocations) == 1
    assert revocations[0]["payload"]["reason"] == "The target branch advanced."


def test_only_approved_requests_can_be_revoked(tmp_path: Path) -> None:
    root = tmp_path / "coord"
    saga = _coord(root, ["create_saga", "Protect a pending merge"])
    request = _coord(
        root,
        ["submit_approval_request", saga["saga_id"], "CODE_MERGE"],
    )

    with pytest.raises(RuntimeError, match="not_approved"):
        _coord(
            root,
            [
                "revoke_approval_request",
                request["approval_id"],
                "--revoked-by",
                "operator",
                "--reason",
                "No longer safe.",
            ],
        )


def test_approval_transition_matrix_prevents_reversal_and_skipping() -> None:
    assert next_approval_statuses(ApprovalStatus.PENDING) == frozenset(
        {
            ApprovalStatus.APPROVED,
            ApprovalStatus.DENIED,
        }
    )
    assert next_approval_statuses(ApprovalStatus.APPROVED) == frozenset({ApprovalStatus.REVOKED})
    assert next_approval_statuses(ApprovalStatus.REVOKED) == frozenset()

    with pytest.raises(ValueError, match="invalid approval status transition"):
        require_approval_status_transition(
            ApprovalStatus.PENDING,
            ApprovalStatus.REVOKED,
        )
    with pytest.raises(ValueError, match="invalid approval status transition"):
        require_approval_status_transition(
            ApprovalStatus.REVOKED,
            ApprovalStatus.APPROVED,
        )


class _ProjectActionSource:
    def __init__(self, facts: dict[str, Any]):
        self._facts = facts

    def read_project_action_facts(self, project_id: str) -> dict[str, Any]:
        assert project_id == "project-1"
        return self._facts


def test_revoked_merge_approval_blocks_project_action() -> None:
    saga_id = "saga-1"
    milestone_id = f"{saga_id}:m01"
    facts = {
        "project": {
            "id": "project-1",
            "path": "/tmp/project-1",
            "exists": True,
            "git_repo": True,
            "branch": "main",
            "head_sha": "commit-1",
        },
        "sagas": [
            {
                "saga_id": saga_id,
                "gawd_doc_id": "gawd-1",
                "status": "EXECUTING",
                "updated_at": "2026-07-29T12:00:00+00:00",
            }
        ],
        "milestones": [
            {
                "milestone_id": milestone_id,
                "name": "Merge",
                "sequence": 1,
                "status": "PENDING",
            }
        ],
        "intents": [],
        "leases": [],
        "checkpoints": [],
        "approvals": [
            {
                "approval_id": "approval-1",
                "saga_id": saga_id,
                "request_type": "CODE_MERGE",
                "status": ApprovalStatus.REVOKED,
                "created_at": "2026-07-29T12:01:00+00:00",
                "payload": {
                    "target_project_id": "project-1",
                    "milestone_id": milestone_id,
                    "commit_sha": "commit-1",
                },
            }
        ],
    }

    result = build_project_action_snapshot(
        "project-1",
        settings=Settings(mock_models=True),
        source=_ProjectActionSource(facts),
        generated_at=datetime(2026, 7, 29, 12, 2, tzinfo=UTC),
    )

    assert result.action is ProjectActionKind.BLOCKED
    assert result.next_command is None
    assert result.approval and result.approval.status == ApprovalStatus.REVOKED
    assert result.warnings == ["Approval approval-1 is REVOKED."]
