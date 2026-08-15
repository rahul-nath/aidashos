# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from local_first_agent_os.coordination.store import tx
from local_first_agent_os.pow_wow import persist_pow_wow_run_result, run_coordination_command
from local_first_agent_os.pow_wow.revision import build_bounded_revision_context_from_review
from local_first_agent_os.pow_wow.types import (
    PowWowArtifact,
    PowWowRunResult,
    PowWowTaskResult,
)


def _blocking_review(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "review_result.v1",
        "verdict": "request_changes",
        "finding_severity": "BLOCKING",
        "review_origin": "RECOVERY_STAFF",
        "reviewer_tier": "STAFF",
        "harness": "codex",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "execution_lease_id": "lease-review-1",
        "task_id": "task-review-1",
        "reviewed_commit_sha": "b" * 40,
        "base_sha": "a" * 40,
        "attempt_number": 1,
        "completion_status": "COMPLETED",
        "review_text": (
            "BLOCK\nNovel finding with unrestricted prose, file anchors, and a remedy.\n"
            "Do not collapse this text into a closed findings taxonomy."
        ),
        "provenance_stamped_by": "pow_wow_executor",
    }
    payload.update(overrides)
    return payload


def _context_payload(review_artifact_id: str | None = None) -> dict[str, object]:
    return build_bounded_revision_context_from_review(
        review_result=_blocking_review(),
        review_task_name="recovery_staff_review",
        review_artifact_id=review_artifact_id,
        retained_branch="agent/retained-implementation",
        retained_worktree_path="/tmp/recovery-review",
        original_task_name="recovery_revision_anchor",
        original_task_contract="Implement only the accepted milestone contract.",
        permission_envelope="read-only review; revisions only after BLOCK",
        verification_commands=("uv run pytest tests/test_feature.py", "uv run ruff check"),
    ).to_payload()


def test_bounded_revision_context_serializes_exact_provenance_and_review_digest() -> None:
    payload = _context_payload("artifact-review-result")
    serialized = json.dumps(payload, sort_keys=True)
    restored = json.loads(serialized)
    review_text = str(_blocking_review()["review_text"])

    assert restored["schema_version"] == "bounded_revision_context.v1"
    assert restored["target"] == {
        "base_commit_sha": "a" * 40,
        "blocked_commit_sha": "b" * 40,
        "retained_branch": "agent/retained-implementation",
        "retained_worktree_path": "/tmp/recovery-review",
    }
    assert restored["reviewer"]["review_origin"] == "RECOVERY_STAFF"
    assert restored["reviewer"]["execution_lease_id"] == "lease-review-1"
    assert restored["reviewer_output"]["artifact_id"] == "artifact-review-result"
    assert (
        restored["reviewer_output"]["review_text_sha256"]
        == hashlib.sha256(review_text.encode("utf-8")).hexdigest()
    )
    assert restored["revision_scope"]["permission_envelope"] == (
        "read-only review; revisions only after BLOCK"
    )
    assert restored["verification"]["commands"] == [
        "uv run pytest tests/test_feature.py",
        "uv run ruff check",
    ]
    assert "findings" not in restored


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"reviewer_tier": "SENIOR"}, "staff reviewer"),
        ({"completion_status": "FAILED"}, "completed review"),
        ({"finding_severity": "NON_BLOCKING"}, "blocking review"),
        ({"review_text": ""}, "complete reviewer output"),
        ({"reviewed_commit_sha": "short"}, "blocked_commit_sha"),
        ({"reviewed_commit_sha": "a" * 40}, "must differ"),
    ],
)
def test_bounded_revision_context_rejects_incomplete_or_invalid_evidence(
    override: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        build_bounded_revision_context_from_review(
            review_result=_blocking_review(**override),
            review_task_name="staff_review",
            review_artifact_id="artifact-review-result",
            retained_branch="agent/retained",
            retained_worktree_path="/tmp/review",
            original_task_name="implement",
            original_task_contract="Implement the accepted task.",
            permission_envelope="No widening.",
            verification_commands=("uv run pytest",),
        )


def test_end_of_run_persistence_resolves_pending_review_reference(
    tmp_path: Path,
) -> None:
    root = tmp_path / "coordination"
    saga = run_coordination_command(["create_saga", "Persist revision envelope"], root=root)
    pow_wow = run_coordination_command(
        [
            "create_pow_wow",
            saga["saga_id"],
            "REVIEW",
            "Review retained work",
        ],
        root=root,
    )
    task = run_coordination_command(
        [
            "claim_task",
            pow_wow["pow_wow_id"],
            "recovery_staff_review",
            "Review exact retained commit",
        ],
        root=root,
    )
    review_artifact = PowWowArtifact(
        artifact_type="review_result",
        schema_version="review_result.v1",
        task_name="recovery_staff_review",
        content=_blocking_review(),
    )
    bounded_artifact = PowWowArtifact(
        artifact_type="bounded_revision_context",
        schema_version="bounded_revision_context.v1",
        task_name="recovery_staff_review",
        content=_context_payload(),
    )
    result = PowWowRunResult(
        executor="test",
        mode="test",
        pow_wow_id=pow_wow["pow_wow_id"],
        target_project_id="target",
        target_project_path="/tmp/target",
        status="FAILED",
        output_summary="blocked for operator",
        tasks=(
            PowWowTaskResult(
                task_name="recovery_staff_review",
                role="reviewer",
                status="completed",
                summary="staff blocked",
                artifacts=(review_artifact, bounded_artifact),
            ),
        ),
    )

    persist_pow_wow_run_result(
        pow_wow["pow_wow_id"],
        {"recovery_staff_review": task["task_id"]},
        result,
        root=root,
    )

    with tx() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT artifact_id, artifact_type, content FROM task_artifacts "
                "WHERE artifact_type IN ('review_result', 'bounded_revision_context') "
                "ORDER BY created_at"
            ).fetchall()
        ]
    assert [row["artifact_type"] for row in rows] == ["review_result", "bounded_revision_context"]
    review_id = rows[0]["artifact_id"]
    stored_review = json.loads(rows[0]["content"])
    stored_bounded = json.loads(rows[1]["content"])
    assert stored_review["content"]["review_text"] == _blocking_review()["review_text"]
    assert stored_bounded["content"]["reviewer_output"]["state"] == "DURABLE_ARTIFACT"
    assert stored_bounded["content"]["reviewer_output"]["artifact_id"] == review_id
