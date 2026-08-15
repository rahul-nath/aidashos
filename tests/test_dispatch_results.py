# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json

import pytest

from local_first_agent_os.coordination.outcomes import (
    DispatchPromotionState,
    DispatchResultOrigin,
    DispatchResultState,
    next_dispatch_promotion_states,
    require_dispatch_promotion_transition,
)
from local_first_agent_os.dispatch_results import normalize_dispatch_runner_result


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
    assert result.run_result["tasks"][0]["status"] == "APPROVE"
    assert result.run_result["tasks"][0]["task_name"] == ("manual_recovery_operator_evidence")
    review = result.run_result["tasks"][0]["artifacts"][0]["content"]
    assert review["review_origin"] == "OPERATOR_EVIDENCE"
    assert review["reviewer_tier"] == "OPERATOR"


def test_manual_recovery_merge_fails_closed_without_approval_verdict() -> None:
    with pytest.raises(ValueError, match="requires APPROVE"):
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
    with pytest.raises(ValueError, match="invalid dispatch result state/promotion"):
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
