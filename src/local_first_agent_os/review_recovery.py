# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Recover staff decisions that an older host parser failed to classify."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .coordination.outcomes import (
    DispatchPromotionState,
    DispatchResultOrigin,
    DispatchResultState,
)
from .engineering_doctrine import CURRENT_ENGINEERING_DOCTRINE, DoctrineProvenanceStatus
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
        issue = _diagnose_staff_review_fields(source_review, checkpoint, require_approve=False)
        detail = (
            issue.message
            if issue is not None
            else "the review was not stamped by the pow-wow executor"
        )
        raise ReviewRecoveryRefused(
            "staff_review_provenance_invalid",
            "the unclassified review does not carry complete host-stamped provenance "
            f"for the retained commit: {detail}",
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


class StaffReviewProvenanceCode(StrEnum):
    """Every named way a final review can fail to prove the retained commit.

    An enum rather than ad hoc strings because the merge gate keys operator
    remedies off these codes, and a misspelled code would silently fall into
    the generic branch.
    """

    NOT_APPROVED = "staff_review_not_approved"
    REVIEWER_TIER_INVALID = "staff_review_reviewer_tier_invalid"
    REVIEW_ORIGIN_INVALID = "staff_review_origin_invalid"
    REVIEW_NOT_COMPLETED = "staff_review_not_completed"
    DOCTRINE_UNSTAMPED = "doctrine_provenance_missing"
    DOCTRINE_VERSION_STALE = "doctrine_version_stale"
    DOCTRINE_TEXT_DRIFT = "doctrine_text_drift"
    COMMIT_MISMATCH = "staff_review_commit_mismatch"
    BASE_MISMATCH = "staff_review_base_mismatch"
    IDENTITY_INCOMPLETE = "staff_review_identity_incomplete"
    STAMP_UNRECOGNIZED = "staff_review_stamp_unrecognized"
    RECOVERY_ENVELOPE_INVALID = "staff_review_recovery_envelope_invalid"
    RECOVERY_SOURCE_MISSING = "staff_review_recovery_source_missing"
    RECOVERY_SOURCE_INVALID = "staff_review_recovery_source_invalid"
    RECOVERY_REPARSE_MISMATCH = "staff_review_recovery_reparse_mismatch"
    RECOVERY_NOT_EXACT = "staff_review_recovery_not_exact"


DOCTRINE_PROVENANCE_CODES = frozenset(
    {
        StaffReviewProvenanceCode.DOCTRINE_UNSTAMPED,
        StaffReviewProvenanceCode.DOCTRINE_VERSION_STALE,
        StaffReviewProvenanceCode.DOCTRINE_TEXT_DRIFT,
    }
)


@dataclass(frozen=True)
class StaffReviewProvenanceIssue:
    """One named reason the final review does not approve the retained commit."""

    code: StaffReviewProvenanceCode
    message: str


def diagnose_staff_review_provenance(
    final_review: Mapping[str, Any],
    prior_reviews: Sequence[Mapping[str, Any]],
    checkpoint: Mapping[str, Any],
) -> StaffReviewProvenanceIssue | None:
    """Name the first predicate that fails, or ``None`` when the review approves.

    The predicates run in a fixed order, from the review's own claims outward
    to the recovery envelope, so the reported issue is the innermost failure
    rather than an arbitrary one.
    """

    issue = _diagnose_staff_review_fields(final_review, checkpoint)
    if issue is not None:
        return issue
    stamped_by = final_review.get("provenance_stamped_by")
    if stamped_by == "pow_wow_executor":
        return None
    if stamped_by != "review_verdict_recovery":
        return StaffReviewProvenanceIssue(
            StaffReviewProvenanceCode.STAMP_UNRECOGNIZED,
            f"provenance_stamped_by is {stamped_by!r}; only the pow-wow executor or the "
            "review-verdict recovery may stamp a merge-eligible review",
        )
    recovery = _mapping(final_review.get("recovery"))
    if (
        recovery.get("schema_version") != REVIEW_VERDICT_RECOVERY_SCHEMA
        or recovery.get("parser_contract") != REVIEW_VERDICT_PARSER_CONTRACT
        or recovery.get("source_provenance_stamped_by") != "pow_wow_executor"
        or recovery.get("source_verdict") != ReviewDisposition.UNCLASSIFIED.value
    ):
        return StaffReviewProvenanceIssue(
            StaffReviewProvenanceCode.RECOVERY_ENVELOPE_INVALID,
            "the recovery envelope does not name exactly one reparse of one "
            "host-stamped unclassified review",
        )
    source_digest = _text(recovery.get("source_review_sha256"))
    source = next(
        (review for review in prior_reviews if _content_digest(review) == source_digest),
        None,
    )
    if source is None:
        return StaffReviewProvenanceIssue(
            StaffReviewProvenanceCode.RECOVERY_SOURCE_MISSING,
            f"no prior review matches source_review_sha256 {source_digest or '(unset)'}",
        )
    if not _direct_staff_review_matches_checkpoint(source, checkpoint):
        return StaffReviewProvenanceIssue(
            StaffReviewProvenanceCode.RECOVERY_SOURCE_INVALID,
            "the recovery's source review does not carry complete host-stamped "
            "provenance for the retained commit",
        )
    parsed = ReviewVerdict.parse(_text(source.get("review_text")))
    if (
        source.get("verdict") != ReviewDisposition.UNCLASSIFIED.value
        or parsed.disposition is not ReviewDisposition.APPROVE
        or parsed.decision_line != final_review.get("decision_line")
    ):
        return StaffReviewProvenanceIssue(
            StaffReviewProvenanceCode.RECOVERY_REPARSE_MISMATCH,
            "the current parser does not reparse the recorded source review to "
            "this APPROVE decision",
        )
    expected = {
        **source,
        "verdict": ReviewDisposition.APPROVE.value,
        "decision_line": parsed.decision_line,
        "finding_severity": ReviewFindingSeverity.NON_BLOCKING.value,
        "provenance_stamped_by": "review_verdict_recovery",
        "recovery": dict(recovery),
    }
    if dict(final_review) != expected:
        return StaffReviewProvenanceIssue(
            StaffReviewProvenanceCode.RECOVERY_NOT_EXACT,
            "the final review is not exactly the source review plus the recovery reclassification",
        )
    return None


def staff_review_approves_checkpoint(
    final_review: Mapping[str, Any],
    prior_reviews: Sequence[Mapping[str, Any]],
    checkpoint: Mapping[str, Any],
) -> bool:
    """Accept direct host provenance or one exact, digest-bound parser recovery."""

    return diagnose_staff_review_provenance(final_review, prior_reviews, checkpoint) is None


def _direct_staff_review_matches_checkpoint(
    review: Mapping[str, Any], checkpoint: Mapping[str, Any]
) -> bool:
    return review.get("provenance_stamped_by") == "pow_wow_executor" and (
        _diagnose_staff_review_fields(review, checkpoint, require_approve=False) is None
    )


def _diagnose_staff_review_fields(
    review: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    *,
    require_approve: bool = True,
) -> StaffReviewProvenanceIssue | None:
    if require_approve and review.get("verdict") != ReviewDisposition.APPROVE.value:
        return StaffReviewProvenanceIssue(
            StaffReviewProvenanceCode.NOT_APPROVED,
            f"the review verdict is {review.get('verdict')!r}, not "
            f"{ReviewDisposition.APPROVE.value}",
        )
    if review.get("reviewer_tier") != "STAFF":
        return StaffReviewProvenanceIssue(
            StaffReviewProvenanceCode.REVIEWER_TIER_INVALID,
            f"the reviewer tier is {review.get('reviewer_tier')!r}, not STAFF",
        )
    if review.get("review_origin") not in {"AUTOMATED_STAFF", "RECOVERY_STAFF"}:
        return StaffReviewProvenanceIssue(
            StaffReviewProvenanceCode.REVIEW_ORIGIN_INVALID,
            f"the review origin is {review.get('review_origin')!r}, not "
            "AUTOMATED_STAFF or RECOVERY_STAFF",
        )
    if review.get("completion_status") != "COMPLETED":
        return StaffReviewProvenanceIssue(
            StaffReviewProvenanceCode.REVIEW_NOT_COMPLETED,
            f"the review completion status is {review.get('completion_status')!r}, not COMPLETED",
        )
    issue = _diagnose_doctrine_provenance(review.get("engineering_doctrine"))
    if issue is not None:
        return issue
    if review.get("reviewed_commit_sha") != checkpoint.get("commit_sha"):
        return StaffReviewProvenanceIssue(
            StaffReviewProvenanceCode.COMMIT_MISMATCH,
            f"the review names commit {review.get('reviewed_commit_sha')!r} but the "
            f"retained checkpoint is {checkpoint.get('commit_sha')!r}",
        )
    if review.get("base_sha") != checkpoint.get("base_head_sha"):
        return StaffReviewProvenanceIssue(
            StaffReviewProvenanceCode.BASE_MISMATCH,
            f"the review names base {review.get('base_sha')!r} but the retained "
            f"checkpoint's base is {checkpoint.get('base_head_sha')!r}",
        )
    required = (
        "execution_lease_id",
        "task_id",
        "reviewed_commit_sha",
        "base_sha",
        "harness",
        "model",
    )
    missing = [field for field in required if not review.get(field)]
    if missing:
        return StaffReviewProvenanceIssue(
            StaffReviewProvenanceCode.IDENTITY_INCOMPLETE,
            "the review is missing host-stamped identity fields: " + ", ".join(missing),
        )
    return None


def _diagnose_doctrine_provenance(value: object) -> StaffReviewProvenanceIssue | None:
    check = CURRENT_ENGINEERING_DOCTRINE.classify_provenance(value)
    current = CURRENT_ENGINEERING_DOCTRINE
    match check.status:
        case DoctrineProvenanceStatus.CURRENT:
            return None
        case DoctrineProvenanceStatus.UNSTAMPED:
            return StaffReviewProvenanceIssue(
                StaffReviewProvenanceCode.DOCTRINE_UNSTAMPED,
                "the review carries no engineering-doctrine provenance stamp",
            )
        case DoctrineProvenanceStatus.STALE_VERSION:
            stamped = check.stamped_version or "an unknown doctrine version"
            return StaffReviewProvenanceIssue(
                StaffReviewProvenanceCode.DOCTRINE_VERSION_STALE,
                f"the review was conducted under {stamped}; the current doctrine is "
                f"{current.schema_version}. A doctrine bump invalidates every earlier "
                "review by design, so this commit needs a fresh staff review under "
                "the current doctrine",
            )
        case DoctrineProvenanceStatus.TEXT_DRIFT:
            return StaffReviewProvenanceIssue(
                StaffReviewProvenanceCode.DOCTRINE_TEXT_DRIFT,
                f"the review's doctrine stamp names {current.schema_version} with "
                f"sha256 {check.stamped_sha256 or '(unset)'} but the current text of "
                f"that version hashes to {current.sha256}. One version name is "
                "claiming two different texts: the doctrine was edited in place "
                "without a version bump, which the doctrine module forbids",
            )


@dataclass(frozen=True)
class MergeGateEvidence:
    """The exact review and checkpoint artifacts the CODE_MERGE gate judges."""

    reviews: tuple[Mapping[str, Any], ...]
    checkpoint: Mapping[str, Any]

    @property
    def final_review(self) -> Mapping[str, Any] | None:
        return self.reviews[-1] if self.reviews else None


def merge_gate_evidence(run_result: Mapping[str, Any]) -> MergeGateEvidence:
    """Select gate evidence from a run result, run-level and task-level alike.

    One owner for the selection, shared by the approval gate and the doctrine
    staleness scan, so the scan can never report a different population than
    the gate would judge.
    """

    artifacts = [_mapping(item) for item in _sequence(run_result.get("artifacts"))]
    for raw_task in _sequence(run_result.get("tasks")):
        artifacts.extend(_mapping(item) for item in _sequence(_mapping(raw_task).get("artifacts")))
    reviews = tuple(
        _mapping(artifact.get("content"))
        for artifact in artifacts
        if artifact.get("artifact_type") == "review_result"
        and artifact.get("schema_version") == "review_result.v1"
    )
    checkpoints = [
        _mapping(artifact.get("content"))
        for artifact in artifacts
        if artifact.get("artifact_type") == "worktree_commit_checkpoint"
        and _mapping(artifact.get("content")).get("commit_sha")
    ]
    return MergeGateEvidence(reviews=reviews, checkpoint=checkpoints[-1] if checkpoints else {})


__all__ = [
    "DOCTRINE_PROVENANCE_CODES",
    "MergeGateEvidence",
    "RecoveredDispatchReview",
    "ReviewRecoveryRefused",
    "StaffReviewProvenanceCode",
    "StaffReviewProvenanceIssue",
    "diagnose_staff_review_provenance",
    "merge_gate_evidence",
    "recover_unparsed_dispatch_review",
    "staff_review_approves_checkpoint",
]
