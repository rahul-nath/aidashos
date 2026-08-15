# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Durable WorkUnit persistence: immutability, idempotency, and legal transitions.

The transition operation is the only writer of WorkUnit state, so these tests aim
at it directly rather than through the workflow. A rule that holds here holds for
every caller, because there is no second path in.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from work_unit_support import compile_acceptance_doc

from local_first_agent_os.coordination.store import tx
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.events import (
    ArtifactKind,
    ArtifactRecord,
    MilestoneTransition,
    PhaseTransition,
    RequirableArtifact,
    WorkUnitEventType,
    WorkUnitTransition,
    idempotency_key,
)
from local_first_agent_os.work_units.lifecycle import (
    IllegalTransition,
    LifecyclePhase,
    MilestoneExecutionStatus,
    PhaseStatus,
    WorkUnitStatus,
    assert_work_unit_transition,
)


def _started_work_unit() -> repo.StartWorkUnitResult:
    compiled = compile_acceptance_doc()
    assert compiled.compiled_plan_revision_id is not None
    return repo.start_work_unit(compiled.compiled_plan_revision_id, title="acceptance work")


def test_start_creates_the_unit_its_milestones_its_events_and_its_outbox_row(
    work_unit_ledger: Path,
) -> None:
    result = _started_work_unit()

    assert result.created is True
    assert result.work_unit.status is WorkUnitStatus.QUEUED
    assert result.root_workflow_id == f"work-unit:{result.work_unit.work_unit_id}"

    executions = repo.list_milestone_executions(result.work_unit.work_unit_id)
    assert [item.stable_key for item in executions] == ["a", "b", "c", "d", "e", "f"]
    assert {item.status for item in executions} == {MilestoneExecutionStatus.PENDING}

    events = repo.list_work_unit_events(result.work_unit.work_unit_id)
    assert [item.event_type for item in events] == [
        WorkUnitEventType.WORK_UNIT_CREATED,
        WorkUnitEventType.PLAN_BOUND,
        WorkUnitEventType.ROOT_WORKFLOW_ENQUEUED,
    ]
    assert [item.sequence_number for item in events] == [1, 2, 3]

    pending = repo.list_pending_enqueues()
    assert [item.work_unit_id for item in pending] == [result.work_unit.work_unit_id]


def test_a_repeated_start_returns_the_existing_unit_and_does_not_enqueue_twice(
    work_unit_ledger: Path,
) -> None:
    compiled = compile_acceptance_doc()
    assert compiled.compiled_plan_revision_id is not None

    first = repo.start_work_unit(compiled.compiled_plan_revision_id)
    second = repo.start_work_unit(compiled.compiled_plan_revision_id)

    assert second.created is False
    assert second.work_unit.work_unit_id == first.work_unit.work_unit_id
    with tx() as c:
        rows = c.execute("SELECT COUNT(*) AS n FROM work_unit_enqueue_outbox").fetchone()
        units = c.execute("SELECT COUNT(*) AS n FROM work_units").fetchone()
    assert dict(rows)["n"] == 1
    assert dict(units)["n"] == 1


def test_the_root_workflow_id_is_unique_across_work_units(work_unit_ledger: Path) -> None:
    result = _started_work_unit()

    with tx() as c:
        indexes = {
            str(dict(row)["indexname"])
            for row in c.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename='work_units' AND schemaname=current_schema()"
            ).fetchall()
        }
    assert "idx_work_units_root_workflow" in indexes
    assert repo.find_work_unit_by_root_workflow(result.root_workflow_id) is not None, (
        "the derived root workflow id must resolve back to its work unit"
    )


def test_design_doc_revisions_are_immutable_and_deduplicated(work_unit_ledger: Path) -> None:
    first = repo.insert_design_doc_revision(
        design_doc_id="doc",
        raw_content="# One\n",
        schema_version="parsed_design_doc.v1",
    )
    again = repo.insert_design_doc_revision(
        design_doc_id="doc",
        raw_content="# One\n",
        schema_version="parsed_design_doc.v1",
    )
    second = repo.insert_design_doc_revision(
        design_doc_id="doc",
        raw_content="# Two\n",
        schema_version="parsed_design_doc.v1",
    )

    assert again.design_doc_revision_id == first.design_doc_revision_id
    assert again.revision_number == 1
    assert second.revision_number == 2
    assert repo.get_design_doc_revision(first.design_doc_revision_id).raw_content == "# One\n"


def test_a_replayed_fact_returns_the_original_event_instead_of_a_second_one(
    work_unit_ledger: Path,
) -> None:
    unit = _started_work_unit().work_unit

    first = repo.record_fact(
        unit.work_unit_id,
        PhaseTransition(phase=LifecyclePhase.PLAN, status=PhaseStatus.RUNNING),
    )
    replay = repo.record_fact(
        unit.work_unit_id,
        PhaseTransition(phase=LifecyclePhase.PLAN, status=PhaseStatus.RUNNING),
    )

    assert first.applied is True
    assert replay.applied is False
    assert replay.event.event_id == first.event.event_id
    events = repo.list_work_unit_events(unit.work_unit_id)
    assert sum(1 for item in events if item.event_type is WorkUnitEventType.PHASE_STARTED) == 1


def test_the_idempotency_key_names_the_transition_it_identifies() -> None:
    key = idempotency_key(
        "work-unit:abc",
        MilestoneTransition(
            phase=LifecyclePhase.IMPLEMENT,
            milestone_key="b",
            status=MilestoneExecutionStatus.RUNNING,
            attempt=2,
        ),
    )

    assert key == "work-unit:abc:IMPLEMENT:b:2:MILESTONE_STARTED"


def test_an_illegal_work_unit_transition_is_refused(work_unit_ledger: Path) -> None:
    unit = _started_work_unit().work_unit

    with pytest.raises(IllegalTransition):
        repo.record_fact(unit.work_unit_id, WorkUnitTransition(status=WorkUnitStatus.SUCCEEDED))

    assert repo.get_work_unit(unit.work_unit_id).status is WorkUnitStatus.QUEUED


def test_an_illegal_milestone_transition_is_refused(work_unit_ledger: Path) -> None:
    unit = _started_work_unit().work_unit

    with pytest.raises(IllegalTransition):
        repo.record_fact(
            unit.work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase.PLAN,
                milestone_key="a",
                status=MilestoneExecutionStatus.SUCCEEDED,
                attempt=1,
            ),
        )


def test_the_transition_table_rejects_impossible_work_unit_moves() -> None:
    assert_work_unit_transition(WorkUnitStatus.DRAFT, WorkUnitStatus.COMPILED)
    assert_work_unit_transition(WorkUnitStatus.COMPILED, WorkUnitStatus.QUEUED)
    assert_work_unit_transition(WorkUnitStatus.QUEUED, WorkUnitStatus.RUNNING)
    assert_work_unit_transition(WorkUnitStatus.RUNNING, WorkUnitStatus.WAITING_FOR_OPERATOR)
    assert_work_unit_transition(WorkUnitStatus.WAITING_FOR_OPERATOR, WorkUnitStatus.RUNNING)
    assert_work_unit_transition(WorkUnitStatus.RUNNING, WorkUnitStatus.BLOCKED)
    assert_work_unit_transition(WorkUnitStatus.BLOCKED, WorkUnitStatus.RUNNING)

    for current, requested in (
        (WorkUnitStatus.DRAFT, WorkUnitStatus.RUNNING),
        (WorkUnitStatus.SUCCEEDED, WorkUnitStatus.RUNNING),
        (WorkUnitStatus.QUEUED, WorkUnitStatus.SUCCEEDED),
        (WorkUnitStatus.CANCELLED, WorkUnitStatus.RUNNING),
    ):
        with pytest.raises(IllegalTransition):
            assert_work_unit_transition(current, requested)


def test_a_milestone_cannot_succeed_without_its_required_artifacts(
    work_unit_ledger: Path,
) -> None:
    unit = _started_work_unit().work_unit
    for status in (MilestoneExecutionStatus.READY, MilestoneExecutionStatus.RUNNING):
        repo.record_fact(
            unit.work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase.PLAN,
                milestone_key="a",
                status=status,
                attempt=1,
            ),
        )

    with pytest.raises(repo.MissingRequiredArtifacts) as raised:
        repo.record_fact(
            unit.work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase.PLAN,
                milestone_key="a",
                status=MilestoneExecutionStatus.SUCCEEDED,
                attempt=1,
            ),
        )

    assert raised.value.missing == ("implementation_plan",)


def test_artifacts_recorded_with_the_transition_satisfy_the_evidence_gate(
    work_unit_ledger: Path,
) -> None:
    unit = _started_work_unit().work_unit
    for status in (MilestoneExecutionStatus.READY, MilestoneExecutionStatus.RUNNING):
        repo.record_fact(
            unit.work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase.PLAN,
                milestone_key="a",
                status=status,
                attempt=1,
            ),
        )

    outcome = repo.record_fact(
        unit.work_unit_id,
        MilestoneTransition(
            phase=LifecyclePhase.PLAN,
            milestone_key="a",
            status=MilestoneExecutionStatus.SUCCEEDED,
            attempt=1,
            result_summary="planned",
            artifacts=(
                ArtifactRecord(
                    artifact_type=RequirableArtifact(ArtifactKind.IMPLEMENTATION_PLAN),
                    uri="workunit://plan",
                    content_hash="abc123",
                ),
            ),
        ),
    )

    assert outcome.applied is True
    artifacts = repo.list_work_unit_artifacts(unit.work_unit_id)
    assert [item.artifact_type for item in artifacts] == [
        RequirableArtifact(ArtifactKind.IMPLEMENTATION_PLAN)
    ]
    assert artifacts[0].producer_workflow_id == unit.root_workflow_id


def test_the_event_log_and_the_summary_move_together(work_unit_ledger: Path) -> None:
    unit = _started_work_unit().work_unit

    repo.record_fact(
        unit.work_unit_id,
        WorkUnitTransition(status=WorkUnitStatus.RUNNING, current_phase=LifecyclePhase.PLAN),
    )

    reloaded = repo.get_work_unit(unit.work_unit_id)
    assert reloaded.status is WorkUnitStatus.RUNNING
    assert reloaded.started_at is not None
    assert reloaded.version == unit.version + 1
    latest = repo.list_work_unit_events(unit.work_unit_id)[-1]
    assert latest.event_type is WorkUnitEventType.WORK_UNIT_STARTED


def test_phase_status_is_projected_from_events_not_stored(work_unit_ledger: Path) -> None:
    unit = _started_work_unit().work_unit

    repo.record_fact(
        unit.work_unit_id,
        PhaseTransition(phase=LifecyclePhase.CLARIFY, status=PhaseStatus.SKIPPED),
    )
    repo.record_fact(
        unit.work_unit_id,
        PhaseTransition(phase=LifecyclePhase.PLAN, status=PhaseStatus.RUNNING),
    )

    statuses = repo.phase_statuses(unit.work_unit_id)
    assert statuses[LifecyclePhase.CLARIFY] is PhaseStatus.SKIPPED
    assert statuses[LifecyclePhase.PLAN] is PhaseStatus.RUNNING
    assert LifecyclePhase.DELIVER not in statuses


def test_the_enqueue_outbox_is_only_marked_delivered_once(work_unit_ledger: Path) -> None:
    unit = _started_work_unit().work_unit

    repo.mark_enqueue_delivered(unit.work_unit_id)
    assert repo.list_pending_enqueues() == ()

    repo.mark_enqueue_failed(unit.work_unit_id, "should not resurrect a delivered row")
    assert repo.list_pending_enqueues() == ()


def test_a_failed_enqueue_stays_pending_and_counts_its_attempts(
    work_unit_ledger: Path,
) -> None:
    unit = _started_work_unit().work_unit

    repo.mark_enqueue_failed(unit.work_unit_id, "dbos unavailable")

    pending = repo.list_pending_enqueues()
    assert len(pending) == 1
    assert pending[0].attempts == 1
    assert pending[0].last_error == "dbos unavailable"


def test_starting_a_blocked_plan_is_refused(work_unit_ledger: Path) -> None:
    from work_unit_support import ACCEPTANCE_DESIGN_DOC

    blocked = compile_acceptance_doc(
        design_doc_id="blocked_doc",
        content=ACCEPTANCE_DESIGN_DOC
        + "\n## Unresolved questions\n\n- BLOCKING: who owns the ledger?\n",
    )
    assert blocked.compiled_plan_revision_id is not None

    with pytest.raises(repo.WorkUnitError) as raised:
        repo.start_work_unit(blocked.compiled_plan_revision_id)

    assert "execution blockers" in str(raised.value)
