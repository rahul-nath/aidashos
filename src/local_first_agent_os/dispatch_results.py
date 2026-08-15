# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Typed normalization for automated and manually recovered dispatch results."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .coordination.outcomes import (
    DispatchPromotionState,
    DispatchResultOrigin,
    DispatchResultState,
)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


@dataclass(frozen=True)
class DispatchRunnerResult:
    """Canonical dispatch evidence consumed by review and promotion code."""

    origin: DispatchResultOrigin
    state: DispatchResultState
    promotion_state: DispatchPromotionState
    run_result: Mapping[str, Any]

    @classmethod
    def unavailable(cls) -> DispatchRunnerResult:
        return cls(
            origin=DispatchResultOrigin.UNKNOWN,
            state=DispatchResultState.UNAVAILABLE,
            promotion_state=DispatchPromotionState.RESULT_RECORDED,
            run_result={},
        )


def normalize_dispatch_runner_result(
    *,
    intent_result: object,
    approval_payload: Mapping[str, Any],
) -> DispatchRunnerResult:
    """Normalize every supported result variant into one finite contract.

    New writers may store a typed ``dispatch_result`` envelope directly in an
    approval.  The legacy direct manual-recovery fields remain readable so
    existing durable approvals do not lose their evidence.
    """

    embedded = _mapping(approval_payload.get("dispatch_result"))
    if embedded:
        return _parse_typed_dispatch_result_payload(embedded)
    if approval_payload.get("manual_recovery"):
        return _parse_manual_recovery_dispatch_result(approval_payload)
    return _build_automated_dispatch_result(intent_result)


def _parse_typed_dispatch_result_payload(payload: Mapping[str, Any]) -> DispatchRunnerResult:
    if payload.get("schema_version") != "dispatch_runner_result.v1":
        raise ValueError("dispatch_result must use dispatch_runner_result.v1")
    try:
        origin = DispatchResultOrigin(str(payload.get("result_origin")))
        state = DispatchResultState(str(payload.get("result_state")))
        promotion = DispatchPromotionState(str(payload.get("promotion_state")))
    except ValueError as exc:
        raise ValueError("dispatch_result contains an invalid enum value") from exc
    run_result = _mapping(payload.get("run_result"))
    if not run_result:
        raise ValueError("dispatch_result.run_result is required")
    _validate_origin_state(origin, state)
    _validate_state_promotion(state, promotion)
    return DispatchRunnerResult(origin, state, promotion, run_result)


def _build_automated_dispatch_result(intent_result: object) -> DispatchRunnerResult:
    if not isinstance(intent_result, str) or not intent_result.strip():
        return DispatchRunnerResult.unavailable()
    try:
        payload = json.loads(intent_result)
    except json.JSONDecodeError:
        return DispatchRunnerResult.unavailable()
    result = _mapping(payload)
    if result.get("schema_version") != "dispatch_runner_result.v1":
        return DispatchRunnerResult.unavailable()
    run_result = _mapping(result.get("run_result"))
    if not run_result:
        return DispatchRunnerResult.unavailable()
    state_text = _text(result.get("result_state"))
    state = (
        DispatchResultState(state_text)
        if state_text
        else (
            DispatchResultState.COMPLETED
            if run_result.get("status") == "COMPLETED"
            else DispatchResultState.FAILED
        )
    )
    origin_text = _text(result.get("result_origin"))
    origin = DispatchResultOrigin(origin_text) if origin_text else DispatchResultOrigin.AUTOMATED
    promotion_text = _text(result.get("promotion_state"))
    promotion = (
        DispatchPromotionState(promotion_text)
        if promotion_text
        else (
            DispatchPromotionState.MERGE_PENDING
            if result.get("merge_approval") or _sequence(run_result.get("changed_files"))
            else DispatchPromotionState.RESULT_RECORDED
        )
    )
    _validate_origin_state(origin, state)
    _validate_state_promotion(state, promotion)
    return DispatchRunnerResult(origin, state, promotion, run_result)


def _parse_manual_recovery_dispatch_result(
    payload: Mapping[str, Any],
) -> DispatchRunnerResult:
    staff_review = _mapping(payload.get("staff_review"))
    verdict = _text(staff_review.get("verdict"))
    if not verdict:
        raise ValueError("manual recovery dispatch result requires a staff verdict")
    if verdict != "APPROVE":
        raise ValueError("manual recovery CODE_MERGE evidence requires APPROVE")
    branch = _text(payload.get("branch"))
    base_sha = _text(payload.get("base_sha"))
    commit_sha = _text(payload.get("commit_sha"))
    if not branch or not base_sha or not commit_sha:
        raise ValueError("manual recovery dispatch result requires branch/base/commit")

    initial_verdict = _text(staff_review.get("initial_verdict"))
    initial_finding = _text(staff_review.get("initial_finding"))
    resolution = _text(staff_review.get("resolution"))
    verdict_lines = [f"VERDICT: {verdict}"]
    if initial_verdict or initial_finding:
        verdict_lines.append(
            f"Initial {initial_verdict or 'review'} finding: {initial_finding}".strip()
        )
    if resolution:
        verdict_lines.append(f"Resolution: {resolution}")
    risks = list(_sequence(staff_review.get("risks")))
    changed_files = list(_sequence(payload.get("changed_files")))
    run_result: dict[str, Any] = {
        "status": "MANUAL_RECOVERY_REVIEWED",
        "output_summary": payload.get("purpose"),
        "target_project_id": payload.get("target_project_id"),
        "changed_files": changed_files,
        "verification_commands": [
            _text(item) for item in _sequence(payload.get("verification")) if _text(item)
        ],
        "risks": risks or list(_sequence(payload.get("risks"))),
        "tasks": [
            {
                "task_name": "manual_recovery_operator_evidence",
                "role": "operator evidence reviewer; independent staff provenance unavailable",
                "status": verdict,
                "summary": resolution or initial_finding,
                "risks": risks,
                "artifacts": [
                    {
                        "artifact_type": "operator_recovery_review",
                        "content": {
                            "schema_version": "operator_recovery_review.v1",
                            "verdict": "\n".join(verdict_lines),
                            "review_origin": "OPERATOR_EVIDENCE",
                            "reviewer_tier": "OPERATOR",
                        },
                    }
                ],
            }
        ],
        "artifacts": [
            {
                "artifact_type": "worktree_commit_checkpoint",
                "content": {
                    "branch_name": branch,
                    "base_head_sha": base_sha,
                    "commit_sha": commit_sha,
                    "commit_created": True,
                    "changed_from_base": True,
                    "checkpointed_files": changed_files,
                },
            }
        ],
    }
    return DispatchRunnerResult(
        origin=DispatchResultOrigin.MANUAL_RECOVERY,
        state=DispatchResultState.REVIEWED,
        promotion_state=DispatchPromotionState.REVIEWED,
        run_result=run_result,
    )


def _validate_origin_state(
    origin: DispatchResultOrigin,
    state: DispatchResultState,
) -> None:
    valid = {
        DispatchResultOrigin.AUTOMATED: {
            DispatchResultState.COMPLETED,
            DispatchResultState.FAILED,
        },
        DispatchResultOrigin.AUTOMATED_RECOVERY: {
            DispatchResultState.COMPLETED,
            DispatchResultState.FAILED,
        },
        DispatchResultOrigin.MANUAL_RECOVERY: {
            DispatchResultState.PAUSED,
            DispatchResultState.REVIEWED,
        },
        DispatchResultOrigin.UNKNOWN: {DispatchResultState.UNAVAILABLE},
    }
    if state not in valid[origin]:
        raise ValueError(f"invalid dispatch result origin/state: {origin}/{state}")


def _validate_state_promotion(
    state: DispatchResultState,
    promotion: DispatchPromotionState,
) -> None:
    valid = {
        DispatchResultState.COMPLETED: {
            DispatchPromotionState.RESULT_RECORDED,
            DispatchPromotionState.MERGE_PENDING,
        },
        DispatchResultState.FAILED: {DispatchPromotionState.RESULT_RECORDED},
        DispatchResultState.PAUSED: {DispatchPromotionState.RESULT_RECORDED},
        DispatchResultState.REVIEWED: {DispatchPromotionState.REVIEWED},
        DispatchResultState.UNAVAILABLE: {DispatchPromotionState.RESULT_RECORDED},
    }
    if promotion not in valid[state]:
        raise ValueError(f"invalid dispatch result state/promotion: {state}/{promotion}")


__all__ = ["DispatchRunnerResult", "normalize_dispatch_runner_result"]
