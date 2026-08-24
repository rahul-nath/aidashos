# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What a doctrine bump does to reviews already waiting at the merge gate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from local_first_agent_os import merge_review
from local_first_agent_os.coordination.checkpoints import (
    create_execution_checkpoint,
    request_recovery_staff_review,
)
from local_first_agent_os.coordination.dispatch import (
    claim_next_dispatch_intent,
    complete_dispatch_intent,
    submit_dispatch_intent,
)
from local_first_agent_os.coordination.doctrine_staleness import list_doctrine_stale_reviews
from local_first_agent_os.coordination.execution import open_execution_lease
from local_first_agent_os.coordination.projects import create_saga
from local_first_agent_os.coordination.store import set_root
from local_first_agent_os.engineering_doctrine import (
    CURRENT_ENGINEERING_DOCTRINE,
    ENGINEERING_DOCTRINE_V1,
    DoctrineProvenanceStatus,
    EngineeringDoctrine,
)
from local_first_agent_os.review_recovery import (
    StaffReviewProvenanceCode,
    diagnose_staff_review_provenance,
)
from local_first_agent_os.work_units.next_commands import (
    NextCommandStatus,
    next_commands_for,
)

BASE_SHA = "a" * 40
COMMIT_SHA = "b" * 40

# The exact stamp a review conducted under the superseded doctrine carries.
PRIOR_DOCTRINE = EngineeringDoctrine(
    schema_version="engineering_doctrine.v1",
    text=ENGINEERING_DOCTRINE_V1,
)


def _review(doctrine: object) -> dict[str, Any]:
    return {
        "schema_version": "review_result.v1",
        "verdict": "approve",
        "decision_line": "## Staff review",
        "review_origin": "AUTOMATED_STAFF",
        "reviewer_tier": "STAFF",
        "harness": "codex",
        "model": "gpt-5.6-sol",
        "execution_lease_id": "lease-1",
        "task_id": "task-1",
        "reviewed_commit_sha": COMMIT_SHA,
        "base_sha": BASE_SHA,
        "completion_status": "COMPLETED",
        "engineering_doctrine": doctrine,
        "provenance_stamped_by": "pow_wow_executor",
    }


def _checkpoint() -> dict[str, Any]:
    return {
        "schema_version": "worktree_commit_checkpoint.v1",
        "branch_name": "agent/retained",
        "base_head_sha": BASE_SHA,
        "commit_sha": COMMIT_SHA,
        "commit_created": True,
        "changed_from_base": True,
    }


def _merge_pending_result(doctrine: object) -> dict[str, Any]:
    return {
        "schema_version": "dispatch_runner_result.v1",
        "result_origin": "AUTOMATED",
        "result_state": "COMPLETED",
        "promotion_state": "MERGE_PENDING",
        "run_result": {
            "status": "COMPLETED",
            "changed_files": ["feature.py"],
            "tasks": [
                {
                    "task_name": "implementation",
                    "artifacts": [
                        {
                            "artifact_type": "worktree_commit_checkpoint",
                            "schema_version": "worktree_commit_checkpoint.v1",
                            "content": _checkpoint(),
                        }
                    ],
                },
                {
                    "task_name": "staff_review",
                    "artifacts": [
                        {
                            "artifact_type": "review_result",
                            "schema_version": "review_result.v1",
                            "content": _review(doctrine),
                        }
                    ],
                },
            ],
        },
    }


def test_review_stamped_under_an_old_doctrine_hits_the_merge_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A doctrine bump refuses the merge, and says that is what happened."""

    monkeypatch.setattr(
        merge_review,
        "run_coordination_command",
        lambda *_args, **_kwargs: {"intents": []},
    )
    approval = {
        "payload": {
            "base_sha": BASE_SHA,
            "commit_sha": COMMIT_SHA,
            "dispatch_result": _merge_pending_result(PRIOR_DOCTRINE.provenance_payload()),
        }
    }

    with pytest.raises(ValueError) as raised:
        merge_review.require_staff_review_provenance(approval, settings=object())

    message = str(raised.value)
    assert StaffReviewProvenanceCode.DOCTRINE_VERSION_STALE.value in message
    # Both versions, so the operator can see which bump did this.
    assert PRIOR_DOCTRINE.schema_version in message
    assert CURRENT_ENGINEERING_DOCTRINE.schema_version in message
    # The remedy, not just the refusal.
    assert "list_doctrine_stale_reviews" in message
    assert "docs/doctrine_bump_recovery.md" in message


def test_the_same_review_passes_once_it_is_stamped_under_the_current_doctrine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        merge_review,
        "run_coordination_command",
        lambda *_args, **_kwargs: {"intents": []},
    )
    approval = {
        "payload": {
            "base_sha": BASE_SHA,
            "commit_sha": COMMIT_SHA,
            "dispatch_result": _merge_pending_result(
                CURRENT_ENGINEERING_DOCTRINE.provenance_payload()
            ),
        }
    }

    merge_review.require_staff_review_provenance(approval, settings=object())


def test_a_version_bump_and_an_in_place_edit_are_different_diagnoses() -> None:
    """The two doctrine failures have different causes and different remedies."""

    drifted = {
        **CURRENT_ENGINEERING_DOCTRINE.provenance_payload(),
        "sha256": "0" * 64,
    }
    cases = {
        StaffReviewProvenanceCode.DOCTRINE_VERSION_STALE: PRIOR_DOCTRINE.provenance_payload(),
        StaffReviewProvenanceCode.DOCTRINE_TEXT_DRIFT: drifted,
        StaffReviewProvenanceCode.DOCTRINE_UNSTAMPED: None,
    }
    for expected_code, doctrine in cases.items():
        issue = diagnose_staff_review_provenance(_review(doctrine), (), _checkpoint())
        assert issue is not None
        assert issue.code is expected_code

    assert (
        diagnose_staff_review_provenance(
            _review(CURRENT_ENGINEERING_DOCTRINE.provenance_payload()), (), _checkpoint()
        )
        is None
    )


def test_provenance_classification_distinguishes_version_from_text() -> None:
    classify = CURRENT_ENGINEERING_DOCTRINE.classify_provenance
    assert classify(CURRENT_ENGINEERING_DOCTRINE.provenance_payload()).status is (
        DoctrineProvenanceStatus.CURRENT
    )
    assert classify(PRIOR_DOCTRINE.provenance_payload()).status is (
        DoctrineProvenanceStatus.STALE_VERSION
    )
    assert (
        classify({**CURRENT_ENGINEERING_DOCTRINE.provenance_payload(), "sha256": "0" * 64}).status
        is DoctrineProvenanceStatus.TEXT_DRIFT
    )
    assert classify(None).status is DoctrineProvenanceStatus.UNSTAMPED
    assert classify({"schema_version": "engineering_doctrine.v2"}).status is (
        DoctrineProvenanceStatus.UNSTAMPED
    )


def _settle(prompt: str, doctrine: object) -> str:
    submitted = submit_dispatch_intent("senior", prompt, kind="code", target_project_id="target")
    intent_id = str(submitted["intent_id"])
    claim_next_dispatch_intent("test-worker", "senior")
    complete_dispatch_intent(intent_id, "DONE", result=json.dumps(_merge_pending_result(doctrine)))
    return intent_id


def test_scan_reports_exactly_the_merge_pending_reviews_the_gate_would_refuse(
    tmp_path: Path,
) -> None:
    set_root(str(tmp_path))
    stale_intent = _settle("stale implementation", PRIOR_DOCTRINE.provenance_payload())
    current_intent = _settle(
        "current implementation", CURRENT_ENGINEERING_DOCTRINE.provenance_payload()
    )

    scan = list_doctrine_stale_reviews()

    assert scan["ok"] is True
    assert scan["merge_pending"] == 2
    assert scan["current"] == [current_intent]
    assert [row["intent_id"] for row in scan["stale"]] == [stale_intent]
    row = scan["stale"][0]
    assert row["issue_code"] == StaffReviewProvenanceCode.DOCTRINE_VERSION_STALE.value
    assert row["commit_sha"] == COMMIT_SHA
    assert row["base_sha"] == BASE_SHA
    assert row["stamped_doctrine"] == PRIOR_DOCTRINE.provenance_payload()
    # A cleanly completed dispatch retains no paused checkpoint, so the
    # checkpoint-keyed re-review cannot run against it.
    assert row["recovery_review"] is None


def test_scan_finds_the_checkpoint_a_recovery_review_intent_owns(tmp_path: Path) -> None:
    """The recovery-review intent reaches its checkpoint by its own column.

    This is the shape a doctrine bump most often invalidates: the review that
    `request_recovery_staff_review` enqueued against a parked commit, which is
    a different intent row from the one the checkpoint names.
    """

    set_root(str(tmp_path))
    saga = create_saga("recovery review staleness", 1_000, 300)
    submitted = submit_dispatch_intent(
        "senior", "parked implementation", kind="code", target_project_id="target"
    )
    parked_intent = str(submitted["intent_id"])
    claim_next_dispatch_intent("test-worker", "senior")
    lease = open_execution_lease(
        "doctrine-staleness", "test-worker", intent_id=parked_intent, timeout_seconds=60
    )
    checkpoint = create_execution_checkpoint(
        lease["lease"]["lease_id"],
        reason="deadline",
        status="PAUSED",
        worktree_path=str(tmp_path / "worktree"),
        base_head_sha=BASE_SHA,
        saga_id=saga["saga_id"],
    )
    checkpoint_id = checkpoint["checkpoint"]["checkpoint_id"]
    requested = request_recovery_staff_review(
        checkpoint_id,
        target_project_id="target",
        branch="agent/retained",
        base_sha=BASE_SHA,
        commit_sha=COMMIT_SHA,
    )
    review_intent = str(requested["intent"]["intent_id"])
    claim_next_dispatch_intent("review-worker", "staff")
    complete_dispatch_intent(
        review_intent,
        "DONE",
        result=json.dumps(_merge_pending_result(PRIOR_DOCTRINE.provenance_payload())),
    )

    scan = list_doctrine_stale_reviews()

    row = next(item for item in scan["stale"] if item["intent_id"] == review_intent)
    assert row["issue_code"] == StaffReviewProvenanceCode.DOCTRINE_VERSION_STALE.value
    assert row["recovery_review"] == {
        "checkpoint_id": checkpoint_id,
        "target_project_id": "target",
        "branch": "agent/retained",
        "base_sha": BASE_SHA,
        "commit_sha": COMMIT_SHA,
    }
    commands = next_commands_for("list_doctrine_stale_reviews", scan)
    assert commands is not None
    assert any(
        item.status is NextCommandStatus.READY
        and item.command
        == (
            f"agent-ledger request_recovery_staff_review {checkpoint_id} "
            f"--target-project-id target --branch agent/retained "
            f"--base-head-sha {BASE_SHA} --commit-sha {COMMIT_SHA}"
        )
        for item in commands.commands
    )


def test_scan_next_commands_offer_re_review_only_where_it_can_run() -> None:
    payload = {
        "ok": True,
        "current_doctrine": CURRENT_ENGINEERING_DOCTRINE.provenance_payload(),
        "merge_pending": 2,
        "current": [],
        "stale": [
            {
                "intent_id": "intent-no-checkpoint",
                "work_unit_id": "wu-1",
                "milestone_key": "implement",
                "target_project_id": "target",
                "branch": "agent/retained",
                "base_sha": BASE_SHA,
                "commit_sha": COMMIT_SHA,
                "issue_code": StaffReviewProvenanceCode.DOCTRINE_VERSION_STALE.value,
                "issue_message": "stale",
                "approval_id": "approval-1",
                "recovery_review": None,
            },
            {
                "intent_id": "intent-with-checkpoint",
                "work_unit_id": None,
                "milestone_key": None,
                "target_project_id": "target",
                "branch": "agent/retained",
                "base_sha": BASE_SHA,
                "commit_sha": COMMIT_SHA,
                "issue_code": StaffReviewProvenanceCode.DOCTRINE_VERSION_STALE.value,
                "issue_message": "stale",
                "approval_id": None,
                "recovery_review": {
                    "checkpoint_id": "checkpoint-1",
                    "target_project_id": "target",
                    "branch": "agent/retained",
                    "base_sha": BASE_SHA,
                    "commit_sha": COMMIT_SHA,
                },
            },
        ],
    }

    result = next_commands_for("list_doctrine_stale_reviews", payload)

    assert result is not None
    assert "cannot pass the merge gate" in result.headline
    assert "docs/doctrine_bump_recovery.md" in (result.detail or "")
    by_status = {
        status: [item.command for item in result.commands if item.status is status]
        for status in NextCommandStatus
    }
    # The dispatch that kept a usable checkpoint gets a runnable re-review.
    assert any(
        command.startswith("agent-ledger request_recovery_staff_review checkpoint-1")
        for command in by_status[NextCommandStatus.READY]
    )
    # The one that did not gets the refusal it would have hit, not a broken command.
    refused = [
        item
        for item in result.commands
        if item.status is NextCommandStatus.REFUSED
        and item.refusal_code == "recovery_staff_review_requires_paused_checkpoint"
    ]
    assert len(refused) == 1
    assert "intent-no-checkpoint" in (refused[0].reason or "")
    # The owning WorkUnit, and retiring the approval the gate refuses.
    assert "agent-ledger get_work_unit wu-1" in by_status[NextCommandStatus.READY]
    assert any(
        command.startswith("agent-ledger resolve_approval_request approval-1 deny")
        for command in by_status[NextCommandStatus.READY]
    )


def test_scan_next_commands_stay_quiet_when_every_review_is_current() -> None:
    result = next_commands_for(
        "list_doctrine_stale_reviews",
        {
            "ok": True,
            "current_doctrine": CURRENT_ENGINEERING_DOCTRINE.provenance_payload(),
            "merge_pending": 3,
            "current": ["a", "b", "c"],
            "stale": [],
        },
    )

    assert result is not None
    assert result.commands == ()
    assert "passes the merge gate" in result.headline
