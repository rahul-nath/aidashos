# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The root lifecycle: fixed order, empty phases, dependencies, and parallelism.

The engine runs here without DBOS, which is the point: the same bodies execute
under the decorators in production, and the lifecycle rules have to hold on their
own rather than because a workflow engine happened to sequence them.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from work_unit_support import (
    compile_acceptance_doc,
    install_simulated_engine,
    run_acceptance_work_unit,
    settle_operator_decisions,
    start_inline,
)

from local_first_agent_os.work_units import cancellation, service
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.events import ArtifactKind, RequirableArtifact
from local_first_agent_os.work_units.execution import (
    MilestoneContext,
    MilestoneFailed,
    MilestoneOutcome,
    MilestoneSucceeded,
    evidence_artifact,
)
from local_first_agent_os.work_units.lifecycle import (
    ORDERED_PHASES,
    FailureClass,
    LifecyclePhase,
    MilestoneExecutionStatus,
    PhaseStatus,
    WorkUnitStatus,
)
from local_first_agent_os.work_units.projection import (
    WorkUnitCancelResult,
    WorkUnitResumeResult,
)
from local_first_agent_os.work_units.root_workflow import (
    EnqueueDelivery,
    ExecutionInputMismatch,
    WorkUnitEngine,
    execute_work_unit,
    load_execution_snapshot_step,
    set_engine,
)
from local_first_agent_os.work_units.scheduling import (
    compute_phase_work_set,
    evaluate_phase_exit,
)


def _start(**kwargs: Any) -> dict[str, Any]:
    compiled = compile_acceptance_doc()
    assert compiled.compiled_plan_revision_id is not None
    return start_inline(compiled.compiled_plan_revision_id, **kwargs)


def _phase_event_order(work_unit_id: str) -> list[tuple[str, str]]:
    return [
        (event.phase.value, event.event_type.value)
        for event in repo.list_work_unit_events(work_unit_id, limit=1000)
        if event.phase is not None and event.event_type.value.startswith("PHASE_")
    ]


def test_all_seven_phases_occur_in_the_fixed_order(work_unit_ledger: Path) -> None:
    install_simulated_engine()

    work_unit_id = run_acceptance_work_unit()

    ordered = [phase for phase, event in _phase_event_order(work_unit_id)]
    first_touch: list[str] = []
    for phase in ordered:
        if phase not in first_touch:
            first_touch.append(phase)
    assert first_touch == [phase.value for phase in ORDERED_PHASES]


def test_a_phase_with_no_milestones_records_skipped_rather_than_vanishing(
    work_unit_ledger: Path,
) -> None:
    install_simulated_engine()

    started = _start()

    events = _phase_event_order(started["work_unit_id"])
    assert ("CLARIFY", "PHASE_SKIPPED") in events
    assert ("VALIDATE", "PHASE_SKIPPED") in events
    view = service.get_work_unit(started["work_unit_id"])
    statuses = {item.phase: item.status for item in view.phases}
    assert statuses[LifecyclePhase.CLARIFY] is PhaseStatus.SKIPPED
    assert statuses[LifecyclePhase.VALIDATE] is PhaseStatus.SKIPPED


def test_milestones_execute_only_inside_their_assigned_phase(work_unit_ledger: Path) -> None:
    install_simulated_engine()

    started = _start()

    phase_of_milestone = {
        "a": "PLAN",
        "b": "IMPLEMENT",
        "c": "IMPLEMENT",
        "d": "VERIFY",
        "e": "REVIEW",
        "f": "DELIVER",
    }
    for event in repo.list_work_unit_events(started["work_unit_id"], limit=1000):
        key = event.payload.get("milestone_key")
        if key and event.phase is not None:
            assert event.phase.value == phase_of_milestone[str(key)]


def test_the_scheduler_never_offers_a_milestone_from_another_phase(
    work_unit_ledger: Path,
) -> None:
    compiled = compile_acceptance_doc()
    assert compiled.compiled_plan_revision_id is not None
    revision = repo.get_compiled_plan_revision(compiled.compiled_plan_revision_id)
    plan = revision.plan
    # Everything has succeeded except the DELIVER milestone, whose dependency is
    # satisfied. It is still not offered while an earlier phase is being scheduled.
    statuses = {
        "a": MilestoneExecutionStatus.SUCCEEDED,
        "b": MilestoneExecutionStatus.SUCCEEDED,
        "c": MilestoneExecutionStatus.SUCCEEDED,
        "d": MilestoneExecutionStatus.SUCCEEDED,
        "e": MilestoneExecutionStatus.SUCCEEDED,
        "f": MilestoneExecutionStatus.PENDING,
    }

    implement = compute_phase_work_set(plan, LifecyclePhase.IMPLEMENT, statuses)
    deliver = compute_phase_work_set(plan, LifecyclePhase.DELIVER, statuses)

    assert implement.ready == ()
    assert deliver.ready == ("f",)


def test_a_milestone_waits_until_every_dependency_has_succeeded(
    work_unit_ledger: Path,
) -> None:
    compiled = compile_acceptance_doc()
    assert compiled.compiled_plan_revision_id is not None
    plan = repo.get_compiled_plan_revision(compiled.compiled_plan_revision_id).plan

    partial = compute_phase_work_set(
        plan,
        LifecyclePhase.VERIFY,
        {
            "a": MilestoneExecutionStatus.SUCCEEDED,
            "b": MilestoneExecutionStatus.SUCCEEDED,
            "c": MilestoneExecutionStatus.RUNNING,
            "d": MilestoneExecutionStatus.PENDING,
        },
    )
    complete = compute_phase_work_set(
        plan,
        LifecyclePhase.VERIFY,
        {
            "a": MilestoneExecutionStatus.SUCCEEDED,
            "b": MilestoneExecutionStatus.SUCCEEDED,
            "c": MilestoneExecutionStatus.SUCCEEDED,
            "d": MilestoneExecutionStatus.PENDING,
        },
    )

    assert partial.ready == ()
    assert partial.waiting == ("d",)
    assert complete.ready == ("d",)


def test_a_failed_dependency_makes_its_dependents_unreachable(
    work_unit_ledger: Path,
) -> None:
    compiled = compile_acceptance_doc()
    assert compiled.compiled_plan_revision_id is not None
    plan = repo.get_compiled_plan_revision(compiled.compiled_plan_revision_id).plan

    work_set = compute_phase_work_set(
        plan,
        LifecyclePhase.VERIFY,
        {
            "b": MilestoneExecutionStatus.SUCCEEDED,
            "c": MilestoneExecutionStatus.FAILED,
            "d": MilestoneExecutionStatus.PENDING,
        },
    )

    assert work_set.unreachable == ("d",)
    assert work_set.ready == ()


def test_independent_milestones_in_one_phase_run_concurrently(
    work_unit_ledger: Path,
) -> None:
    """Two IMPLEMENT milestones must overlap in time, not merely both finish.

    The barrier is the proof: if the engine ran them one after another, the first
    would wait forever for a sibling that had not started.
    """

    barrier = threading.Barrier(2, timeout=10)
    observed: list[str] = []
    lock = threading.Lock()

    class BarrierRuntime:
        def run(self, context: MilestoneContext) -> MilestoneOutcome:
            if context.milestone.phase is LifecyclePhase.IMPLEMENT:
                barrier.wait()
                with lock:
                    observed.append(context.milestone.stable_key)
            artifacts = tuple(
                evidence_artifact(
                    context,
                    RequirableArtifact(ArtifactKind(artifact_type)),
                    content=f"{artifact_type}:{context.milestone.stable_key}",
                    step_name="barrier",
                )
                for artifact_type in context.milestone.required_artifacts
            )
            return MilestoneSucceeded(result_summary="done", artifacts=artifacts)

    set_engine(
        WorkUnitEngine(
            runtime=BarrierRuntime(),
            approval_wait_seconds=0.0,
            approval_poll_seconds=0.01,
        )
    )

    started = _start()

    assert sorted(observed) == ["b", "c"]
    view = service.get_work_unit(started["work_unit_id"])
    statuses = {item.stable_key: item.status for item in view.milestones}
    assert statuses["b"] is MilestoneExecutionStatus.SUCCEEDED
    assert statuses["c"] is MilestoneExecutionStatus.SUCCEEDED


def test_a_failed_sibling_does_not_erase_the_other_sibling_or_earlier_phases(
    work_unit_ledger: Path,
) -> None:
    install_simulated_engine(failing_milestones=frozenset({"c"}))

    started = _start()

    view = service.get_work_unit(started["work_unit_id"])
    statuses = {item.stable_key: item.status for item in view.milestones}
    assert statuses["a"] is MilestoneExecutionStatus.SUCCEEDED
    assert statuses["b"] is MilestoneExecutionStatus.SUCCEEDED
    assert statuses["c"] is MilestoneExecutionStatus.FAILED
    assert view.status is WorkUnitStatus.FAILED
    phases = {item.phase: item.status for item in view.phases}
    assert phases[LifecyclePhase.PLAN] is PhaseStatus.SUCCEEDED
    assert phases[LifecyclePhase.IMPLEMENT] is PhaseStatus.FAILED
    assert phases[LifecyclePhase.VERIFY] is PhaseStatus.PENDING
    # The successful sibling keeps its evidence.
    assert any(item.artifact_type == "source_patch" for item in view.artifacts)


def test_a_milestone_that_reports_success_without_evidence_fails(
    work_unit_ledger: Path,
) -> None:
    class EvidencelessRuntime:
        def run(self, context: MilestoneContext) -> MilestoneOutcome:
            return MilestoneSucceeded(result_summary="trust me", artifacts=())

    set_engine(
        WorkUnitEngine(
            runtime=EvidencelessRuntime(),
            approval_wait_seconds=0.0,
            approval_poll_seconds=0.01,
        )
    )

    started = _start()

    view = service.get_work_unit(started["work_unit_id"])
    plan_milestone = next(item for item in view.milestones if item.stable_key == "a")
    assert plan_milestone.status is MilestoneExecutionStatus.FAILED
    assert plan_milestone.failure_code == "missing_required_artifacts"
    assert view.status is WorkUnitStatus.FAILED


def test_a_correctable_failure_blocks_rather_than_failing_the_milestone(
    work_unit_ledger: Path,
) -> None:
    class CorrectableRuntime:
        def run(self, context: MilestoneContext) -> MilestoneOutcome:
            return MilestoneFailed(
                failure_class=FailureClass.CORRECTABLE,
                failure_code="patch_conflict",
                failure_summary="the patch did not apply",
            )

    set_engine(
        WorkUnitEngine(
            runtime=CorrectableRuntime(),
            approval_wait_seconds=0.0,
            approval_poll_seconds=0.01,
        )
    )

    started = _start()

    view = service.get_work_unit(started["work_unit_id"])
    plan_milestone = next(item for item in view.milestones if item.stable_key == "a")
    assert plan_milestone.status is MilestoneExecutionStatus.BLOCKED
    assert view.status is WorkUnitStatus.BLOCKED
    assert view.blocking.kind == "BLOCKED_MILESTONE"


def test_re_entering_a_finished_execution_repeats_no_work(work_unit_ledger: Path) -> None:
    runtime = install_simulated_engine()
    unit = repo.get_work_unit(run_acceptance_work_unit())
    assert unit.status is WorkUnitStatus.SUCCEEDED
    first_pass = list(runtime.started)
    events_before = len(repo.list_work_unit_events(unit.work_unit_id, limit=1000))

    execute_work_unit(
        unit.work_unit_id,
        unit.design_doc_revision_id,
        unit.compiled_plan_revision_id,
        unit.compiled_plan_hash,
        unit.lifecycle_profile_version,
    )

    assert runtime.started == first_pass, "no milestone may run a second time"
    assert len(repo.list_work_unit_events(unit.work_unit_id, limit=1000)) == events_before


def test_a_restarted_execution_resumes_without_repeating_completed_phases(
    work_unit_ledger: Path,
) -> None:
    """Simulate a process kill by running with a runtime that stops mid-lifecycle.

    The first engine fails IMPLEMENT so the run halts with PLAN complete. A fresh
    engine, standing in for a restarted process, then completes the work and must
    not re-run the PLAN milestone.
    """

    first_runtime = install_simulated_engine(
        failing_milestones=frozenset({"b", "c"}),
        failure_class=FailureClass.CORRECTABLE,
    )
    started = _start()
    unit = repo.get_work_unit(started["work_unit_id"])
    assert "a" in first_runtime.started
    assert unit.status is WorkUnitStatus.BLOCKED

    second_runtime = install_simulated_engine()
    service.resume_work_unit(unit.work_unit_id, delivery=EnqueueDelivery.INLINE)
    settle_operator_decisions(unit.work_unit_id)

    assert "a" not in second_runtime.started, "a completed milestone must not run again"
    view = service.get_work_unit(unit.work_unit_id)
    assert view.status is WorkUnitStatus.SUCCEEDED
    assert {item.status for item in view.milestones} == {MilestoneExecutionStatus.SUCCEEDED}


def test_cancellation_stops_live_milestones_and_keeps_finished_ones(
    work_unit_ledger: Path,
) -> None:
    install_simulated_engine(
        failing_milestones=frozenset({"c"}),
        failure_class=FailureClass.CORRECTABLE,
    )
    started = _start()
    unit_id = started["work_unit_id"]
    # The run blocked in IMPLEMENT, so VERIFY onward never started.
    assert repo.get_work_unit(unit_id).status is WorkUnitStatus.BLOCKED

    result = service.cancel_work_unit(unit_id, reason="operator stopped the work")

    assert result["cancelled"] is True
    view = service.get_work_unit(unit_id)
    assert view.status is WorkUnitStatus.CANCELLED
    statuses = {item.stable_key: item.status for item in view.milestones}
    assert statuses["a"] is MilestoneExecutionStatus.SUCCEEDED
    assert statuses["b"] is MilestoneExecutionStatus.SUCCEEDED
    assert statuses["c"] is MilestoneExecutionStatus.CANCELLED
    assert statuses["f"] is MilestoneExecutionStatus.CANCELLED


def test_cancelling_is_recorded_before_cancelled(work_unit_ledger: Path) -> None:
    """The ordering is the invariant, not a detail.

    `CANCELLING` exists so that "we were asked to stop" and "we have stopped" are
    different states. Writing `CANCELLED` first, as the old code did, meant a
    crash mid-cascade left a WorkUnit claiming it had stopped while its intents
    and workflows were still live.
    """

    install_simulated_engine(
        failing_milestones=frozenset({"c"}),
        failure_class=FailureClass.CORRECTABLE,
    )
    unit_id = _start()["work_unit_id"]

    service.cancel_work_unit(unit_id, reason="operator stopped the work")

    types = [item.event_type.value for item in repo.list_work_unit_events(unit_id, limit=1000)]
    assert "WORK_UNIT_CANCELLING" in types, "the request itself must be a recorded fact"
    assert types.index("WORK_UNIT_CANCELLING") < types.index("WORK_UNIT_CANCELLED")


def test_a_claimed_intent_is_reported_rather_than_silently_left_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The honesty property: cancel must not imply it stopped what it could not.

    `cancel_dispatch_intent` refuses active states, so an intent whose agent is
    already running cannot be stopped here. The cascade has to surface that,
    because the operator's next move is to cancel its lease or kill the process.
    """

    monkeypatch.setattr(
        "local_first_agent_os.coordination.dispatch.cancel_dispatch_intent",
        lambda *_a, **_k: {
            "ok": False,
            "error": "not_cancelable",
            "message": "Only unstarted or parked dispatch intents can be canceled.",
        },
    )

    attempt = cancellation._stop_dispatch_intent("intent-live", "operator stopped the work")

    assert attempt.verdict is cancellation.StopVerdict.REFUSED
    assert "unstarted or parked" in attempt.detail
    assert cancellation.CancellationResult(
        work_unit_id="wu-1",
        status=WorkUnitStatus.CANCELLED,
        cancelled=True,
        reason="r",
        attempts=(attempt,),
    ).refused == (attempt,)


def test_the_cancel_payload_satisfies_the_route_contract(work_unit_ledger: Path) -> None:
    """The HTTP surface must accept what the service returns.

    `WorkUnitCancelResult` forbids extra keys and the route calls
    `model_validate` on the service payload, so a field added to the cascade and
    not to the contract is a 500 on a route no test exercises. Adding
    `refused`/`awaiting_stop` did exactly that and the whole suite stayed green.
    """

    install_simulated_engine(
        failing_milestones=frozenset({"c"}),
        failure_class=FailureClass.CORRECTABLE,
    )
    unit_id = _start()["work_unit_id"]

    payload = service.cancel_work_unit(unit_id, reason="operator stopped the work")
    validated = WorkUnitCancelResult.model_validate(payload)

    assert validated.cancelled is True
    assert validated.status is WorkUnitStatus.CANCELLED
    # The field an operator has to read is published, not just computed.
    assert "refused" in WorkUnitCancelResult.model_fields


def test_the_resume_payload_satisfies_the_route_contract(work_unit_ledger: Path) -> None:
    """The same gap the cancel route had, on the route beside it.

    `test_the_cancel_payload_satisfies_the_route_contract` was written after a
    field added to the cascade turned that route into a 500 behind a green suite.
    Resume had no equivalent, so adding `recovered` to the service payload did it
    again, on the one route this WorkUnit needed in order to run at all. The
    lesson was recorded and the pin was not generalised; this is the pin.
    """

    install_simulated_engine(
        failing_milestones=frozenset({"c"}),
        failure_class=FailureClass.CORRECTABLE,
    )
    unit_id = _start()["work_unit_id"]
    assert repo.get_work_unit(unit_id).status is WorkUnitStatus.BLOCKED

    payload = service.resume_work_unit(unit_id, delivery=EnqueueDelivery.INLINE)
    validated = WorkUnitResumeResult.model_validate(payload)

    assert validated.work_unit_id == unit_id
    assert validated.delivered is True
    # Reported rather than merely computed: an operator has to be able to tell a
    # resume that repaired a crashed run from one that continued a parked one.
    assert validated.recovered is not None


def test_the_lease_is_what_reaches_a_running_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling a claimed intent is refused; the lease is the handle that works.

    `request_execution_cancel` is cooperative, so the honest verdict is that
    cancellation was requested and a supervisor terminates the process group at
    its next heartbeat. Reporting that as `STOPPED` would claim a process is dead
    while it is still writing files.
    """

    captured: list[str] = []

    def _cancel(lease_id: str, **_k: Any) -> dict[str, Any]:
        captured.append(lease_id)
        return {"ok": True}

    monkeypatch.setattr(
        "local_first_agent_os.coordination.execution.request_execution_cancel", _cancel
    )

    attempt = cancellation._stop_execution_lease("lease-1", "operator stopped the work")

    assert captured == ["lease-1"]
    assert attempt.verdict is cancellation.StopVerdict.CANCELLATION_REQUESTED
    assert attempt.kind is cancellation.StopTargetKind.EXECUTION_LEASE
    result = cancellation.CancellationResult(
        work_unit_id="wu-1",
        status=WorkUnitStatus.CANCELLED,
        cancelled=True,
        reason="r",
        attempts=(attempt,),
    )
    assert result.awaiting_stop == (attempt,)
    assert result.refused == (), "a requested cancellation is not a failure to stop"


def test_a_plan_hash_mismatch_fails_closed(work_unit_ledger: Path) -> None:
    install_simulated_engine()
    started = _start()
    unit = repo.get_work_unit(started["work_unit_id"])

    with pytest.raises(ExecutionInputMismatch):
        load_execution_snapshot_step(
            unit.work_unit_id,
            unit.design_doc_revision_id,
            unit.compiled_plan_revision_id,
            "0" * 64,
            unit.lifecycle_profile_version,
        )


def test_a_design_doc_revision_mismatch_fails_closed(work_unit_ledger: Path) -> None:
    install_simulated_engine()
    started = _start()
    unit = repo.get_work_unit(started["work_unit_id"])

    with pytest.raises(ExecutionInputMismatch):
        load_execution_snapshot_step(
            unit.work_unit_id,
            "ddr_someone_elses_revision",
            unit.compiled_plan_revision_id,
            unit.compiled_plan_hash,
            unit.lifecycle_profile_version,
        )


def test_phase_exit_policy_reports_the_reason_the_phase_stopped(
    work_unit_ledger: Path,
) -> None:
    compiled = compile_acceptance_doc()
    assert compiled.compiled_plan_revision_id is not None
    plan = repo.get_compiled_plan_revision(compiled.compiled_plan_revision_id).plan

    assert evaluate_phase_exit(plan, LifecyclePhase.CLARIFY, {}) is PhaseStatus.SKIPPED, (
        "a phase with no milestones is skipped, not succeeded"
    )
    assert (
        evaluate_phase_exit(
            plan,
            LifecyclePhase.IMPLEMENT,
            {"b": MilestoneExecutionStatus.SUCCEEDED, "c": MilestoneExecutionStatus.SUCCEEDED},
        )
        is PhaseStatus.SUCCEEDED
    )
    assert (
        evaluate_phase_exit(
            plan,
            LifecyclePhase.IMPLEMENT,
            {"b": MilestoneExecutionStatus.SUCCEEDED, "c": MilestoneExecutionStatus.FAILED},
        )
        is PhaseStatus.FAILED
    )
    assert (
        evaluate_phase_exit(
            plan,
            LifecyclePhase.IMPLEMENT,
            {"b": MilestoneExecutionStatus.SUCCEEDED, "c": MilestoneExecutionStatus.BLOCKED},
        )
        is PhaseStatus.BLOCKED
    )
