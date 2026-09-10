# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from work_unit_support import (
    ACCEPTANCE_DESIGN_DOC,
    compile_acceptance_doc,
    write_test_project_registry,
)

from local_first_agent_os import merge_review
from local_first_agent_os.coordination.approvals import list_approval_requests
from local_first_agent_os.coordination.dispatch import (
    claim_next_dispatch_intent,
    complete_dispatch_intent,
    list_dispatch_intents,
    submit_dispatch_intent,
)
from local_first_agent_os.coordination.projects import create_saga
from local_first_agent_os.coordination.review_recovery import (
    recover_unparsed_staff_review,
)
from local_first_agent_os.coordination.store import set_root
from local_first_agent_os.engineering_doctrine import CURRENT_ENGINEERING_DOCTRINE
from local_first_agent_os.review_recovery import (
    ReviewRecoveryRefused,
    recover_unparsed_dispatch_review,
    staff_review_approves_checkpoint,
)
from local_first_agent_os.settings import Settings, get_settings
from local_first_agent_os.work_units import dispatch_adoption
from local_first_agent_os.work_units import repository as work_unit_repo
from local_first_agent_os.work_units.events import MilestoneTransition, WorkUnitTransition
from local_first_agent_os.work_units.lifecycle import (
    FailureClass,
    LifecyclePhase,
    MilestoneExecutionStatus,
    WorkUnitStatus,
)


def _failed_result(
    saga_id: str,
    *,
    intent_id: str = "intent-1",
    verdict: str = "unclassified",
    base_sha: str = "a" * 40,
    commit_sha: str = "b" * 40,
) -> dict[str, Any]:
    checkpoint = {
        "schema_version": "worktree_commit_checkpoint.v1",
        "branch_name": "agent/retained",
        "base_head_sha": base_sha,
        "commit_sha": commit_sha,
        "commit_created": True,
        "changed_from_base": True,
        "checkpointed_files": ["feature.py"],
    }
    review = {
        "schema_version": "review_result.v1",
        "verdict": verdict,
        "decision_line": "## Staff review",
        "review_text": "## Staff review\n\n**Verdict: APPROVE.** Exact commit is correct.",
        "finding_severity": "UNKNOWN",
        "review_origin": "AUTOMATED_STAFF",
        "reviewer_tier": "STAFF",
        "harness": "claude",
        "model": "claude-opus-5",
        "execution_lease_id": "lease-1",
        "task_id": "task-1",
        "attempt_number": 1,
        "reviewed_commit_sha": commit_sha,
        "base_sha": base_sha,
        "completion_status": "COMPLETED",
        "engineering_doctrine": CURRENT_ENGINEERING_DOCTRINE.provenance_payload(),
        "provenance_stamped_by": "pow_wow_executor",
    }
    return {
        "schema_version": "dispatch_runner_result.v1",
        "intent_id": intent_id,
        "result_origin": "AUTOMATED",
        "result_state": "FAILED",
        "promotion_state": "RESULT_RECORDED",
        "saga_id": saga_id,
        "pow_wow_id": "pow-wow-1",
        "target_project_id": "target",
        "run_result": {
            "status": "FAILED",
            "external_agents_started": True,
            "changed_files": ["feature.py"],
            "tasks": [
                {
                    "task_name": "implementation",
                    "artifacts": [
                        {
                            "artifact_type": "worktree_commit_checkpoint",
                            "schema_version": "worktree_commit_checkpoint.v1",
                            "content": checkpoint,
                        }
                    ],
                },
                {
                    "task_name": "staff_review",
                    "artifacts": [
                        {
                            "artifact_type": "review_result",
                            "schema_version": "review_result.v1",
                            "content": review,
                        }
                    ],
                },
            ],
        },
    }


def test_unparsed_staff_review_recovery_opens_one_normal_merge_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_root(str(tmp_path))
    saga = create_saga("recover parser miss", 1_000, 300)
    submitted = submit_dispatch_intent(
        "senior",
        "implement feature",
        kind="code",
        target_project_id="target",
    )
    intent_id = submitted["intent_id"]
    claim_next_dispatch_intent("test-worker", "senior")
    result = _failed_result(saga["saga_id"], intent_id=intent_id)
    completed = complete_dispatch_intent(
        intent_id,
        "FAILED",
        result=json.dumps(result),
        error="old host parser classified the review as unclassified",
    )
    assert completed["ok"], completed

    first = recover_unparsed_staff_review(intent_id)
    replay = recover_unparsed_staff_review(intent_id)

    assert first["created"] is True
    assert replay["created"] is False
    assert first["approval_id"] == replay["approval_id"]
    approval = next(
        request
        for request in list_approval_requests(status_filter="PENDING")["requests"]
        if request["approval_id"] == first["approval_id"]
    )
    recovered = approval["payload"]["dispatch_result"]
    reviews = [
        artifact["content"]
        for task in recovered["run_result"]["tasks"]
        for artifact in task.get("artifacts", [])
        if artifact.get("artifact_type") == "review_result"
    ]
    assert [review["verdict"] for review in reviews] == ["unclassified", "approve"]
    assert recovered["result_origin"] == "AUTOMATED_RECOVERY"
    assert recovered["promotion_state"] == "MERGE_PENDING"
    assert approval["payload"]["commit_sha"] == "b" * 40
    monkeypatch.setattr(
        merge_review,
        "run_coordination_command",
        lambda *_args, **_kwargs: {"intents": list_dispatch_intents()["intents"]},
    )
    merge_review.require_staff_review_provenance(approval, settings=object())


def test_recovery_refuses_anything_except_an_unclassified_approve() -> None:
    result = _failed_result("saga-1", verdict="request_changes")
    with pytest.raises(ReviewRecoveryRefused, match="only an unclassified"):
        recover_unparsed_dispatch_review("intent-1", result)

    result = _failed_result("saga-1")
    review = result["run_result"]["tasks"][-1]["artifacts"][-1]["content"]
    review["review_text"] = "## Staff review\n\nVerdict: BLOCK. Missing invariant."
    with pytest.raises(ReviewRecoveryRefused, match="does not classify"):
        recover_unparsed_dispatch_review("intent-1", result)


def test_recovered_review_is_bound_to_the_immutable_source_digest() -> None:
    recovered = recover_unparsed_dispatch_review("intent-1", _failed_result("saga-1"))
    run_result = recovered.dispatch_result["run_result"]
    reviews = [
        artifact["content"]
        for task in run_result["tasks"]
        for artifact in task.get("artifacts", [])
        if artifact.get("artifact_type") == "review_result"
    ]
    assert staff_review_approves_checkpoint(reviews[-1], reviews[:-1], recovered.checkpoint)

    tampered = deepcopy(reviews[-1])
    tampered["recovery"]["source_review_sha256"] = "0" * 64
    assert not staff_review_approves_checkpoint(tampered, reviews[:-1], recovered.checkpoint)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.parametrize("recovery_origin", ["parser", "staff", "staff_retry"])
def test_approved_recovery_advances_only_after_exact_commit_is_integrated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery_origin: str
) -> None:
    set_root(str(tmp_path / "coordination"))
    target = tmp_path / "target"
    target.mkdir()
    _git(target, "init", "-q", "-b", "main")
    _git(target, "config", "user.email", "recovery@example.com")
    _git(target, "config", "user.name", "Recovery Test")
    (target / "feature.py").write_text("READY = False\n", encoding="utf-8")
    _git(target, "add", "feature.py")
    _git(target, "commit", "-qm", "base")
    base_sha = _git(target, "rev-parse", "HEAD")
    _git(target, "checkout", "-qb", "agent/retained")
    (target / "feature.py").write_text("READY = True\n", encoding="utf-8")
    _git(target, "commit", "-am", "reviewed implementation")
    commit_sha = _git(target, "rev-parse", "HEAD")
    _git(target, "checkout", "-q", "main")

    config_dir = tmp_path / "configs"
    registry = write_test_project_registry(config_dir, "target", target)
    registry_before = registry.read_bytes()
    monkeypatch.setenv("LOCAL_AGENT_CONFIG_DIR", str(config_dir))
    get_settings.cache_clear()
    compiled = compile_acceptance_doc(
        design_doc_id="dispatch_adoption",
        content=ACCEPTANCE_DESIGN_DOC.replace(
            "Target project: local_first_agent_os", "Target project: target"
        ),
    )
    assert registry.read_bytes() == registry_before
    assert compiled.compiled_plan_revision_id is not None
    started = work_unit_repo.start_work_unit(compiled.compiled_plan_revision_id)
    work_unit_id = started.work_unit.work_unit_id
    milestone_key = next(
        item.stable_key
        for item in work_unit_repo.list_milestone_executions(work_unit_id)
        if item.phase is LifecyclePhase.IMPLEMENT
    )
    work_unit_repo.record_fact(
        work_unit_id,
        WorkUnitTransition(
            status=WorkUnitStatus.RUNNING,
            current_phase=LifecyclePhase.IMPLEMENT,
        ),
    )
    for status in (
        MilestoneExecutionStatus.READY,
        MilestoneExecutionStatus.RUNNING,
        MilestoneExecutionStatus.BLOCKED,
    ):
        blocked = status is MilestoneExecutionStatus.BLOCKED
        work_unit_repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase.IMPLEMENT,
                milestone_key=milestone_key,
                status=status,
                attempt=1,
                failure_class=FailureClass.CORRECTABLE if blocked else None,
                failure_code="old_review_parser" if blocked else None,
            ),
        )
    work_unit_repo.record_fact(
        work_unit_id,
        WorkUnitTransition(
            status=WorkUnitStatus.BLOCKED,
            current_phase=LifecyclePhase.IMPLEMENT,
        ),
    )

    saga = create_saga("adopt recovered dispatch", 1_000, 300)
    submitted = submit_dispatch_intent(
        "senior",
        "implement feature",
        kind="code",
        target_project_id="target",
        source=f"work_unit:{work_unit_id}:milestone_execution:{milestone_key}",
    )
    intent_id = submitted["intent_id"]
    claim_next_dispatch_intent("test-worker", "senior")
    completed = complete_dispatch_intent(
        intent_id,
        "FAILED",
        result=json.dumps(
            _failed_result(
                saga["saga_id"], intent_id=intent_id, base_sha=base_sha, commit_sha=commit_sha
            )
        ),
        error="old host parser classified the review as unclassified",
    )
    assert completed["ok"], completed
    recovery = recover_unparsed_staff_review(intent_id)
    with dispatch_adoption.connect() as connection:
        connection.execute(
            "UPDATE approval_requests SET status = 'APPROVED' WHERE approval_id = ?",
            (recovery["approval_id"],),
        )
        connection.commit()
    if recovery_origin in {"staff", "staff_retry"}:
        from local_first_agent_os.coordination.checkpoints import (
            create_execution_checkpoint,
            request_recovery_staff_review,
        )
        from local_first_agent_os.coordination.execution import open_execution_lease
        from local_first_agent_os.coordination.store import decode_json_object

        with dispatch_adoption.connect() as connection:
            connection.execute(
                "UPDATE dispatch_intents SET permitted_capabilities=? WHERE intent_id=?",
                (json.dumps(["read_repository", "invoke_model"]), intent_id),
            )
            connection.commit()
        lease = open_execution_lease(
            "retained-staff-fixture", "test-worker", intent_id=intent_id, timeout_seconds=60
        )["lease"]
        checkpoint = create_execution_checkpoint(
            lease["lease_id"],
            reason="supervisor_error",
            status="PAUSED",
            saga_id=saga["saga_id"],
            base_head_sha=base_sha,
        )["checkpoint"]
        fresh = request_recovery_staff_review(
            checkpoint["checkpoint_id"],
            target_project_id="target",
            branch="agent/retained",
            base_sha=base_sha,
            commit_sha=commit_sha,
        )["intent"]
        if recovery_origin == "staff_retry":
            claim_next_dispatch_intent("unavailable-review", "staff")
            unavailable = complete_dispatch_intent(
                fresh["intent_id"], "FAILED", error="reviewer unavailable before producing a report"
            )
            assert unavailable["ok"], unavailable
            fresh = request_recovery_staff_review(
                checkpoint["checkpoint_id"],
                target_project_id="target",
                branch="agent/retained",
                base_sha=base_sha,
                commit_sha=commit_sha,
                retry_of=fresh["intent_id"],
            )["intent"]
        run = _failed_result(
            saga["saga_id"], verdict="approve", base_sha=base_sha, commit_sha=commit_sha
        )["run_result"]
        run["status"] = "COMPLETED"
        run["tasks"][-1]["artifacts"][0]["content"]["review_origin"] = "RECOVERY_STAFF"
        recorded = {
            "schema_version": "dispatch_runner_result.v1",
            "intent_id": fresh["intent_id"],
            "target_project_id": "target",
            "result_origin": "AUTOMATED_RECOVERY",
            "result_state": "COMPLETED",
            "promotion_state": "MERGE_PENDING",
            "run_result": run,
        }
        claim_next_dispatch_intent("review-fixture", "staff")
        completed = complete_dispatch_intent(
            fresh["intent_id"], "DONE", result=json.dumps(recorded)
        )
        assert completed["ok"], completed
        with dispatch_adoption.connect() as connection:
            approval = connection.execute(
                "SELECT payload_json FROM approval_requests WHERE approval_id=?",
                (recovery["approval_id"],),
            ).fetchone()
            payload = decode_json_object(approval["payload_json"])
            payload.pop("review_recovery")
            payload.update(
                intent_id=fresh["intent_id"],
                checkpoint_id=checkpoint["checkpoint_id"],
                dispatch_result=recorded,
            )
            connection.execute(
                "UPDATE approval_requests SET payload_json=? WHERE approval_id=?",
                (json.dumps(payload), recovery["approval_id"]),
            )
            connection.commit()
        assert isinstance(
            dispatch_adoption._approved_recovery(intent_id),
            dispatch_adoption._StaffRecoveryApproval,
        )
        with dispatch_adoption.connect() as connection:
            connection.execute(
                "UPDATE dispatch_intents SET parent_intent_id=NULL WHERE intent_id=?",
                (fresh["intent_id"],),
            )
            connection.commit()
        with pytest.raises(
            dispatch_adoption.DispatchAdoptionRefused, match="expected one approved"
        ):
            dispatch_adoption._approved_recovery(intent_id)
        with dispatch_adoption.connect() as connection:
            connection.execute(
                "UPDATE dispatch_intents SET parent_intent_id=? WHERE intent_id=?",
                (intent_id, fresh["intent_id"]),
            )
            connection.commit()
    settings = Settings(coordination_root=tmp_path / "coordination")

    with pytest.raises(
        dispatch_adoption.DispatchAdoptionRefused,
        match="does not contain",
    ):
        dispatch_adoption.adopt_recovered_dispatch(intent_id, settings=settings)

    _git(target, "merge", "--ff-only", commit_sha)
    adopted = dispatch_adoption.adopt_recovered_dispatch(intent_id, settings=settings)
    replay = dispatch_adoption.adopt_recovered_dispatch(intent_id, settings=settings)

    milestone = next(
        item
        for item in work_unit_repo.list_milestone_executions(work_unit_id)
        if item.stable_key == milestone_key
    )
    artifacts = work_unit_repo.list_work_unit_artifacts(work_unit_id)
    source_patch = next(item for item in artifacts if item.artifact_type.value == "source_patch")
    assert milestone.status is MilestoneExecutionStatus.SUCCEEDED
    assert milestone.attempt == 2
    assert adopted.applied is True
    assert replay.applied is False
    assert source_patch.metadata["source_dispatch_intent_id"] == intent_id
    assert source_patch.metadata["integrated_commit_sha"] == commit_sha
