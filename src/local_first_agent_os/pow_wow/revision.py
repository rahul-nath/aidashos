# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Typed, durable envelope for one staff-blocked revision boundary."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any

from .protocol import ReviewCompletionStatus, ReviewerTier, ReviewOrigin

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class ReviewerOutputReferenceState(StrEnum):
    """Whether the referenced review already has a durable ledger identity."""

    PERSISTENCE_PENDING = "PERSISTENCE_PENDING"
    DURABLE_ARTIFACT = "DURABLE_ARTIFACT"


class ApprovalBoundary(StrEnum):
    """Operator gates that a code revision cannot consume or bypass."""

    CODE_MERGE = "CODE_MERGE"
    DEPLOYMENT = "DEPLOYMENT"
    CREDENTIAL_USE = "CREDENTIAL_USE"
    METERED_SPEND = "METERED_SPEND"
    EXTERNAL_COMMUNICATION = "EXTERNAL_COMMUNICATION"
    PUBLICATION = "PUBLICATION"


@dataclass(frozen=True)
class ReviewerProvenance:
    review_origin: ReviewOrigin
    reviewer_tier: ReviewerTier
    harness: str
    model: str | None
    reasoning_effort: str | None
    execution_lease_id: str | None
    task_id: str | None
    attempt_number: int
    completion_status: ReviewCompletionStatus
    stamped_by: str

    def __post_init__(self) -> None:
        if self.reviewer_tier is not ReviewerTier.STAFF:
            raise ValueError("bounded revision requires a staff reviewer")
        if self.completion_status is not ReviewCompletionStatus.COMPLETED:
            raise ValueError("bounded revision requires a completed review")
        for name, value in (("harness", self.harness), ("stamped_by", self.stamped_by)):
            if not value.strip():
                raise ValueError(f"reviewer provenance requires {name}")
        if self.model is not None and not self.model.strip():
            raise ValueError("reviewer provenance model cannot be empty")
        if self.attempt_number < 1:
            raise ValueError("reviewer attempt_number must be positive")

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["review_origin"] = self.review_origin.value
        payload["reviewer_tier"] = self.reviewer_tier.value
        payload["completion_status"] = self.completion_status.value
        return payload


@dataclass(frozen=True)
class ReviewerOutputReference:
    """Integrity-bound locator for unrestricted, complete reviewer prose."""

    state: ReviewerOutputReferenceState
    artifact_id: str | None
    artifact_type: str
    schema_version: str
    task_name: str
    review_text_sha256: str
    review_text_utf8_bytes: int

    def __post_init__(self) -> None:
        if self.artifact_type != "review_result":
            raise ValueError("review output reference must target review_result")
        if self.schema_version != "review_result.v1":
            raise ValueError("review output reference must target review_result.v1")
        if not self.task_name.strip():
            raise ValueError("review output reference requires task_name")
        if not re.fullmatch(r"[0-9a-f]{64}", self.review_text_sha256):
            raise ValueError("review output reference requires a sha256 digest")
        if self.review_text_utf8_bytes < 1:
            raise ValueError("review output reference requires non-empty reviewer output")
        if self.state is ReviewerOutputReferenceState.DURABLE_ARTIFACT:
            if not self.artifact_id:
                raise ValueError("durable review output reference requires artifact_id")
        elif self.artifact_id is not None:
            raise ValueError("pending review output reference cannot have artifact_id")

    def to_payload(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "schema_version": self.schema_version,
            "task_name": self.task_name,
            "review_text_sha256": self.review_text_sha256,
            "review_text_utf8_bytes": self.review_text_utf8_bytes,
        }

    def bind_durable_artifact(self, artifact_id: str) -> ReviewerOutputReference:
        if not artifact_id.strip():
            raise ValueError("durable review output artifact_id cannot be empty")
        return replace(
            self,
            state=ReviewerOutputReferenceState.DURABLE_ARTIFACT,
            artifact_id=artifact_id,
        )


@dataclass(frozen=True)
class RevisionTarget:
    base_commit_sha: str
    blocked_commit_sha: str
    retained_branch: str
    retained_worktree_path: str

    def __post_init__(self) -> None:
        for name, value in (
            ("base_commit_sha", self.base_commit_sha),
            ("blocked_commit_sha", self.blocked_commit_sha),
        ):
            if not _FULL_SHA.fullmatch(value):
                raise ValueError(f"bounded revision requires exact {name}")
        if self.base_commit_sha == self.blocked_commit_sha:
            raise ValueError("blocked commit must differ from its base")
        if not self.retained_branch.strip() and not self.retained_worktree_path.strip():
            raise ValueError("bounded revision requires a retained branch or worktree")


@dataclass(frozen=True)
class RevisionScope:
    original_task_name: str
    original_task_contract: str
    permission_envelope: str
    allowed_change: str
    forbidden_change: str

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if not value.strip():
                raise ValueError(f"bounded revision scope requires {name}")


@dataclass(frozen=True)
class RevisionVerification:
    commands: tuple[str, ...]
    source: str = "linked_project.verification_commands"
    policy: str = (
        "Rerun every recorded command after revision. If no command is recorded, "
        "promotion remains blocked until explicit verification is defined."
    )

    def __post_init__(self) -> None:
        if any(not command.strip() for command in self.commands):
            raise ValueError("verification commands cannot contain empty values")
        if not self.source.strip() or not self.policy.strip():
            raise ValueError("revision verification requires source and policy")


@dataclass(frozen=True)
class BoundedRevisionContext:
    target: RevisionTarget
    reviewer: ReviewerProvenance
    reviewer_output: ReviewerOutputReference
    revision_scope: RevisionScope
    verification: RevisionVerification
    remaining_approval_boundaries: tuple[ApprovalBoundary, ...]
    schema_version: str = "bounded_revision_context.v1"
    provenance_stamped_by: str = "pow_wow_executor"

    def __post_init__(self) -> None:
        if self.schema_version != "bounded_revision_context.v1":
            raise ValueError("unsupported bounded revision schema version")
        if not self.remaining_approval_boundaries:
            raise ValueError("bounded revision must preserve approval boundaries")
        if len(set(self.remaining_approval_boundaries)) != len(self.remaining_approval_boundaries):
            raise ValueError("bounded revision approval boundaries must be unique")
        if ApprovalBoundary.CODE_MERGE not in self.remaining_approval_boundaries:
            raise ValueError("bounded revision cannot omit the CODE_MERGE boundary")
        if not self.provenance_stamped_by.strip():
            raise ValueError("bounded revision requires host provenance")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "target": asdict(self.target),
            "reviewer": self.reviewer.to_payload(),
            "reviewer_output": self.reviewer_output.to_payload(),
            "revision_scope": asdict(self.revision_scope),
            "verification": {
                "commands": list(self.verification.commands),
                "source": self.verification.source,
                "policy": self.verification.policy,
            },
            "remaining_approval_boundaries": [
                {
                    "boundary": boundary.value,
                    "status": "REQUIRED",
                    "authority": "operator",
                }
                for boundary in self.remaining_approval_boundaries
            ],
            "provenance_stamped_by": self.provenance_stamped_by,
        }

    def with_review_artifact_id(self, artifact_id: str) -> BoundedRevisionContext:
        return replace(
            self,
            reviewer_output=self.reviewer_output.bind_durable_artifact(artifact_id),
        )


DEFAULT_REMAINING_APPROVAL_BOUNDARIES = (
    ApprovalBoundary.CODE_MERGE,
    ApprovalBoundary.DEPLOYMENT,
    ApprovalBoundary.CREDENTIAL_USE,
    ApprovalBoundary.METERED_SPEND,
    ApprovalBoundary.EXTERNAL_COMMUNICATION,
    ApprovalBoundary.PUBLICATION,
)


def build_bounded_revision_context_from_review(
    *,
    review_result: dict[str, Any],
    review_task_name: str,
    review_artifact_id: str | None,
    retained_branch: str,
    retained_worktree_path: str,
    original_task_name: str,
    original_task_contract: str,
    permission_envelope: str,
    verification_commands: tuple[str, ...],
) -> BoundedRevisionContext:
    """Validate host-stamped review evidence and construct the revision envelope."""

    review_text = review_result.get("review_text")
    if not isinstance(review_text, str) or not review_text.strip():
        raise ValueError("bounded revision requires complete reviewer output")
    if review_result.get("finding_severity") != "BLOCKING":
        raise ValueError("bounded revision requires a blocking review")
    text_bytes = review_text.encode("utf-8")
    reference = ReviewerOutputReference(
        state=(
            ReviewerOutputReferenceState.DURABLE_ARTIFACT
            if review_artifact_id
            else ReviewerOutputReferenceState.PERSISTENCE_PENDING
        ),
        artifact_id=review_artifact_id,
        artifact_type="review_result",
        schema_version="review_result.v1",
        task_name=review_task_name,
        review_text_sha256=hashlib.sha256(text_bytes).hexdigest(),
        review_text_utf8_bytes=len(text_bytes),
    )
    return BoundedRevisionContext(
        target=RevisionTarget(
            base_commit_sha=str(review_result.get("base_sha") or ""),
            blocked_commit_sha=str(review_result.get("reviewed_commit_sha") or ""),
            retained_branch=retained_branch,
            retained_worktree_path=retained_worktree_path,
        ),
        reviewer=ReviewerProvenance(
            review_origin=ReviewOrigin(str(review_result.get("review_origin") or "")),
            reviewer_tier=ReviewerTier(str(review_result.get("reviewer_tier") or "")),
            harness=str(review_result.get("harness") or ""),
            model=(str(review_result["model"]) if review_result.get("model") is not None else None),
            reasoning_effort=(
                str(review_result["reasoning_effort"])
                if review_result.get("reasoning_effort") is not None
                else None
            ),
            execution_lease_id=(
                str(review_result["execution_lease_id"])
                if review_result.get("execution_lease_id") is not None
                else None
            ),
            task_id=(
                str(review_result["task_id"]) if review_result.get("task_id") is not None else None
            ),
            attempt_number=int(review_result.get("attempt_number") or 0),
            completion_status=ReviewCompletionStatus(
                str(review_result.get("completion_status") or "")
            ),
            stamped_by=str(review_result.get("provenance_stamped_by") or ""),
        ),
        reviewer_output=reference,
        revision_scope=RevisionScope(
            original_task_name=original_task_name,
            original_task_contract=original_task_contract,
            permission_envelope=permission_envelope,
            allowed_change=(
                "Address or explicitly rebut the complete referenced staff review within "
                "the original task contract on the retained branch/worktree."
            ),
            forbidden_change=(
                "Do not widen permissions, refactor unrelated code, merge, deploy, spend, "
                "publish, use credentials, or contact external parties."
            ),
        ),
        verification=RevisionVerification(commands=verification_commands),
        remaining_approval_boundaries=DEFAULT_REMAINING_APPROVAL_BOUNDARIES,
    )


__all__ = [
    "ApprovalBoundary",
    "BoundedRevisionContext",
    "DEFAULT_REMAINING_APPROVAL_BOUNDARIES",
    "ReviewerOutputReference",
    "ReviewerOutputReferenceState",
    "ReviewerProvenance",
    "RevisionScope",
    "RevisionTarget",
    "RevisionVerification",
    "build_bounded_revision_context_from_review",
]
