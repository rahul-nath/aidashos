# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

import json
import uuid
from pathlib import Path

import pytest

from local_first_agent_os.coordination.dispatch import submit_dispatch_intent
from local_first_agent_os.coordination.execution import open_execution_lease
from local_first_agent_os.coordination.pow_wows import create_pow_wow, submit_artifact
from local_first_agent_os.coordination.projects import create_saga
from local_first_agent_os.coordination.store import now, tx
from local_first_agent_os.pow_wow.protocol import ReviewDisposition
from local_first_agent_os.work_units.review_evidence import (
    InvalidStaffReview,
    RecordedStaffReview,
    latest_dispatch_reviews,
    parse_staff_review_evidence,
)


def _content(verdict: str, report: str) -> str:
    return json.dumps({"content": {"verdict": verdict, "review_text": report}})


@pytest.mark.parametrize("verdict", list(ReviewDisposition))
def test_review_prose_does_not_reclassify_the_recorded_verdict(verdict: ReviewDisposition) -> None:
    report = "Staff verdict: BLOCK approval.\nNo code defect is confirmed."
    evidence = parse_staff_review_evidence("review-1", _content(verdict, report))
    assert isinstance(evidence, RecordedStaffReview)
    assert evidence.verdict is verdict
    assert evidence.review_text == report
    assert evidence.artifact_id == "review-1"


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "null",
        "[]",
        "{}",
        '{"content": []}',
        _content("invalid", "report"),
        _content("approve", ""),
    ],
)
def test_invalid_stored_review_is_visible_not_an_approval(content: str) -> None:
    evidence = parse_staff_review_evidence("bad-review", content)
    assert isinstance(evidence, InvalidStaffReview)
    assert evidence.artifact_id == "bad-review"
    assert evidence.kind == "INVALID_ARTIFACT"


def test_latest_review_is_bound_to_the_exact_dispatch_and_invalid_never_falls_back(
    work_unit_ledger: Path,
) -> None:
    saga = create_saga("review evidence")
    pow_wow = create_pow_wow(saga["saga_id"], "IMPLEMENT", "review evidence", "reviewed")
    intent_ids: list[str] = []
    for ordinal in range(2):
        intent = submit_dispatch_intent(tier="senior", prompt="same milestone", kind="code")
        intent_ids.append(intent["intent_id"])
        task_id = str(uuid.uuid4())
        with tx() as connection:
            connection.execute(
                "INSERT INTO saga_tasks "
                "(task_id,pow_wow_id,saga_id,task_name,description,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    task_id,
                    pow_wow["pow_wow_id"],
                    saga["saga_id"],
                    "staff review",
                    "review",
                    now(),
                    now(),
                ),
            )
        for lease_ordinal in range(2):
            assert open_execution_lease(
                f"review-{ordinal}-{lease_ordinal}",
                "test",
                intent_id=intent["intent_id"],
                task_id=task_id,
            )["ok"]
        assert submit_artifact(
            pow_wow["pow_wow_id"],
            "review_result",
            _content("approve", f"Report {ordinal}"),
            task_id=task_id,
            schema_version="review_result.v1",
        )["ok"]
        if ordinal == 0:
            invalid = submit_artifact(
                pow_wow["pow_wow_id"],
                "review_result",
                "{}",
                task_id=task_id,
                schema_version="review_result.v1",
            )
            assert invalid["ok"]
        assert submit_artifact(
            pow_wow["pow_wow_id"],
            "cli_agent_run",
            "private transcript must not be projected",
            task_id=task_id,
            schema_version="cli_agent_run.v1",
        )["ok"]

    reviews = latest_dispatch_reviews([*intent_ids, "unknown"])
    assert set(reviews) == set(intent_ids)
    assert isinstance(reviews[intent_ids[0]], InvalidStaffReview)
    second = reviews[intent_ids[1]]
    assert isinstance(second, RecordedStaffReview)
    assert second.review_text == "Report 1"
    assert latest_dispatch_reviews([]) == {}
