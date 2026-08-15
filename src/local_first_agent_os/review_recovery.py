# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Recover staff decisions that an older host parser failed to classify."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .coordination.outcomes import (
    DispatchPromotionState,
    DispatchResultOrigin,
    DispatchResultState,
)
from .engineering_doctrine import CURRENT_ENGINEERING_DOCTRINE
from .pow_wow.protocol import (
    ReviewDisposition,
    ReviewFindingSeverity,
    ReviewVerdict,
)

REVIEW_VERDICT_RECOVERY_SCHEMA = "review_verdict_recovery.v1"
REVIEW_VERDICT_PARSER_CONTRACT = "review_verdict.v2"


class ReviewRecoveryRefused(ValueError):
    """The failed result is not one exact, recoverable parser miss."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _artifacts(run_result: Mapping[str, Any], artifact_type: str) -> list[Mapping[str, Any]]:
    return [
        _mapping(artifact)
        for task in _sequence(run_result.get("tasks"))
        for artifact in _sequence(_mapping(task).get("artifacts"))
        if _mapping(artifact).get("artifact_type") == artifact_type
    ]


def _content_digest(content: Mapping[str, Any]) -> str:
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RecoveredDispatchReview:
    """A new promotion envelope plus the exact commit it may ask to merge."""

    dispatch_result: dict[str, Any]
    checkpoint: Mapping[str, Any]
    source_review_sha256: str
    decision_line: str


def recover_unparsed_dispatch_review(
    source_intent_id: str,
    intent_result: object,
) -> RecoveredDispatchReview:
    """Reparse one immutable failed result without rewriting its original review."""

    try:
        payload = json.loads(intent_result) if isinstance(intent_result, str) else intent_result
    except json.JSONDecodeError as exc:
        raise ReviewRecoveryRefused(
            "dispatch_result_unreadable", "dispatch result is not valid JSON"
        ) from exc
    result = _mapping(payload)
    if result.get("schema_version") != "dispatch_runner_result.v1":
        raise ReviewRecoveryRefused(
            "dispatch_result_schema_mismatch",
            "dispatch result must use dispatch_runner_result.v1",
        )
    if result.get("result_state") != DispatchResultState.FAILED.value:
        raise ReviewRecoveryRefused(
            "dispatch_result_not_failed", "only a failed dispatch result can be recovered"
        )
    if result.get("promotion_state") != DispatchPromotionState.RESULT_RECORDED.value:
        raise ReviewRecoveryRefused(
            "dispatch_result_already_promoted",
            "dispatch result has already crossed the result-recorded boundary",
        )
    run_result = _mapping(result.get("run_result"))
    checkpoints = _artifacts(run_result, "worktree_commit_checkpoint")
    reviews = _artifacts(run_result, "review_result")
    if not checkpoints:
        raise ReviewRecoveryRefused(
            "verified_checkpoint_missing", "dispatch result has no retained commit checkpoint"
        )
    if not reviews:
        raise ReviewRecoveryRefused(
            "staff_review_missing", "dispatch result has no typed staff review to reparse"
        )
    checkpoint = _mapping(checkpoints[-1].get("content"))
    source_review = _mapping(reviews[-1].get("content"))
    if source_review.get("verdict") != ReviewDisposition.UNCLASSIFIED.value:
        raise ReviewRecoveryRefused(
            "staff_review_not_unclassified",
            "only an unclassified staff review can use parser recovery",
        )
    if not _direct_staff_review_matches_checkpoint(source_review, checkpoint):
        raise ReviewRecoveryRefused(
            "staff_review_provenance_invalid",
            "the unclassified review does not carry complete host-stamped provenance "
            "for the retained commit",
        )
    parsed = ReviewVerdict.parse(_text(source_review.get("review_text")))
    if parsed.disposition is not ReviewDisposition.APPROVE or not parsed.decision_line:
        raise ReviewRecoveryRefused(
            "staff_review_does_not_reparse_to_approve",
            "the current parser does not classify the recorded staff review as APPROVE",
        )

    source_digest = _content_digest(source_review)
    recovered_review = {
        **source_review,
        "verdict": ReviewDisposition.APPROVE.value,
        "decision_line": parsed.decision_line,
        "finding_severity": ReviewFindingSeverity.NON_BLOCKING.value,
        "provenance_stamped_by": "review_verdict_recovery",
        "recovery": {
            "schema_version": REVIEW_VERDICT_RECOVERY_SCHEMA,
            "parser_contract": REVIEW_VERDICT_PARSER_CONTRACT,
            "source_intent_id": source_intent_id,
            "source_review_sha256": source_digest,
            "source_provenance_stamped_by": source_review.get("provenance_stamped_by"),
            "source_verdict": source_review.get("verdict"),
        },
    }
    recovered_run_result = deepcopy(dict(run_result))
    recovered_run_result["status"] = "COMPLETED"
    tasks = list(_sequence(recovered_run_result.get("tasks")))
    tasks.append(
        {
            "task_name": "review_verdict_recovery",
            "role": "host review parser recovery",
            "status": "COMPLETED",
            "summary": (
                "The current typed parser classified the immutable staff decision as APPROVE."
            ),
            "risks": [],
            "artifacts": [
                {
                    "artifact_type": "review_result",
                    "schema_version": "review_result.v1",
                    "content": recovered_review,
                }
            ],
        }
    )
    recovered_run_result["tasks"] = tasks
    recovered = {
        **dict(result),
        "result_origin": DispatchResultOrigin.AUTOMATED_RECOVERY.value,
        "result_state": DispatchResultState.COMPLETED.value,
        "promotion_state": DispatchPromotionState.MERGE_PENDING.value,
        "recovered_from_intent_id": source_intent_id,
        "run_result": recovered_run_result,
    }
    return RecoveredDispatchReview(
        dispatch_result=recovered,
        checkpoint=checkpoint,
        source_review_sha256=source_digest,
        decision_line=parsed.decision_line,
    )


def staff_review_approves_checkpoint(
    final_review: Mapping[str, Any],
    prior_reviews: Sequence[Mapping[str, Any]],
    checkpoint: Mapping[str, Any],
) -> bool:
    """Accept direct host provenance or one exact, digest-bound parser recovery."""

    if not _staff_review_fields_match_checkpoint(final_review, checkpoint):
        return False
    stamped_by = final_review.get("provenance_stamped_by")
    if stamped_by == "pow_wow_executor":
        return True
    if stamped_by != "review_verdict_recovery":
        return False
    recovery = _mapping(final_review.get("recovery"))
    if (
        recovery.get("schema_version") != REVIEW_VERDICT_RECOVERY_SCHEMA
        or recovery.get("parser_contract") != REVIEW_VERDICT_PARSER_CONTRACT
        or recovery.get("source_provenance_stamped_by") != "pow_wow_executor"
        or recovery.get("source_verdict") != ReviewDisposition.UNCLASSIFIED.value
    ):
        return False
    source_digest = _text(recovery.get("source_review_sha256"))
    source = next(
        (review for review in prior_reviews if _content_digest(review) == source_digest),
        None,
    )
    if source is None or not _direct_staff_review_matches_checkpoint(source, checkpoint):
        return False
    parsed = ReviewVerdict.parse(_text(source.get("review_text")))
    if (
        source.get("verdict") != ReviewDisposition.UNCLASSIFIED.value
        or parsed.disposition is not ReviewDisposition.APPROVE
        or parsed.decision_line != final_review.get("decision_line")
    ):
        return False
    expected = {
        **source,
        "verdict": ReviewDisposition.APPROVE.value,
        "decision_line": parsed.decision_line,
        "finding_severity": ReviewFindingSeverity.NON_BLOCKING.value,
        "provenance_stamped_by": "review_verdict_recovery",
        "recovery": dict(recovery),
    }
    return dict(final_review) == expected


def _direct_staff_review_matches_checkpoint(
    review: Mapping[str, Any], checkpoint: Mapping[str, Any]
) -> bool:
    return review.get("provenance_stamped_by") == "pow_wow_executor" and (
        _staff_review_fields_match_checkpoint(review, checkpoint, require_approve=False)
    )


def _staff_review_fields_match_checkpoint(
    review: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    *,
    require_approve: bool = True,
) -> bool:
    required = (
        "execution_lease_id",
        "task_id",
        "reviewed_commit_sha",
        "base_sha",
        "harness",
        "model",
    )
    return (
        (not require_approve or review.get("verdict") == ReviewDisposition.APPROVE.value)
        and review.get("reviewer_tier") == "STAFF"
        and review.get("review_origin") in {"AUTOMATED_STAFF", "RECOVERY_STAFF"}
        and review.get("completion_status") == "COMPLETED"
        and CURRENT_ENGINEERING_DOCTRINE.matches_provenance(review.get("engineering_doctrine"))
        and review.get("reviewed_commit_sha") == checkpoint.get("commit_sha")
        and review.get("base_sha") == checkpoint.get("base_head_sha")
        and all(review.get(field) for field in required)
    )


__all__ = [
    "RecoveredDispatchReview",
    "ReviewRecoveryRefused",
    "recover_unparsed_dispatch_review",
    "staff_review_approves_checkpoint",
]
