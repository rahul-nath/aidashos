# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Human-in-the-loop boundaries: named requests, durable waits, and denials.

An approval is a durable request with an identity. These tests pin the four ways
that can be got wrong: approving nothing, approving the wrong thing, approving
twice, treating an unanswered request as a yes, and answering a question where an
approval was asked for.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from work_unit_support import compile_acceptance_doc, install_simulated_engine, start_inline

from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units import service
from local_first_agent_os.work_units.events import (
    Answered,
    Approved,
    DecisionKindMismatch,
    DecisionRequestKind,
    DecisionRequestStatus,
    Denied,
    OperatorDecision,
    decision_outcome,
)
from local_first_agent_os.work_units.lifecycle import (
    MilestoneExecutionStatus,
    WorkUnitStatus,
)
from local_first_agent_os.work_units.root_workflow import EnqueueDelivery


def _blocked_on_approval(work_unit_ledger: Path) -> tuple[str, str]:
    install_simulated_engine()
    compiled = compile_acceptance_doc()
    assert compiled.compiled_plan_revision_id is not None
    started = start_inline(compiled.compiled_plan_revision_id)
    work_unit_id = str(started["work_unit_id"])
    pending = service.pending_operator_decisions(work_unit_id)
    assert len(pending) == 1
    return work_unit_id, str(pending[0]["request_id"])


def test_an_approval_gated_milestone_parks_on_a_named_request(
    work_unit_ledger: Path,
) -> None:
    work_unit_id, request_id = _blocked_on_approval(work_unit_ledger)

    view = service.get_work_unit(work_unit_id)
    review = next(item for item in view.milestones if item.stable_key == "e")
    deliver = next(item for item in view.milestones if item.stable_key == "f")

    assert review.requires_operator_approval is True
    assert review.status is MilestoneExecutionStatus.BLOCKED
    assert deliver.status is MilestoneExecutionStatus.PENDING, (
        "delivery must not run before the review it depends on is approved"
    )
    assert view.blocking.kind == "OPERATOR_DECISION"
    assert [item.request_id for item in view.pending_decisions] == [request_id]


def test_the_request_survives_a_restarted_process(work_unit_ledger: Path) -> None:
    """A fresh engine, standing in for a restarted process, finds the same request.

    The wait is a poll against a durable row, so nothing about it lives in the
    process that created it.
    """

    work_unit_id, request_id = _blocked_on_approval(work_unit_ledger)

    install_simulated_engine()
    service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.INLINE)

    pending = service.pending_operator_decisions(work_unit_id)
    assert [item["request_id"] for item in pending] == [request_id]
    assert repo.get_work_unit(work_unit_id).status is WorkUnitStatus.BLOCKED


def test_approval_resumes_the_milestone_and_completes_the_lifecycle(
    work_unit_ledger: Path,
) -> None:
    work_unit_id, request_id = _blocked_on_approval(work_unit_ledger)

    submitted = service.submit_work_unit_decision(
        work_unit_id,
        request_id,
        "APPROVED",
        "idem-1",
        decided_by="rahul",
    )
    service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.INLINE)

    assert submitted["applied"] is True
    view = service.get_work_unit(work_unit_id)
    assert view.status is WorkUnitStatus.SUCCEEDED
    review = next(item for item in view.milestones if item.stable_key == "e")
    assert review.status is MilestoneExecutionStatus.SUCCEEDED
    assert "operator_approval" in review.produced_artifacts
    assert any(item.artifact_type == "delivery_record" for item in view.artifacts)


def test_a_duplicate_approval_delivery_is_harmless(work_unit_ledger: Path) -> None:
    work_unit_id, request_id = _blocked_on_approval(work_unit_ledger)

    first = service.submit_work_unit_decision(work_unit_id, request_id, "APPROVED", "idem-1")
    second = service.submit_work_unit_decision(work_unit_id, request_id, "APPROVED", "idem-1")

    assert first["applied"] is True
    assert second["applied"] is False
    request = repo.get_decision_request(request_id)
    assert request is not None
    assert request.status is DecisionRequestStatus.RESOLVED
    events = repo.list_work_unit_events(work_unit_id, limit=1000)
    assert sum(1 for item in events if item.event_type.value == "APPROVAL_RECEIVED") == 1


def test_a_decision_for_an_unknown_request_is_rejected(work_unit_ledger: Path) -> None:
    work_unit_id, _ = _blocked_on_approval(work_unit_ledger)

    with pytest.raises(repo.DecisionRequestMismatch):
        service.submit_work_unit_decision(work_unit_id, "wud_not_a_request", "APPROVED", "idem-1")


def test_a_decision_for_another_work_units_request_is_rejected(
    work_unit_ledger: Path,
) -> None:
    work_unit_id, request_id = _blocked_on_approval(work_unit_ledger)
    other = compile_acceptance_doc(design_doc_id="second_doc")
    assert other.compiled_plan_revision_id is not None
    other_unit = service.start_work_unit(other.compiled_plan_revision_id, delivery=None)

    with pytest.raises(repo.DecisionRequestMismatch):
        service.submit_work_unit_decision(
            str(other_unit["work_unit_id"]),
            request_id,
            "APPROVED",
            "idem-cross",
        )

    assert repo.get_work_unit(work_unit_id).status is WorkUnitStatus.BLOCKED


def test_a_malformed_decision_value_is_rejected(work_unit_ledger: Path) -> None:
    work_unit_id, request_id = _blocked_on_approval(work_unit_ledger)

    with pytest.raises(ValueError):
        service.submit_work_unit_decision(work_unit_id, request_id, "LOOKS_FINE", "idem-1")

    request = repo.get_decision_request(request_id)
    assert request is not None
    assert request.status is DecisionRequestStatus.PENDING


def test_a_clarification_answer_cannot_resolve_an_approval(work_unit_ledger: Path) -> None:
    """The fifth way: answering a question that was never asked.

    `ANSWERED` belongs to a CLARIFICATION request. The gate used to test only for
    `DENIED`, so an `ANSWERED` on an approval request read as consent and the
    milestone ran.
    """

    work_unit_id, request_id = _blocked_on_approval(work_unit_ledger)

    with pytest.raises(repo.DecisionRequestMismatch):
        service.submit_work_unit_decision(
            work_unit_id, request_id, OperatorDecision.ANSWERED.value, "idem-1"
        )

    request = repo.get_decision_request(request_id)
    assert request is not None
    assert request.status is DecisionRequestStatus.PENDING


def test_decision_outcome_admits_exactly_the_pairings_that_make_sense() -> None:
    """The rule itself, asserted where it lives rather than through a milestone."""

    assert decision_outcome(DecisionRequestKind.APPROVAL, OperatorDecision.APPROVED) == Approved()
    assert decision_outcome(DecisionRequestKind.APPROVAL, OperatorDecision.DENIED) == Denied()
    answered = decision_outcome(
        DecisionRequestKind.CLARIFICATION, OperatorDecision.ANSWERED, {"answer": "yes"}
    )
    assert answered == Answered(payload={"answer": "yes"})

    for kind, decision in (
        (DecisionRequestKind.APPROVAL, OperatorDecision.ANSWERED),
        (DecisionRequestKind.CLARIFICATION, OperatorDecision.APPROVED),
        (DecisionRequestKind.CLARIFICATION, OperatorDecision.DENIED),
    ):
        with pytest.raises(DecisionKindMismatch):
            decision_outcome(kind, decision)


def test_a_denial_follows_the_compiled_failure_policy(work_unit_ledger: Path) -> None:
    work_unit_id, request_id = _blocked_on_approval(work_unit_ledger)

    service.submit_work_unit_decision(
        work_unit_id,
        request_id,
        "DENIED",
        "idem-deny",
        decided_by="rahul",
    )
    service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.INLINE)

    view = service.get_work_unit(work_unit_id)
    review = next(item for item in view.milestones if item.stable_key == "e")
    deliver = next(item for item in view.milestones if item.stable_key == "f")
    assert review.status is MilestoneExecutionStatus.FAILED
    assert review.failure_code == "operator_denied"
    assert view.status is WorkUnitStatus.FAILED
    assert deliver.status is MilestoneExecutionStatus.PENDING


def test_an_unanswered_request_never_reads_as_approval(work_unit_ledger: Path) -> None:
    work_unit_id, request_id = _blocked_on_approval(work_unit_ledger)

    for _ in range(3):
        install_simulated_engine()
        service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.INLINE)

    view = service.get_work_unit(work_unit_id)
    review = next(item for item in view.milestones if item.stable_key == "e")
    deliver = next(item for item in view.milestones if item.stable_key == "f")
    assert review.status is MilestoneExecutionStatus.BLOCKED
    assert deliver.status is MilestoneExecutionStatus.PENDING
    assert view.status is WorkUnitStatus.BLOCKED
    request = repo.get_decision_request(request_id)
    assert request is not None
    assert request.status is DecisionRequestStatus.PENDING


def test_the_decision_record_is_kept_with_who_made_it(work_unit_ledger: Path) -> None:
    work_unit_id, request_id = _blocked_on_approval(work_unit_ledger)

    service.submit_work_unit_decision(
        work_unit_id,
        request_id,
        "APPROVED",
        "idem-1",
        decided_by="rahul",
        payload={"note": "reviewed the diff"},
    )

    request = repo.get_decision_request(request_id)
    assert request is not None
    assert request.decided_by == "rahul"
    assert request.decision_payload == {"note": "reviewed the diff"}
    approval_event = next(
        item
        for item in repo.list_work_unit_events(work_unit_id, limit=1000)
        if item.event_type.value == "APPROVAL_RECEIVED"
    )
    assert approval_event.payload["decided_by"] == "rahul"
    assert approval_event.payload["request_id"] == request_id
