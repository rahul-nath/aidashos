# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from local_first_agent_os.coordination.checkpoints import (
    append_execution_event,
    create_execution_checkpoint,
    decide_execution_checkpoint,
    list_execution_events,
    request_recovery_staff_review,
)
from local_first_agent_os.coordination.dispatch import (
    claim_next_dispatch_intent,
    complete_dispatch_intent,
    list_dispatch_intents,
    submit_dispatch_intent,
)
from local_first_agent_os.coordination.execution import open_execution_lease
from local_first_agent_os.coordination.projects import create_saga
from local_first_agent_os.coordination.store import set_root


def _claimed_lease(tmp_path: Path) -> tuple[str, str]:
    set_root(str(tmp_path))
    submitted = submit_dispatch_intent(
        "senior", "Implement bounded change", kind="code", target_project_id="target"
    )
    intent_id = submitted["intent_id"]
    claimed = claim_next_dispatch_intent("test-worker", "senior")
    assert claimed["intent"]["intent_id"] == intent_id
    opened = open_execution_lease(
        "checkpoint-test", "test-worker", intent_id=intent_id, timeout_seconds=60
    )
    return intent_id, opened["lease"]["lease_id"]


def test_events_are_append_only_and_idempotent(tmp_path: Path, monkeypatch) -> None:
    _, lease_id = _claimed_lease(tmp_path)
    payload = {"type": "item.completed", "text": "visible output"}
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    first = append_execution_event(lease_id, 1, 1.0, "stdout", "item.completed", payload, digest)
    replay = append_execution_event(lease_id, 1, 1.0, "stdout", "item.completed", payload, digest)
    conflict = append_execution_event(
        lease_id,
        1,
        1.0,
        "stdout",
        "item.completed",
        {"text": "different"},
        hashlib.sha256(b'{"text":"different"}').hexdigest(),
    )

    assert first["created"] is True
    assert replay["created"] is False
    assert conflict["error"] == "sequence_conflict"
    assert [event["sequence"] for event in list_execution_events(lease_id)["events"]] == [1]


def test_checkpoint_review_and_continuation_are_durable(tmp_path: Path, monkeypatch) -> None:
    intent_id, lease_id = _claimed_lease(tmp_path)

    created = create_execution_checkpoint(
        lease_id,
        reason="deadline",
        status="PENDING_JUNIOR",
        worktree_path=str(tmp_path / "worktree"),
        task_contract="Implement only the accepted milestone scope.",
        submit_review=True,
    )
    replay = create_execution_checkpoint(
        lease_id,
        reason="deadline",
        status="PENDING_JUNIOR",
        submit_review=True,
    )
    checkpoint = created["checkpoint"]

    intents = list_dispatch_intents()["intents"]
    original = next(item for item in intents if item["intent_id"] == intent_id)
    reviews = [item for item in intents if item.get("checkpoint_id") == checkpoint["checkpoint_id"]]
    assert created["created"] is True
    assert replay["created"] is False
    assert original["status"] == "CHECKPOINT_REVIEW"
    assert len(reviews) == 1
    assert reviews[0]["tier"] == "junior"
    late_completion = complete_dispatch_intent(intent_id, "FAILED", error="runner timed out")
    assert late_completion["completion_skipped"] is True
    assert late_completion["status"] == "CHECKPOINT_REVIEW"

    decision = {
        "schema_version": "checkpoint_review.v1",
        "decision": "resume_one",
        "completed_work": ["partial implementation"],
        "remaining_work": ["verification"],
        "continuations": [
            {
                "title": "Finish bounded verification",
                "scope": "Verify and repair only the preserved milestone change.",
                "acceptance_criteria": ["tests pass"],
                "verification_commands": ["uv run pytest -q"],
            }
        ],
        "risks": [],
        "rationale": "One continuation preserves task cohesion.",
    }
    decided = decide_execution_checkpoint(checkpoint["checkpoint_id"], decision)
    decided_replay = decide_execution_checkpoint(checkpoint["checkpoint_id"], decision)

    assert decided["checkpoint"]["status"] == "DECIDED"
    assert len(decided["continuation_intent_ids"]) == 1
    assert decided_replay["created"] is False
    intents = list_dispatch_intents()["intents"]
    original_status = next(item for item in intents if item["intent_id"] == intent_id)["status"]
    assert original_status == "SUPERSEDED"


def test_invalid_junior_decision_pauses_for_operator(tmp_path: Path, monkeypatch) -> None:
    intent_id, lease_id = _claimed_lease(tmp_path)
    checkpoint = create_execution_checkpoint(
        lease_id,
        reason="deadline",
        status="PENDING_JUNIOR",
        submit_review=True,
    )["checkpoint"]

    result = decide_execution_checkpoint(
        checkpoint["checkpoint_id"], {"schema_version": "wrong", "decision": "resume_one"}
    )

    assert result["error"] == "invalid_checkpoint_review"
    assert result["checkpoint"]["status"] == "PAUSED"
    original = next(
        item for item in list_dispatch_intents()["intents"] if item["intent_id"] == intent_id
    )
    assert original["status"] == "PAUSED"


def test_recovery_staff_review_request_is_typed_idempotent_and_staff_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _, lease_id = _claimed_lease(tmp_path)
    saga = create_saga("Recover retained implementation", 1000, 300)
    base_sha = "a" * 40
    commit_sha = "b" * 40
    checkpoint = create_execution_checkpoint(
        lease_id,
        reason="supervisor_error",
        status="PAUSED",
        saga_id=saga["saga_id"],
        base_head_sha=base_sha,
    )["checkpoint"]

    first = request_recovery_staff_review(
        checkpoint["checkpoint_id"],
        target_project_id="target",
        branch="agent/retained",
        base_sha=base_sha,
        commit_sha=commit_sha,
        milestone_id="milestone-3",
    )
    replay = request_recovery_staff_review(
        checkpoint["checkpoint_id"],
        target_project_id="target",
        branch="agent/retained",
        base_sha=base_sha,
        commit_sha=commit_sha,
        milestone_id="milestone-3",
    )

    assert first["created"] is True
    assert replay["created"] is False
    assert first["intent"]["intent_id"] == replay["intent"]["intent_id"]
    assert first["intent"]["tier"] == "staff"
    assert first["intent"]["kind"] == "code"
    request = json.loads(first["intent"]["prompt"])
    assert request == {
        "base_sha": base_sha,
        "branch": "agent/retained",
        "checkpoint_id": checkpoint["checkpoint_id"],
        "commit_sha": commit_sha,
        "milestone_id": "milestone-3",
        "permission_envelope": "read-only review; revisions only after BLOCK",
        "review_origin": "RECOVERY_STAFF",
        "saga_id": saga["saga_id"],
        "schema_version": "recovery_staff_review_request.v1",
        "target_project_id": "target",
    }
    assert first["next_step"] == "pi /dispatch"

    conflict = request_recovery_staff_review(
        checkpoint["checkpoint_id"],
        target_project_id="target",
        branch="agent/different",
        base_sha=base_sha,
        commit_sha="c" * 40,
        milestone_id="milestone-3",
    )
    assert conflict["error"] == "recovery_staff_review_conflict"


def test_recovery_staff_review_rejects_checkpoint_base_drift(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _, lease_id = _claimed_lease(tmp_path)
    saga = create_saga("Recover retained implementation", 1000, 300)
    checkpoint = create_execution_checkpoint(
        lease_id,
        reason="supervisor_error",
        status="PAUSED",
        saga_id=saga["saga_id"],
        base_head_sha="a" * 40,
    )["checkpoint"]

    result = request_recovery_staff_review(
        checkpoint["checkpoint_id"],
        target_project_id="target",
        branch="agent/retained",
        base_sha="c" * 40,
        commit_sha="b" * 40,
    )

    assert result["error"] == "checkpoint_base_mismatch"
