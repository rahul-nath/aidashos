# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from typing import Any

import pytest

from local_first_agent_os.coordination.outcomes import (
    DispatchPromotionState,
    DispatchResultOrigin,
    DispatchResultState,
    next_dispatch_promotion_states,
    require_dispatch_promotion_transition,
)
from local_first_agent_os.dispatch_payloads import ManualRunObservation
from local_first_agent_os.dispatch_results import (
    DispatchEvidenceSubject,
    normalize_dispatch_runner_result,
)


def test_automated_dispatch_result_uses_finite_enums() -> None:
    result = normalize_dispatch_runner_result(
        intent_result=json.dumps(
            {
                "schema_version": "dispatch_runner_result.v1",
                "result_origin": "AUTOMATED",
                "result_state": "COMPLETED",
                "promotion_state": "MERGE_PENDING",
                "run_result": {
                    "status": "COMPLETED",
                    "changed_files": ["feature.py"],
                },
            }
        ),
        approval_payload={},
    )

    assert result.origin is DispatchResultOrigin.AUTOMATED
    assert result.state is DispatchResultState.COMPLETED
    assert result.promotion_state is DispatchPromotionState.MERGE_PENDING


def test_manual_recovery_fields_normalize_to_dispatch_runner_result() -> None:
    result = normalize_dispatch_runner_result(
        intent_result=None,
        approval_payload={
            "manual_recovery": True,
            "purpose": "Reviewed recovery",
            "target_project_id": "target",
            "branch": "agent/recovery",
            "base_sha": "a" * 40,
            "commit_sha": "b" * 40,
            "changed_files": ["feature.py"],
            "verification": ["pytest -q -> passed"],
            "staff_review": {
                "verdict": "APPROVE",
                "resolution": "Blocking finding repaired.",
                "risks": ["Manual preview remains."],
            },
        },
    )

    assert result.origin is DispatchResultOrigin.MANUAL_RECOVERY
    assert result.state is DispatchResultState.REVIEWED
    assert result.promotion_state is DispatchPromotionState.REVIEWED
    assert result.run_result["verification_commands"] == ["pytest -q -> passed"]
    assert isinstance(result.observation, ManualRunObservation)
    assert len(result.observation.tasks) == 1
    task = result.observation.tasks[0]
    assert task.status == "APPROVE"
    assert task.task_name == "manual_recovery_operator_evidence"
    assert len(task.artifacts) == 1
    review = task.artifacts[0].content
    assert review["review_origin"] == "OPERATOR_EVIDENCE"
    assert review["reviewer_tier"] == "OPERATOR"


def test_manual_recovery_merge_fails_closed_without_approval_verdict() -> None:
    with pytest.raises(ValueError, match="INVALID_ENVELOPE"):
        normalize_dispatch_runner_result(
            intent_result=None,
            approval_payload={
                "manual_recovery": True,
                "branch": "agent/recovery",
                "base_sha": "a" * 40,
                "commit_sha": "b" * 40,
                "staff_review": {"verdict": "BLOCK"},
            },
        )


def test_dispatch_promotion_state_machine_forbids_skipping_merge() -> None:
    require_dispatch_promotion_transition(
        DispatchPromotionState.MERGE_PENDING,
        DispatchPromotionState.MERGE_APPROVED,
    )
    assert next_dispatch_promotion_states(DispatchPromotionState.MERGE_APPROVED) == {
        DispatchPromotionState.MERGED
    }
    with pytest.raises(ValueError, match="invalid dispatch promotion transition"):
        require_dispatch_promotion_transition(
            DispatchPromotionState.MERGE_APPROVED,
            DispatchPromotionState.MILESTONE_COMPLETED,
        )


def test_dispatch_result_envelope_rejects_state_promotion_skip() -> None:
    with pytest.raises(ValueError, match="INCONSISTENT_OUTCOME"):
        normalize_dispatch_runner_result(
            intent_result=None,
            approval_payload={
                "dispatch_result": {
                    "schema_version": "dispatch_runner_result.v1",
                    "result_origin": "AUTOMATED_RECOVERY",
                    "result_state": "COMPLETED",
                    "promotion_state": "MERGE_APPROVED",
                    "run_result": {"status": "COMPLETED"},
                }
            },
        )


@pytest.mark.parametrize("nested", ["FAILED", "VERIFICATION_FAILED", "BLOCKED", "unknown"])
@pytest.mark.parametrize("embedded", [False, True])
def test_completed_envelope_cannot_override_unsuccessful_nested_run(
    nested: str, embedded: bool
) -> None:
    payload = {
        "schema_version": "dispatch_runner_result.v1",
        "result_origin": "AUTOMATED",
        "result_state": "COMPLETED",
        "promotion_state": "RESULT_RECORDED",
        "run_result": {"status": nested},
    }
    with pytest.raises(ValueError):
        normalize_dispatch_runner_result(
            intent_result=None if embedded else json.dumps(payload),
            approval_payload={"dispatch_result": payload} if embedded else {},
        )


@pytest.mark.parametrize(
    "changed",
    [
        {"intent_id": "other"},
        {"target_project_id": "other"},
        {"run_result": {"status": "COMPLETED", "target_project_id": "other"}},
    ],
)
def test_result_cannot_choose_a_different_ledger_evidence_subject(changed: dict[str, Any]) -> None:
    payload = {
        "schema_version": "dispatch_runner_result.v1",
        "intent_id": "intent",
        "target_project_id": "project",
        "run_result": {"status": "COMPLETED", "target_project_id": "project"},
        **changed,
    }
    with pytest.raises(ValueError, match="SUBJECT_MISMATCH"):
        normalize_dispatch_runner_result(
            intent_result=json.dumps(payload),
            approval_payload={},
            expected_subject=DispatchEvidenceSubject("intent", "project"),
        )


def test_legacy_result_is_readable_but_cannot_supply_a_new_completion_subject() -> None:
    legacy = json.dumps(
        {"schema_version": "dispatch_runner_result.v1", "run_result": {"status": "COMPLETED"}}
    )
    assert (
        normalize_dispatch_runner_result(intent_result=legacy, approval_payload={}).state
        is DispatchResultState.COMPLETED
    )
    with pytest.raises(ValueError, match="SUBJECT_MISMATCH"):
        normalize_dispatch_runner_result(
            intent_result=legacy,
            approval_payload={},
            expected_subject=DispatchEvidenceSubject("intent", "project"),
        )


@pytest.mark.parametrize(
    "corruption",
    [
        {"changed_files": [4]},
        {"verification_commands": "pytest"},
        {"verification_output": [False]},
        {"tasks": [{"status": "invented"}]},
        {"tasks": [{"summary": {"not": "text"}}]},
        {"artifacts": [{"artifact_type": "test_result", "content": "invented"}]},
        {"external_agents_started": "true"},
        {"changed_files": None},
        {"unregistered_field": "ignored before"},
    ],
)
def test_report_ingress_rejects_untyped_or_unregistered_fields(
    corruption: dict[str, Any],
) -> None:
    raw = json.dumps(
        {
            "schema_version": "dispatch_runner_result.v1",
            "run_result": {"status": "COMPLETED", **corruption},
        }
    )
    with pytest.raises(ValueError):
        normalize_dispatch_runner_result(intent_result=raw, approval_payload={})


def test_envelope_cannot_add_a_future_authority_field_silently() -> None:
    raw = json.dumps(
        {
            "schema_version": "dispatch_runner_result.v1",
            "run_result": {"status": "COMPLETED"},
            "skip_approval": True,
        }
    )
    with pytest.raises(ValueError):
        normalize_dispatch_runner_result(intent_result=raw, approval_payload={})


def test_valid_report_is_a_typed_observation_and_projection_cannot_mutate_it() -> None:
    from local_first_agent_os.dispatch_payloads import AutomatedRunObservation

    result = normalize_dispatch_runner_result(
        intent_result=json.dumps(
            {
                "schema_version": "dispatch_runner_result.v1",
                "run_result": {"status": "COMPLETED", "verification_output": ["observed"]},
            }
        ),
        approval_payload={},
    )
    assert isinstance(result.observation, AutomatedRunObservation)
    projection = dict(result.run_result)
    projection["verification_output"] = ["replaced"]
    assert result.observation.verification_output == ("observed",)


def test_raw_mapping_cannot_bypass_the_normalized_result_constructor() -> None:
    from local_first_agent_os.dispatch_results import DispatchRunnerResult

    with pytest.raises(TypeError, match="raw report mappings"):
        DispatchRunnerResult(
            DispatchResultOrigin.AUTOMATED,
            DispatchResultState.COMPLETED,
            DispatchPromotionState.RESULT_RECORDED,
            {"status": "COMPLETED"},  # type: ignore[arg-type]
        )


def test_reviewed_manual_observation_cannot_claim_a_paused_state() -> None:
    with pytest.raises(ValueError, match="INCONSISTENT_OUTCOME"):
        normalize_dispatch_runner_result(
            intent_result=None,
            approval_payload={
                "dispatch_result": {
                    "schema_version": "dispatch_runner_result.v1",
                    "result_origin": "MANUAL_RECOVERY",
                    "result_state": "PAUSED",
                    "promotion_state": "RESULT_RECORDED",
                    "run_result": {"status": "MANUAL_RECOVERY_REVIEWED"},
                }
            },
        )
