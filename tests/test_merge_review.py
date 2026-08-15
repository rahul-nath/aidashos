# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from local_first_agent_os import merge_review
from local_first_agent_os.engineering_doctrine import CURRENT_ENGINEERING_DOCTRINE


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_manual_recovery_approval_hydrates_complete_review_packet(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "review@example.com")
    _git(repo, "config", "user.name", "Review Test")
    feature = repo / "feature.py"
    feature.write_text("READY = False\n", encoding="utf-8")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-qm", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    feature.write_text("READY = True\n", encoding="utf-8")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-qm", "manual recovery")
    commit_sha = _git(repo, "rev-parse", "HEAD")

    monkeypatch.setattr(
        merge_review,
        "run_coordination_command",
        lambda *_args, **_kwargs: {
            "intents": [
                {
                    "intent_id": "intent-1",
                    "target_project_id": "target",
                    "status": "PAUSED",
                    "result": None,
                }
            ]
        },
    )
    monkeypatch.setattr(merge_review, "_project_path", lambda *_args: repo)
    approval = {
        "approval_id": "approval-1",
        "saga_id": "saga-1",
        "requested_by": "manual-recovery",
        "payload": {
            "schema_version": "code_merge_request.v1",
            "manual_recovery": True,
            "intent_id": "intent-1",
            "target_project_id": "target",
            "purpose": "Merge reviewed manual recovery",
            "branch": "agent/recovery",
            "base_sha": base_sha,
            "commit_sha": commit_sha,
            "changed_files": ["feature.py"],
            "verification": ["pytest -q -> passed", "git diff --check -> passed"],
            "staff_review": {
                "initial_verdict": "BLOCK",
                "initial_finding": "The edit path could revert fields.",
                "verdict": "APPROVE",
                "resolution": "The invariant and E2E proof now cover the edit path.",
                "risks": ["Manual preview review remains required."],
            },
        },
    }

    packet = merge_review.review_packet_for_approval(approval, settings=object())
    rendered = merge_review.render_merge_review_packet(packet)

    assert packet["executor_status"] == "MANUAL_RECOVERY_REVIEWED"
    assert packet["dispatch_result_origin"] == "MANUAL_RECOVERY"
    assert packet["dispatch_result_state"] == "REVIEWED"
    assert packet["promotion_state"] == "REVIEWED"
    assert packet["summary"] == "Merge reviewed manual recovery"
    assert packet["changed_files"] == ["feature.py"]
    assert [row["command"] for row in packet["verification"]] == [
        "pytest -q -> passed",
        "git diff --check -> passed",
    ]
    assert packet["reviews"][0]["status"] == "APPROVE"
    assert "VERDICT: APPROVE" in packet["reviews"][0]["verdict"]
    assert packet["risks"] == ["Manual preview review remains required."]
    assert packet["diffs"][0]["base_head_sha"] == base_sha
    assert packet["diffs"][0]["commit_sha"] == commit_sha
    assert "feature.py" in packet["diffs"][0]["name_status"]
    assert "No verification evidence recorded" not in rendered
    assert "No independent reviewer result recorded" not in rendered
    assert "VERDICT: APPROVE" in rendered
    assert "Manual preview review remains required." in rendered
    assert "Result origin: MANUAL_RECOVERY" in rendered
    assert "Promotion state: REVIEWED" in rendered


def test_manual_operator_evidence_cannot_approve_code_merge(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "review@example.com")
    _git(repo, "config", "user.name", "Review Test")
    (repo / "feature.py").write_text("READY = False\n", encoding="utf-8")
    _git(repo, "add", "feature.py")
    _git(repo, "commit", "-qm", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "feature.py").write_text("READY = True\n", encoding="utf-8")
    _git(repo, "commit", "-am", "manual recovery")
    commit_sha = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(
        merge_review,
        "run_coordination_command",
        lambda *_args, **_kwargs: {"intents": []},
    )
    approval = {
        "payload": {
            "manual_recovery": True,
            "intent_id": "intent-1",
            "target_project_id": "target",
            "branch": "agent/recovery",
            "base_sha": base_sha,
            "commit_sha": commit_sha,
            "staff_review": {"verdict": "APPROVE"},
        }
    }

    with pytest.raises(ValueError, match="expected MERGE_PENDING"):
        merge_review.require_staff_review_provenance(approval, settings=object())


def test_staff_review_must_name_the_exact_approved_commit(monkeypatch: Any) -> None:
    base_sha = "a" * 40
    approved_commit = "b" * 40
    reviewed_commit = "c" * 40
    monkeypatch.setattr(
        merge_review,
        "run_coordination_command",
        lambda *_args, **_kwargs: {"intents": []},
    )
    approval = {
        "payload": {
            "base_sha": base_sha,
            "commit_sha": approved_commit,
            "dispatch_result": {
                "schema_version": "dispatch_runner_result.v1",
                "result_origin": "AUTOMATED_RECOVERY",
                "result_state": "COMPLETED",
                "promotion_state": "MERGE_PENDING",
                "run_result": {
                    "status": "COMPLETED",
                    "tasks": [
                        {
                            "task_name": "recovery_staff_review",
                            "artifacts": [
                                {
                                    "artifact_type": "review_result",
                                    "schema_version": "review_result.v1",
                                    "content": {
                                        "verdict": "approve",
                                        "review_origin": "RECOVERY_STAFF",
                                        "reviewer_tier": "STAFF",
                                        "harness": "codex",
                                        "model": "gpt-5.6-sol",
                                        "execution_lease_id": "lease-1",
                                        "task_id": "task-1",
                                        "reviewed_commit_sha": reviewed_commit,
                                        "base_sha": base_sha,
                                        "completion_status": "COMPLETED",
                                        "engineering_doctrine": (
                                            CURRENT_ENGINEERING_DOCTRINE.provenance_payload()
                                        ),
                                        "provenance_stamped_by": "pow_wow_executor",
                                    },
                                }
                            ],
                        }
                    ],
                },
            },
        }
    }

    with pytest.raises(ValueError, match="provenance is incomplete"):
        merge_review.require_staff_review_provenance(approval, settings=object())


def test_staff_review_must_match_current_engineering_doctrine(monkeypatch: Any) -> None:
    base_sha = "a" * 40
    approved_commit = "b" * 40
    bad_doctrine = {
        **CURRENT_ENGINEERING_DOCTRINE.provenance_payload(),
        "sha256": "0" * 64,
    }
    monkeypatch.setattr(
        merge_review,
        "run_coordination_command",
        lambda *_args, **_kwargs: {"intents": []},
    )
    approval = {
        "payload": {
            "base_sha": base_sha,
            "commit_sha": approved_commit,
            "dispatch_result": {
                "schema_version": "dispatch_runner_result.v1",
                "result_origin": "AUTOMATED_RECOVERY",
                "result_state": "COMPLETED",
                "promotion_state": "MERGE_PENDING",
                "run_result": {
                    "status": "COMPLETED",
                    "tasks": [
                        {
                            "task_name": "recovery_staff_review",
                            "artifacts": [
                                {
                                    "artifact_type": "review_result",
                                    "schema_version": "review_result.v1",
                                    "content": {
                                        "verdict": "approve",
                                        "review_origin": "RECOVERY_STAFF",
                                        "reviewer_tier": "STAFF",
                                        "harness": "codex",
                                        "model": "gpt-5.6-sol",
                                        "engineering_doctrine": bad_doctrine,
                                        "execution_lease_id": "lease-1",
                                        "task_id": "task-1",
                                        "reviewed_commit_sha": approved_commit,
                                        "base_sha": base_sha,
                                        "completion_status": "COMPLETED",
                                        "provenance_stamped_by": "pow_wow_executor",
                                    },
                                }
                            ],
                        }
                    ],
                },
            },
        }
    }

    with pytest.raises(ValueError, match="current engineering doctrine"):
        merge_review.require_staff_review_provenance(approval, settings=object())
