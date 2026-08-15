# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Legacy adoption: one owner for existing saga history, with nothing invented.

The Pest project is the case that matters: five milestones complete, the sixth
stopped by an infrastructure persistence failure with a paused dispatch intent.
Adoption has to preserve all of that, and it has to refuse to run until an
operator has confirmed the phase classification it guessed.
"""

from __future__ import annotations

from pathlib import Path

from work_unit_support import install_simulated_engine

from local_first_agent_os.coordination.dispatch import submit_dispatch_intent
from local_first_agent_os.coordination.milestones import (
    complete_saga_milestone,
    create_saga_milestone,
    fail_saga_milestone,
    start_saga_milestone,
)
from local_first_agent_os.coordination.projects import create_saga
from local_first_agent_os.coordination.store import now, tx
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units import service
from local_first_agent_os.work_units.lifecycle import (
    LifecyclePhase,
    MilestoneExecutionStatus,
    WorkUnitStatus,
)
from local_first_agent_os.work_units.reconciliation import (
    LegacySagaKind,
    list_legacy_sagas,
    reconcile_saga,
)
from local_first_agent_os.work_units.root_workflow import EnqueueDelivery

PEST_MILESTONES = (
    "Merge the landing-page skill and wire the skills root",
    "Harden the archetype and copy pipeline on main",
    "Fix the production indexability gate and add structured data",
    "Add the conversion-grade template sections",
    "Emit the operator presence handoff per generated site",
    "Full verification suite plus manual preview review, then staff review and merge approval",
)


def _pest_like_saga() -> tuple[str, list[str], str]:
    """Build the Pest shape: five complete milestones and a stalled sixth.

    The final milestone is left IN_PROGRESS with a paused dispatch intent and a
    failed checkpoint, which is the durable state the real project is in.
    """

    saga_id = str(create_saga("Pest site factory")["saga_id"])
    milestone_ids: list[str] = []
    for index, name in enumerate(PEST_MILESTONES, start=1):
        created = create_saga_milestone(
            saga_id,
            name,
            index,
            description=name,
            exit_criteria=[f"{name} is complete"],
            approval_required=index == len(PEST_MILESTONES),
        )
        milestone_ids.append(str(created["milestone"]["milestone_id"]))

    for milestone_id in milestone_ids[:5]:
        start_saga_milestone(milestone_id)
        complete_saga_milestone(
            milestone_id,
            evidence_type="test_log",
            evidence_content=f"suite passed for {milestone_id}",
            outcome="AUTOMATED_COMPLETION",
        )

    intent_id = str(
        submit_dispatch_intent(
            "senior",
            "run the final verification suite",
            "code",
            None,
            f"saga:{saga_id}:milestone:{milestone_ids[5]}",
        )["intent_id"]
    )
    start_saga_milestone(milestone_ids[5], dispatch_intent_id=intent_id)
    # Stand in for the checkpoint-recovery state the real project reached: the
    # intent is paused and its latest checkpoint failed on a Postgres deadlock
    # while persisting execution events.
    timestamp = now()
    with tx() as c:
        c.execute(
            "UPDATE dispatch_intents SET status='PAUSED' WHERE intent_id=?",
            (intent_id,),
        )
        c.execute(
            """
            INSERT INTO agent_execution_leases(
                lease_id, idempotency_key, intent_id, worker_id, status,
                lease_expires_at, created_at, heartbeat_at
            ) VALUES (?, ?, ?, 'pest-worker', 'FAILED', ?, ?, ?)
            """,
            ("lease_pest_1", "pest-1", intent_id, timestamp, timestamp, timestamp),
        )
        c.execute(
            """
            INSERT INTO agent_execution_checkpoints(
                checkpoint_id, lease_id, intent_id, reason, status, created_at
            ) VALUES (?, ?, ?, ?, 'FAILED', ?)
            """,
            (
                "cp_pest_1",
                "lease_pest_1",
                intent_id,
                "supervisor_error: AccessExclusiveLock on relation",
                timestamp,
            ),
        )
    return saga_id, milestone_ids, intent_id


def test_a_milestone_less_saga_is_a_dispatch_record_not_a_work_unit(
    work_unit_ledger: Path,
) -> None:
    saga_id = str(create_saga("one-off dispatch execution")["saga_id"])

    plan = reconcile_saga(saga_id)

    assert plan.kind is LegacySagaKind.DISPATCH_EXECUTION
    assert plan.work_unit_id is None
    assert plan.applied is False
    assert any("dispatch-execution record" in item for item in plan.blockers)
    assert repo.find_work_unit_by_legacy_saga(saga_id) is None


def test_legacy_sagas_are_listed_with_the_kind_adoption_would_treat_them_as(
    work_unit_ledger: Path,
) -> None:
    project_saga, _, _ = _pest_like_saga()
    dispatch_saga = str(create_saga("child execution saga")["saga_id"])

    kinds = dict(list_legacy_sagas())

    assert kinds[project_saga] is LegacySagaKind.PROJECT_SAGA
    assert kinds[dispatch_saga] is LegacySagaKind.DISPATCH_EXECUTION


def test_a_dry_run_writes_no_work_unit_and_shows_its_classification(
    work_unit_ledger: Path,
) -> None:
    saga_id, _, _ = _pest_like_saga()

    plan = reconcile_saga(saga_id, dry_run=True)

    assert plan.applied is False
    assert repo.find_work_unit_by_legacy_saga(saga_id) is None
    phases = {item.milestone_key: item.phase for item in plan.classifications}
    assert phases["m01"] is LifecyclePhase.PLAN
    assert phases["m02"] is LifecyclePhase.IMPLEMENT
    assert phases["m06"] is LifecyclePhase.VERIFY
    assert all(item.reasoning for item in plan.classifications)


def test_an_unconfirmed_classification_blocks_adoption(work_unit_ledger: Path) -> None:
    saga_id, _, _ = _pest_like_saga()

    plan = reconcile_saga(saga_id, dry_run=False, confirm_classification=False)

    assert plan.applied is False
    assert plan.blockers, "an inferred classification must not execute unconfirmed"
    assert repo.find_work_unit_by_legacy_saga(saga_id) is None


def test_the_pest_case_adopts_five_successes_and_one_blocked_milestone(
    work_unit_ledger: Path,
) -> None:
    saga_id, milestone_ids, intent_id = _pest_like_saga()

    plan = reconcile_saga(saga_id, dry_run=False, confirm_classification=True)

    assert plan.applied is True
    assert plan.work_unit_id is not None
    view = service.get_work_unit(plan.work_unit_id)
    statuses = {item.stable_key: item.status for item in view.milestones}
    assert [statuses[f"m0{index}"] for index in range(1, 6)] == [
        MilestoneExecutionStatus.SUCCEEDED
    ] * 5
    assert statuses["m06"] is MilestoneExecutionStatus.BLOCKED
    assert view.status is WorkUnitStatus.BLOCKED
    # The final milestone is verification work under the fixed lifecycle, so the
    # WorkUnit sits in VERIFY rather than the legacy saga's IMPLEMENTATION stage.
    assert view.current_phase == LifecyclePhase.VERIFY.value
    blocked = next(item for item in view.milestones if item.stable_key == "m06")
    assert blocked.dispatch_intent_id == intent_id
    assert "PAUSED" in str(blocked.failure_summary)
    assert "supervisor_error" in str(blocked.failure_summary)


def test_adopted_successes_carry_the_ledger_evidence_they_came_from(
    work_unit_ledger: Path,
) -> None:
    saga_id, milestone_ids, _ = _pest_like_saga()

    plan = reconcile_saga(saga_id, dry_run=False, confirm_classification=True)

    assert plan.work_unit_id is not None
    artifacts = repo.list_work_unit_artifacts(plan.work_unit_id)
    adopted = [item for item in artifacts if item.metadata.get("legacy_adopted")]
    assert len(adopted) == 5
    for artifact in adopted:
        assert artifact.uri.startswith("legacy://milestone_evidence/")
        assert artifact.metadata["evidence_types"] == ["test_log"]
        assert artifact.content_hash


def test_reconciliation_is_safe_to_repeat(work_unit_ledger: Path) -> None:
    saga_id, _, _ = _pest_like_saga()

    first = reconcile_saga(saga_id, dry_run=False, confirm_classification=True)
    events_after_first = len(repo.list_work_unit_events(str(first.work_unit_id), limit=1000))
    second = reconcile_saga(saga_id, dry_run=False, confirm_classification=True)

    assert second.work_unit_id == first.work_unit_id
    assert (
        len(repo.list_work_unit_events(str(first.work_unit_id), limit=1000)) == events_after_first
    )
    with tx() as c:
        total = c.execute("SELECT COUNT(*) AS n FROM work_units").fetchone()
    assert dict(total)["n"] == 1


def test_the_adoption_appends_an_explicit_migration_event(work_unit_ledger: Path) -> None:
    saga_id, _, _ = _pest_like_saga()

    plan = reconcile_saga(saga_id, dry_run=False, confirm_classification=True)

    assert plan.work_unit_id is not None
    events = repo.list_work_unit_events(plan.work_unit_id, limit=1000)
    migration = next(item for item in events if item.event_type.value == "LEGACY_SAGA_RECONCILED")
    assert migration.payload["saga_id"] == saga_id
    assert migration.payload["milestone_count"] == len(PEST_MILESTONES)


def test_the_resumed_work_unit_continues_from_the_first_incomplete_milestone(
    work_unit_ledger: Path,
) -> None:
    saga_id, _, _ = _pest_like_saga()
    plan = reconcile_saga(saga_id, dry_run=False, confirm_classification=True)
    assert plan.work_unit_id is not None
    runtime = install_simulated_engine()

    service.resume_work_unit(plan.work_unit_id, delivery=EnqueueDelivery.INLINE)

    assert runtime.started == ["m06"], "only the incomplete milestone may run"
    view = service.get_work_unit(plan.work_unit_id)
    assert {item.status for item in view.milestones} == {MilestoneExecutionStatus.SUCCEEDED}
    assert view.status is WorkUnitStatus.SUCCEEDED


def test_a_completed_legacy_milestone_without_evidence_cannot_be_adopted(
    work_unit_ledger: Path,
) -> None:
    saga_id = str(create_saga("evidence-free saga")["saga_id"])
    first = create_saga_milestone(saga_id, "Plan the work", 1, exit_criteria=["planned"])
    second = create_saga_milestone(saga_id, "Verify the suite", 2, exit_criteria=["verified"])
    start_saga_milestone(str(first["milestone"]["milestone_id"]))
    complete_saga_milestone(
        str(first["milestone"]["milestone_id"]),
        outcome="AUTOMATED_COMPLETION",
    )
    fail_saga_milestone(
        str(second["milestone"]["milestone_id"]),
        "nothing ran",
        status="BLOCKED",
    )

    plan = reconcile_saga(saga_id, dry_run=False, confirm_classification=True)

    assert plan.applied is False
    assert any("holds no evidence" in item for item in plan.blockers)
    assert repo.find_work_unit_by_legacy_saga(saga_id) is None
