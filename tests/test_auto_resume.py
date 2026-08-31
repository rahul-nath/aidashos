# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The bounded sweep is the only unattended actor that re-drives BLOCKED work.

`retry.attempt_charge` leaves TRANSIENT uncharged because the work was never
judged, and the boundedness of that grant used to rest on nothing re-driving a
BLOCKED milestone at all. These pin the actor that now does: it re-drives only
transient-blocked units, it stops at its cap, it never touches a judged
failure, and it heals the stall a delivered-nothing resume leaves behind
(observed live: work unit e5d41f8805f4, 2026-08-29).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from work_unit_support import compile_acceptance_doc

from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units import root_workflow, service
from local_first_agent_os.work_units.auto_resume import (
    AutoResumeEnqueued,
    AutoResumeRefused,
    sweep_transient_blocked,
)
from local_first_agent_os.work_units.enqueue_drainer import EnqueueDrainer
from local_first_agent_os.work_units.events import (
    MilestoneTransition,
    WorkUnitTransition,
)
from local_first_agent_os.work_units.lifecycle import (
    FailureClass,
    LifecyclePhase,
    MilestoneExecutionStatus,
    WorkUnitStatus,
)
from local_first_agent_os.work_units.root_workflow import EnqueueDelivery

MILESTONE = "a"


def _blocked_unit(
    design_doc_id: str,
    *,
    failure_classes: tuple[FailureClass, ...],
    milestone_key: str = MILESTONE,
) -> str:
    """A BLOCKED WorkUnit whose milestone failed once per given class, in order.

    The last class is the current failure. The START outbox row is marked
    delivered, because a unit that reached BLOCKED in production had its first
    delivery long ago.
    """

    compiled = compile_acceptance_doc(design_doc_id=design_doc_id)
    assert compiled.compiled_plan_revision_id is not None
    started = repo.start_work_unit(compiled.compiled_plan_revision_id, title="auto resume")
    work_unit_id = started.work_unit.work_unit_id
    repo.mark_enqueue_delivered(work_unit_id)
    for attempt, failure_class in enumerate(failure_classes, start=1):
        for status in (MilestoneExecutionStatus.READY, MilestoneExecutionStatus.RUNNING):
            repo.record_fact(
                work_unit_id,
                MilestoneTransition(
                    phase=LifecyclePhase.PLAN,
                    milestone_key=milestone_key,
                    status=status,
                    attempt=attempt,
                ),
            )
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase.PLAN,
                milestone_key=milestone_key,
                status=MilestoneExecutionStatus.BLOCKED,
                attempt=attempt,
                failure_code="the request died in flight",
                failure_class=failure_class,
            ),
        )
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
    return work_unit_id


def _pending_resume_rows() -> list[repo.EnqueueOutboxRow]:
    return [row for row in repo.list_pending_enqueues(10) if row.kind is repo.EnqueueKind.RESUME]


def test_a_transient_block_within_the_bound_is_re_driven(work_unit_ledger: Path) -> None:
    work_unit_id = _blocked_unit("auto_resume_transient", failure_classes=(FailureClass.TRANSIENT,))

    outcomes = sweep_transient_blocked(max_transient_resumes=3)

    assert outcomes == (
        AutoResumeEnqueued(
            work_unit_id=work_unit_id,
            reason="every blocked milestone is transient-blocked within the bound",
        ),
    )
    assert [row.work_unit_id for row in _pending_resume_rows()] == [work_unit_id]


def test_the_failure_beyond_the_bound_stands_for_the_operator(
    work_unit_ledger: Path,
) -> None:
    """The sweep carries the bound that inaction used to provide.

    Four transient failures against a cap of three is a provider that keeps
    dropping; re-driving it again is the loop the old comment promised could
    not happen.
    """

    work_unit_id = _blocked_unit(
        "auto_resume_bounded", failure_classes=(FailureClass.TRANSIENT,) * 4
    )

    outcomes = sweep_transient_blocked(max_transient_resumes=3)

    assert outcomes == (
        AutoResumeRefused(
            work_unit_id=work_unit_id,
            milestone_key=MILESTONE,
            transient_failures=4,
        ),
    )
    assert _pending_resume_rows() == []


def test_a_judged_failure_is_never_re_driven_unattended(work_unit_ledger: Path) -> None:
    """A CORRECTABLE retry spends budget, and spending is an operator's call
    or the workflow's own, never this sweep's."""

    _blocked_unit("auto_resume_judged", failure_classes=(FailureClass.CORRECTABLE,))

    assert sweep_transient_blocked(max_transient_resumes=3) == ()
    assert _pending_resume_rows() == []


def test_a_mixed_block_is_left_for_the_operator(work_unit_ledger: Path) -> None:
    """One resume re-drives every BLOCKED milestone, so one judged failure
    among the transients disqualifies the whole unit."""

    work_unit_id = _blocked_unit("auto_resume_mixed", failure_classes=(FailureClass.TRANSIENT,))
    for status in (MilestoneExecutionStatus.READY, MilestoneExecutionStatus.RUNNING):
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase.PLAN,
                milestone_key="b",
                status=status,
                attempt=1,
            ),
        )
    repo.record_fact(
        work_unit_id,
        MilestoneTransition(
            phase=LifecyclePhase.PLAN,
            milestone_key="b",
            status=MilestoneExecutionStatus.BLOCKED,
            attempt=1,
            failure_code="the agent could not finish",
            failure_class=FailureClass.CORRECTABLE,
        ),
    )

    assert sweep_transient_blocked(max_transient_resumes=3) == ()
    assert _pending_resume_rows() == []


def test_the_stall_a_delivered_nothing_resume_left_is_healed(
    work_unit_ledger: Path,
) -> None:
    """READY milestones on a BLOCKED unit with nothing pending is a resume
    whose continuation never reached a runtime; its retries were already
    permitted, so re-driving spends nothing new."""

    work_unit_id = _blocked_unit("auto_resume_stalled", failure_classes=(FailureClass.TRANSIENT,))
    repo.record_fact(
        work_unit_id,
        MilestoneTransition(
            phase=LifecyclePhase.PLAN,
            milestone_key=MILESTONE,
            status=MilestoneExecutionStatus.READY,
            attempt=2,
        ),
    )

    outcomes = sweep_transient_blocked(max_transient_resumes=3)

    assert outcomes == (
        AutoResumeEnqueued(
            work_unit_id=work_unit_id,
            reason="ready milestones had no pending delivery",
        ),
    )
    assert [row.work_unit_id for row in _pending_resume_rows()] == [work_unit_id]


def test_a_pending_delivery_is_not_re_reported_every_pass(
    work_unit_ledger: Path,
) -> None:
    work_unit_id = _blocked_unit("auto_resume_pending", failure_classes=(FailureClass.TRANSIENT,))
    assert repo.enqueue_resume(work_unit_id) is True

    assert sweep_transient_blocked(max_transient_resumes=3) == ()
    assert [row.work_unit_id for row in _pending_resume_rows()] == [work_unit_id]


def test_zero_disables_the_sweep(work_unit_ledger: Path) -> None:
    _blocked_unit("auto_resume_disabled", failure_classes=(FailureClass.TRANSIENT,))

    assert sweep_transient_blocked(max_transient_resumes=0) == ()
    assert _pending_resume_rows() == []


def test_the_drainer_pass_delivers_what_its_own_sweep_queued(
    monkeypatch: pytest.MonkeyPatch, work_unit_ledger: Path
) -> None:
    """The sweep runs before the drain, so one pass queues and delivers."""

    work_unit_id = _blocked_unit("auto_resume_one_pass", failure_classes=(FailureClass.TRANSIENT,))
    resumed_with: list[str] = []

    def _resume(unit_id: str, *, delivery: EnqueueDelivery) -> dict[str, Any]:
        resumed_with.append(unit_id)
        return {"work_unit_id": unit_id, "delivered": True, "durable": True}

    monkeypatch.setattr(service, "resume_work_unit", _resume)

    outcome = EnqueueDrainer(delivery=EnqueueDelivery.DURABLE).poll_once()

    assert resumed_with == [work_unit_id]
    assert getattr(outcome, "work_unit_ids", ()) == (work_unit_id,)
    assert repo.list_pending_enqueues(10) == ()


def test_the_delivered_sweep_resume_runs_the_full_operator_path(
    monkeypatch: pytest.MonkeyPatch, work_unit_ledger: Path
) -> None:
    """The sweep's row is a RESUME row, so it must never be started fresh."""

    _blocked_unit("auto_resume_full_path", failure_classes=(FailureClass.TRANSIENT,))
    sweep_transient_blocked(max_transient_resumes=3)

    def _must_not_start(*_args: Any) -> dict[str, Any]:
        raise AssertionError("a RESUME row must not be delivered as a first start")

    monkeypatch.setattr(root_workflow, "start_root_workflow", _must_not_start)
    monkeypatch.setattr(
        service,
        "resume_work_unit",
        lambda unit_id, *, delivery: {
            "work_unit_id": unit_id,
            "delivered": True,
            "durable": True,
        },
    )

    outcomes = root_workflow.drain_enqueue_outbox(10, EnqueueDelivery.DURABLE)

    assert [item.to_payload()["delivered"] for item in outcomes] == [True]
