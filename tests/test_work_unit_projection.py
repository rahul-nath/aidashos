# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The cockpit projection: durable state, rebuildable from events.

The materialized summary exists for fast reads. These tests hold it to the event
log, because a summary that can drift from the history is a summary an operator
cannot trust.
"""

from __future__ import annotations

from pathlib import Path

from work_unit_support import (
    compile_acceptance_doc,
    install_simulated_engine,
    run_acceptance_work_unit,
    start_inline,
)

from local_first_agent_os.contracts import DispatchIntentStatus
from local_first_agent_os.coordination.dispatch import (
    claim_next_dispatch_intent,
    submit_dispatch_intent,
)
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units import service
from local_first_agent_os.work_units.events import DispatchIntentCreated, MilestoneTransition
from local_first_agent_os.work_units.lifecycle import (
    ORDERED_PHASES,
    LifecyclePhase,
    MilestoneExecutionStatus,
    PhaseStatus,
    WorkUnitPhaseMarker,
    WorkUnitStatus,
)
from local_first_agent_os.work_units.projection import (
    build_work_unit_view,
    rebuild_from_events,
)


def test_the_view_carries_everything_the_cockpit_must_show(work_unit_ledger: Path) -> None:
    install_simulated_engine()
    work_unit_id = run_acceptance_work_unit()

    view = build_work_unit_view(work_unit_id)

    assert view.schema_version == "work_unit_view.v1"
    assert view.title
    assert view.design_doc_revision_id.startswith("ddr_")
    assert view.compiled_plan_revision_id.startswith("cpr_")
    assert len(view.compiled_plan_hash) == 64
    assert view.root_workflow_id == f"work-unit:{work_unit_id}"
    assert view.current_phase == WorkUnitPhaseMarker.COMPLETE.value
    assert view.status is WorkUnitStatus.SUCCEEDED
    assert [item.phase for item in view.phases] == list(ORDERED_PHASES)
    assert [item.stable_key for item in view.milestones] == ["a", "b", "c", "d", "e", "f"]
    assert view.blocking.kind == "NONE"
    assert view.pending_decisions == ()
    assert {item.artifact_type for item in view.artifacts} == {
        "implementation_plan",
        "source_patch",
        "test_result",
        "operator_approval",
        "delivery_record",
    }
    assert view.recent_events
    assert view.root_workflow_id in view.dbos_workflow_ids
    # The row-to-evidence join the cockpit uses: each milestone names its
    # execution, and every artifact's execution id resolves to exactly one row.
    executions = {item.milestone_execution_id: item for item in view.milestones}
    assert len(executions) == len(view.milestones)
    assert all(key for key in executions)
    for artifact in view.artifacts:
        if artifact.milestone_execution_id is not None:
            assert artifact.milestone_execution_id in executions


def test_the_view_lists_milestones_in_lifecycle_order_not_alphabetical_order(
    work_unit_ledger: Path,
) -> None:
    install_simulated_engine()
    work_unit_id = run_acceptance_work_unit()

    view = build_work_unit_view(work_unit_id)

    phases = [item.phase for item in view.milestones]
    assert phases == [
        LifecyclePhase.PLAN,
        LifecyclePhase.IMPLEMENT,
        LifecyclePhase.IMPLEMENT,
        LifecyclePhase.VERIFY,
        LifecyclePhase.REVIEW,
        LifecyclePhase.DELIVER,
    ]


def test_the_state_can_be_rebuilt_from_definitions_plus_events(
    work_unit_ledger: Path,
) -> None:
    install_simulated_engine()
    work_unit_id = run_acceptance_work_unit()
    unit = repo.get_work_unit(work_unit_id)
    revision = repo.get_compiled_plan_revision(unit.compiled_plan_revision_id)

    rebuilt = rebuild_from_events(
        revision.plan,
        repo.list_work_unit_events(work_unit_id, limit=1000),
    )

    view = build_work_unit_view(work_unit_id)
    assert rebuilt.status is view.status
    assert rebuilt.current_phase == view.current_phase
    assert rebuilt.milestone_statuses == {item.stable_key: item.status for item in view.milestones}
    assert rebuilt.phase_statuses == {
        item.phase: item.status for item in view.phases if item.status is not PhaseStatus.PENDING
    }
    assert set(rebuilt.artifact_types) == {item.artifact_type for item in view.artifacts}
    assert rebuilt.pending_decision_ids == ()


def test_out_of_order_sibling_completion_does_not_corrupt_the_rebuild(
    work_unit_ledger: Path,
) -> None:
    """Replaying the same events in a shuffled arrival order converges.

    The rebuild sorts by sequence number, so the projection depends on the order
    the facts were recorded rather than the order a reader happens to receive them.
    """

    install_simulated_engine()
    work_unit_id = run_acceptance_work_unit()
    unit = repo.get_work_unit(work_unit_id)
    revision = repo.get_compiled_plan_revision(unit.compiled_plan_revision_id)
    events = list(repo.list_work_unit_events(work_unit_id, limit=1000))

    in_order = rebuild_from_events(revision.plan, events)
    reversed_arrival = rebuild_from_events(revision.plan, list(reversed(events)))

    assert reversed_arrival == in_order


def test_the_rebuild_reports_a_pending_decision_until_it_is_answered(
    work_unit_ledger: Path,
) -> None:
    install_simulated_engine()
    compiled = compile_acceptance_doc()
    assert compiled.compiled_plan_revision_id is not None
    started = start_inline(compiled.compiled_plan_revision_id)
    work_unit_id = str(started["work_unit_id"])
    unit = repo.get_work_unit(work_unit_id)
    revision = repo.get_compiled_plan_revision(unit.compiled_plan_revision_id)

    waiting = rebuild_from_events(
        revision.plan,
        repo.list_work_unit_events(work_unit_id, limit=1000),
    )
    request_id = waiting.pending_decision_ids[0]
    service.submit_work_unit_decision(work_unit_id, request_id, "APPROVED", "idem-1")
    answered = rebuild_from_events(
        revision.plan,
        repo.list_work_unit_events(work_unit_id, limit=1000),
    )

    assert len(waiting.pending_decision_ids) == 1
    assert answered.pending_decision_ids == ()


def test_the_terminal_status_agrees_with_the_milestone_and_phase_outcomes(
    work_unit_ledger: Path,
) -> None:
    install_simulated_engine()
    work_unit_id = run_acceptance_work_unit()

    view = build_work_unit_view(work_unit_id)

    assert view.status is WorkUnitStatus.SUCCEEDED
    assert {item.status for item in view.milestones} == {MilestoneExecutionStatus.SUCCEEDED}
    assert {item.status for item in view.phases} == {
        PhaseStatus.SUCCEEDED,
        PhaseStatus.SKIPPED,
    }


def test_the_projection_does_not_read_model_transcripts(work_unit_ledger: Path) -> None:
    """Everything the view shows comes from the four WorkUnit tables.

    The check is structural: the view's fields are lifecycle facts and evidence
    references. If a future change made the cockpit depend on agent narration, this
    assertion is where it would show up.
    """

    install_simulated_engine()
    work_unit_id = run_acceptance_work_unit()

    payload = build_work_unit_view(work_unit_id).model_dump(mode="json")

    assert set(payload) == {
        "schema_version",
        "work_unit_id",
        "title",
        "status",
        "current_phase",
        "design_doc_revision_id",
        "compiled_plan_revision_id",
        "compiled_plan_hash",
        "lifecycle_profile",
        "lifecycle_profile_version",
        "root_workflow_id",
        "supersedes_work_unit_id",
        "legacy_saga_id",
        "created_at",
        "started_at",
        "completed_at",
        "failure_code",
        "failure_summary",
        "phases",
        "milestones",
        "blocking",
        "pending_decisions",
        "artifacts",
        "recent_events",
        "dbos_workflow_ids",
    }


def test_a_skipped_milestone_shows_why_it_was_skipped(work_unit_ledger: Path) -> None:
    install_simulated_engine()
    compiled = compile_acceptance_doc()
    assert compiled.compiled_plan_revision_id is not None
    started = service.start_work_unit(compiled.compiled_plan_revision_id, delivery=None)
    work_unit_id = str(started["work_unit_id"])
    repo.record_fact(
        work_unit_id,
        MilestoneTransition(
            phase=LifecyclePhase.PLAN,
            milestone_key="a",
            status=MilestoneExecutionStatus.SKIPPED,
            attempt=1,
            failure_code="dependency_unreachable",
            failure_summary="a dependency did not succeed",
        ),
    )

    view = build_work_unit_view(work_unit_id)

    skipped = next(item for item in view.milestones if item.stable_key == "a")
    assert skipped.status is MilestoneExecutionStatus.SKIPPED
    assert skipped.failure_code == "dependency_unreachable"


def test_the_view_distinguishes_a_parked_dispatch_from_a_claimed_one(
    work_unit_ledger: Path,
) -> None:
    """RUNNING is two different facts, and the operator must see which one.

    Twice in one morning a milestone whose intent sat PENDING read as stuck: the
    pill said RUNNING for a parked dispatch and for a working agent alike, and
    the operator's next move differs completely between the two. The view now
    carries the intent's live status beside the milestone's own, read from the
    dispatch ledger at build time.
    """

    compiled = compile_acceptance_doc()
    assert compiled.compiled_plan_revision_id is not None
    started = repo.start_work_unit(compiled.compiled_plan_revision_id, title="parked or working")
    work_unit_id = started.work_unit.work_unit_id
    for status in (MilestoneExecutionStatus.READY, MilestoneExecutionStatus.RUNNING):
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase.PLAN,
                milestone_key="a",
                status=status,
                attempt=1,
            ),
        )
    submitted = submit_dispatch_intent(
        tier="senior",
        prompt="implement milestone a",
        kind="code",
        source=f"work_unit:{work_unit_id}:milestone_execution:0",
    )
    repo.record_fact(
        work_unit_id,
        DispatchIntentCreated(
            phase=LifecyclePhase.PLAN,
            milestone_key="a",
            attempt=1,
            dispatch_intent_id=submitted["intent_id"],
            tier="senior",
            kind="code",
        ),
    )

    parked = next(
        item for item in build_work_unit_view(work_unit_id).milestones if item.stable_key == "a"
    )
    assert parked.status is MilestoneExecutionStatus.RUNNING
    assert parked.dispatch_status is DispatchIntentStatus.PENDING, (
        "an unclaimed intent must read as parked, not as work in progress"
    )

    claimed_intent = claim_next_dispatch_intent("pi-dispatcher")
    assert claimed_intent["intent"]["intent_id"] == submitted["intent_id"]

    working = next(
        item for item in build_work_unit_view(work_unit_id).milestones if item.stable_key == "a"
    )
    assert working.status is MilestoneExecutionStatus.RUNNING
    assert working.dispatch_status is DispatchIntentStatus.CLAIMED, (
        "a claimed intent is an agent working, and must not read as parked"
    )

    untouched = [
        item for item in build_work_unit_view(work_unit_id).milestones if item.stable_key != "a"
    ]
    assert all(item.dispatch_status is None for item in untouched), (
        "milestones with no intent must not borrow anyone else's dispatch state"
    )
