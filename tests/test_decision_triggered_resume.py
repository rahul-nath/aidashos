# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""An approved override moves the work it unblocks, not just the request row.

The durable wake in `submit_work_unit_decision` is a send to a milestone
workflow, and a BLOCKED WorkUnit's epoch has ended: nothing holds a `recv`, so
the send lands nowhere and the unit used to stay parked until an operator also
typed `resume_work_unit` (observed live, 2026-08-23). These pin the delivery
that closes that gap: the resolution leaves a pending RESUME row in the enqueue
outbox, the drainer delivers it through the operator resume path, and the
answers that must not restart anything - a denial, a unit that is not blocked,
a refused door - do not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from work_unit_support import compile_acceptance_doc

from local_first_agent_os.work_units import commands, root_workflow, service
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.events import (
    MilestoneTransition,
    WorkUnitTransition,
)
from local_first_agent_os.work_units.execution_recovery import (
    ExecutionLiveness,
    RecoveredExecution,
)
from local_first_agent_os.work_units.lifecycle import (
    FailureClass,
    LifecyclePhase,
    MilestoneExecutionStatus,
    WorkUnitStatus,
)
from local_first_agent_os.work_units.root_workflow import EnqueueDelivery

FIRST_MILESTONE = "a"


def _exhausted_blocked_unit(design_doc_id: str, *, halt_unit: bool = True) -> tuple[str, str]:
    """A WorkUnit whose first milestone spent its budget, plus the override
    request a refused resume opened for it.

    Driven through the public surface: the milestone blocks through
    `record_fact`, the unit halts the way `_halt` halts it, and the override
    request is the one `resume_work_unit` itself opens when it refuses. The
    START outbox row is marked delivered first, because a unit that reached
    BLOCKED in production had its first delivery long ago.

    ``halt_unit=False`` leaves the WorkUnit itself unparked, which is the shape
    the not-blocked gate test needs: an exhausted milestone with a resolved
    override on a unit whose status never halted.
    """

    compiled = compile_acceptance_doc(design_doc_id=design_doc_id)
    assert compiled.compiled_plan_revision_id is not None
    started = repo.start_work_unit(compiled.compiled_plan_revision_id, title="decision resume")
    work_unit_id = started.work_unit.work_unit_id
    repo.mark_enqueue_delivered(work_unit_id)
    for attempt in range(1, 4):
        for status in (
            MilestoneExecutionStatus.READY,
            MilestoneExecutionStatus.RUNNING,
        ):
            repo.record_fact(
                work_unit_id,
                MilestoneTransition(
                    phase=LifecyclePhase.PLAN,
                    milestone_key=FIRST_MILESTONE,
                    status=status,
                    attempt=attempt,
                ),
            )
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase.PLAN,
                milestone_key=FIRST_MILESTONE,
                status=MilestoneExecutionStatus.BLOCKED,
                attempt=attempt,
                failure_code="the agent could not finish",
                failure_class=FailureClass.CORRECTABLE,
            ),
        )
    if halt_unit:
        repo.record_fact(
            work_unit_id,
            WorkUnitTransition(status=WorkUnitStatus.RUNNING, current_phase=LifecyclePhase.PLAN),
        )
        repo.record_fact(
            work_unit_id,
            WorkUnitTransition(
                status=WorkUnitStatus.BLOCKED,
                current_phase=LifecyclePhase.PLAN,
                failure_code="plan_phase_blocked",
                failure_summary="phase PLAN cannot proceed without intervention",
            ),
        )
    resumed = service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.DURABLE)
    exhausted = resumed["exhausted"]
    assert [item["milestone_key"] for item in exhausted] == [FIRST_MILESTONE]
    return work_unit_id, str(exhausted[0]["override_request_id"])


def _pending_resume_rows() -> list[repo.EnqueueOutboxRow]:
    return [row for row in repo.list_pending_enqueues(10) if row.kind is repo.EnqueueKind.RESUME]


def test_an_approved_override_on_a_blocked_unit_leaves_a_pending_resume_delivery(
    work_unit_ledger: Path,
) -> None:
    work_unit_id, request_id = _exhausted_blocked_unit("decision_resume_approved")

    result = service.submit_work_unit_decision(
        work_unit_id, request_id, "APPROVED", f"idem-{request_id}"
    )

    assert result["applied"] is True
    assert result["resume"] == {
        "enqueued": True,
        "reason": (
            "the approved override unblocks this BLOCKED work unit; a pending "
            "RESUME delivery awaits the enqueue drainer"
        ),
    }
    rows = _pending_resume_rows()
    assert [row.work_unit_id for row in rows] == [work_unit_id]
    assert rows[0].attempts == 0


def test_a_denied_override_resolves_the_request_and_enqueues_nothing(
    work_unit_ledger: Path,
) -> None:
    """A denial is a person saying the budget stands, not permission to run."""

    work_unit_id, request_id = _exhausted_blocked_unit("decision_resume_denied")

    result = service.submit_work_unit_decision(
        work_unit_id, request_id, "DENIED", f"idem-{request_id}"
    )

    assert result["applied"] is True
    assert result["resume"] is None
    assert repo.list_pending_enqueues(10) == ()


def test_a_decision_on_a_unit_that_is_not_blocked_enqueues_nothing(
    work_unit_ledger: Path,
) -> None:
    """Only a BLOCKED unit has nothing listening.

    A WAITING_FOR_OPERATOR milestone inside a live root keeps its durable wake,
    and everything else has nothing to resume, so the enqueue is gated on the
    one status where the epoch has ended.
    """

    work_unit_id, request_id = _exhausted_blocked_unit(
        "decision_resume_not_blocked", halt_unit=False
    )

    result = service.submit_work_unit_decision(
        work_unit_id, request_id, "APPROVED", f"idem-{request_id}"
    )

    assert result["applied"] is True
    assert result["resume"] is None
    assert repo.get_work_unit(work_unit_id).status is WorkUnitStatus.RUNNING
    assert _pending_resume_rows() == []


def test_replaying_the_resolved_approval_re_ensures_the_delivery(
    work_unit_ledger: Path,
) -> None:
    """The crash window between the fact and the enqueue is healed by resubmission.

    The enqueue commits after the decision does, so a crash between the two
    loses only the delivery. The replay answers `applied: false` and still
    re-derives the resume from the durable request row.
    """

    work_unit_id, request_id = _exhausted_blocked_unit("decision_resume_replay")
    service.submit_work_unit_decision(work_unit_id, request_id, "APPROVED", f"idem-{request_id}")
    repo.mark_enqueue_delivered(work_unit_id)
    assert _pending_resume_rows() == []

    replay = service.submit_work_unit_decision(
        work_unit_id, request_id, "APPROVED", f"idem-{request_id}-again"
    )

    assert replay["applied"] is False
    assert replay["resume"] is not None and replay["resume"]["enqueued"] is True
    assert [row.work_unit_id for row in _pending_resume_rows()] == [work_unit_id]


def test_the_drainer_delivers_a_resume_row_through_the_operator_resume_path(
    monkeypatch: pytest.MonkeyPatch, work_unit_ledger: Path
) -> None:
    """A RESUME row is the whole `service.resume_work_unit`, never a bare start.

    Recovery, the liveness refusal, and the retry decisions are the resume;
    delivering the row through `start_root_workflow` would re-enter a root ID
    DBOS already finished and silently do nothing.
    """

    work_unit_id, request_id = _exhausted_blocked_unit("decision_resume_drain")
    service.submit_work_unit_decision(work_unit_id, request_id, "APPROVED", f"idem-{request_id}")

    resumed_with: list[tuple[str, EnqueueDelivery]] = []

    def _resume(unit_id: str, *, delivery: EnqueueDelivery) -> dict[str, Any]:
        resumed_with.append((unit_id, delivery))
        return {"work_unit_id": unit_id, "delivered": True, "durable": True}

    def _must_not_start(*_args: Any) -> dict[str, Any]:
        raise AssertionError("a RESUME row must not be delivered as a first start")

    monkeypatch.setattr(service, "resume_work_unit", _resume)
    monkeypatch.setattr(root_workflow, "start_root_workflow", _must_not_start)

    outcomes = root_workflow.drain_enqueue_outbox(10, EnqueueDelivery.DURABLE)

    assert resumed_with == [(work_unit_id, EnqueueDelivery.DURABLE)]
    assert [item.to_payload()["delivered"] for item in outcomes] == [True]
    assert repo.list_pending_enqueues(10) == ()


def test_a_resume_row_for_a_terminal_unit_is_consumed_without_resuming(
    monkeypatch: pytest.MonkeyPatch, work_unit_ledger: Path
) -> None:
    """Cancellation racing the decision must not leave a row that fails forever."""

    work_unit_id, request_id = _exhausted_blocked_unit("decision_resume_cancelled")
    service.submit_work_unit_decision(work_unit_id, request_id, "APPROVED", f"idem-{request_id}")
    service.cancel_work_unit(work_unit_id, reason="operator changed their mind")

    def _must_not_resume(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("a terminal work unit must not be resumed")

    monkeypatch.setattr(service, "resume_work_unit", _must_not_resume)

    outcomes = root_workflow.drain_enqueue_outbox(10, EnqueueDelivery.DURABLE)

    payloads = [item.to_payload() for item in outcomes]
    assert [item["delivered"] for item in payloads] == [True]
    assert "obsolete" in str(payloads[0]["reason"])
    assert repo.list_pending_enqueues(10) == ()


def test_a_refused_door_records_the_decision_and_reports_the_refusal(
    monkeypatch: pytest.MonkeyPatch, work_unit_ledger: Path
) -> None:
    """An operator's answer must never be lost, and neither may the reason it
    did not start anything."""

    work_unit_id, request_id = _exhausted_blocked_unit("decision_resume_refused")
    monkeypatch.setattr(
        commands,
        "_budget_refusal",
        lambda: {"ok": False, "error": "connection_budget_exceeded", "message": "budget spent"},
    )

    out = commands.submit_work_unit_decision(
        work_unit_id, request_id, "APPROVED", f"idem-{request_id}"
    )

    assert out["ok"] is True
    assert out["applied"] is True
    assert out["resume"]["enqueued"] is False
    assert "budget spent" in out["resume"]["reason"]
    assert _pending_resume_rows() == []
    request = repo.get_decision_request(request_id)
    assert request is not None and request.decision is not None


def test_an_indeterminate_liveness_answer_leaves_the_resume_row_pending(
    monkeypatch: pytest.MonkeyPatch, work_unit_ledger: Path
) -> None:
    """The intent must survive an answer the runtime could not give.

    `resume_work_unit` refuses when DBOS cannot say whether the previous
    execution ended, because resuming would mint a continuation on an epoch
    nothing repaired. The drain has to read that refusal as "not delivered"
    and leave the row for a later pass. Consuming it would discard an intent
    that never ran, and the operator would see a resolved decision, an
    unblocked-looking WorkUnit, and no execution.

    Milestone 3 of docs/completed/decision_triggered_resume_gawd.md states this as an
    acceptance. It held by inspection and nothing proved it until here.
    """

    work_unit_id, request_id = _exhausted_blocked_unit("decision_resume_indeterminate")
    service.submit_work_unit_decision(work_unit_id, request_id, "APPROVED", f"idem-{request_id}")
    assert [row.work_unit_id for row in _pending_resume_rows()] == [work_unit_id]

    monkeypatch.setattr(
        service,
        "recover_dead_execution",
        lambda _work_unit_id: RecoveredExecution(
            ExecutionLiveness.INDETERMINATE,
            "work-unit:indeterminate",
        ),
    )

    outcomes = root_workflow.drain_enqueue_outbox(10, EnqueueDelivery.DURABLE)

    payloads = [item.to_payload() for item in outcomes]
    assert [item["delivered"] for item in payloads] == [False]
    assert "could not determine whether execution" in str(payloads[0]["reason"])
    # Still RESUME, still pending: a later pass retries it once DBOS answers.
    assert [row.work_unit_id for row in _pending_resume_rows()] == [work_unit_id]
