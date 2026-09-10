# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read staff explanations without reinterpreting or granting review authority."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..coordination.store import rowdict, tx
from ..pow_wow.protocol import ReviewDisposition


class RecordedStaffReview(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)

    kind: Literal["RECORDED"] = "RECORDED"
    artifact_id: str
    verdict: ReviewDisposition
    review_text: str = Field(min_length=1)
    reviewed_commit_sha: str | None = None


class InvalidStaffReview(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)

    kind: Literal["INVALID_ARTIFACT"] = "INVALID_ARTIFACT"
    artifact_id: str
    explanation: str = "Stored review evidence is malformed; its verdict cannot be displayed."


StaffReviewEvidence = Annotated[
    RecordedStaffReview | InvalidStaffReview, Field(discriminator="kind")
]


def parse_staff_review_evidence(artifact_id: str, content: str) -> StaffReviewEvidence:
    """Retain the recorded verdict, including UNCLASSIFIED, beside the actual report.

    Invalid stored evidence is itself an operator-visible finding, not a reason
    to hide the whole cockpit or silently fall back to an earlier approval.
    """

    try:
        payload = json.loads(content)["content"]
        return RecordedStaffReview.model_validate(
            {
                "artifact_id": artifact_id,
                "verdict": payload["verdict"],
                "review_text": payload["review_text"],
                "reviewed_commit_sha": payload.get("reviewed_commit_sha"),
            }
        )
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError, ValidationError):
        return InvalidStaffReview(artifact_id=artifact_id)


def latest_dispatch_reviews(intent_ids: Sequence[str]) -> dict[str, StaffReviewEvidence]:
    """Read one latest review per exact dispatch through its execution leases.

    Task identity isolates attempts, even if their titles match. Only review
    artifacts are fetched, never the large run result or CLI transcripts.
    Multiple leases for one task cannot duplicate a review.
    """

    if not intent_ids:
        return {}
    placeholders = ",".join("?" for _ in intent_ids)
    with tx() as connection:
        rows = connection.execute(
            "SELECT DISTINCT ON (lease.intent_id) "
            "lease.intent_id, artifact.artifact_id, artifact.content "
            "FROM agent_execution_leases lease "
            "JOIN task_artifacts artifact ON artifact.task_id = lease.task_id "
            f"WHERE lease.intent_id IN ({placeholders}) "
            "AND artifact.artifact_type = 'review_result' "
            "AND artifact.schema_version = 'review_result.v1' "
            "ORDER BY lease.intent_id, artifact.created_at DESC, artifact.artifact_id DESC",
            tuple(intent_ids),
        ).fetchall()
    return {
        row["intent_id"]: parse_staff_review_evidence(row["artifact_id"], row["content"])
        for row in (rowdict(item) for item in rows)
    }
