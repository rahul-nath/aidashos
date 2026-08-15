# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The end-to-end acceptance scenario from the design brief, as one story.

A in PLAN, B and C in IMPLEMENT running in parallel, D in VERIFY gated on both, E
in REVIEW waiting durably for an operator, F in DELIVER only after approval. Each
test below is one of the twelve demonstrations the brief asks for, so a regression
names the property it broke rather than "the acceptance test failed".
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from work_unit_support import (
    ACCEPTANCE_DESIGN_DOC,
    compile_acceptance_doc,
    install_simulated_engine,
    start_inline,
)

from local_first_agent_os.coordination.store import tx
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units import service
from local_first_agent_os.work_units.events import ArtifactKind, RequirableArtifact
from local_first_agent_os.work_units.execution import (
    MilestoneContext,
    MilestoneOutcome,
    MilestoneSucceeded,
    evidence_artifact,
)
from local_first_agent_os.work_units.lifecycle import (
    LifecyclePhase,
    MilestoneExecutionStatus,
    PhaseStatus,
    WorkUnitStatus,
)
from local_first_agent_os.work_units.projection import rebuild_from_events
from local_first_agent_os.work_units.root_workflow import (
    EnqueueDelivery,
    WorkUnitEngine,
    execute_work_unit,
    set_engine,
)


@pytest.fixture()
def acceptance(work_unit_ledger: Path) -> dict[str, str]:
    install_simulated_engine()
    compiled = compile_acceptance_doc()
    assert compiled.compiled_plan_revision_id is not None
    started = start_inline(compiled.compiled_plan_revision_id)
    return {
        "design_doc_revision_id": compiled.design_doc_revision_id,
        "compiled_plan_revision_id": compiled.compiled_plan_revision_id,
        "plan_hash": str(compiled.plan_hash),
        "work_unit_id": str(started["work_unit_id"]),
        "root_workflow_id": str(started["root_workflow_id"]),
    }


def test_1_the_source_design_doc_revision_is_immutable(acceptance: dict[str, str]) -> None:
    revision = repo.get_design_doc_revision(acceptance["design_doc_revision_id"])

    again = service.ingest_design_doc(
        ACCEPTANCE_DESIGN_DOC,
        design_doc_id="acceptance_design_doc",
    )

    assert again.design_doc_revision_id == revision.design_doc_revision_id
    assert again.revision_number == revision.revision_number
    assert (
        repo.get_design_doc_revision(revision.design_doc_revision_id).raw_content
        == ACCEPTANCE_DESIGN_DOC
    )


def test_2_the_compiled_plan_has_stable_provenance_and_hash(acceptance: dict[str, str]) -> None:
    revision = repo.get_compiled_plan_revision(acceptance["compiled_plan_revision_id"])

    assert revision.plan_hash == acceptance["plan_hash"]
    assert revision.plan.plan_hash() == revision.plan_hash
    assert revision.plan.source.design_doc_revision_id == acceptance["design_doc_revision_id"]
    for milestone in revision.plan.milestones:
        assert (
            milestone.source_provenance.design_doc_revision_id
            == (acceptance["design_doc_revision_id"])
        )


def test_3_one_work_unit_row_references_one_root_dbos_workflow_id(
    acceptance: dict[str, str],
) -> None:
    with tx() as c:
        rows = c.execute(
            "SELECT work_unit_id, root_workflow_id FROM work_units WHERE work_unit_id=?",
            (acceptance["work_unit_id"],),
        ).fetchall()
        total = c.execute("SELECT COUNT(*) AS n FROM work_units").fetchone()

    assert len(rows) == 1
    assert dict(rows[0])["root_workflow_id"] == acceptance["root_workflow_id"]
    assert dict(total)["n"] == 1


def test_4_and_5_b_and_c_run_in_parallel_and_d_waits_for_both(
    work_unit_ledger: Path,
) -> None:
    barrier = threading.Barrier(2, timeout=10)
    order: list[str] = []
    lock = threading.Lock()

    class ObservingRuntime:
        def run(self, context: MilestoneContext) -> MilestoneOutcome:
            key = context.milestone.stable_key
            if context.milestone.phase is LifecyclePhase.IMPLEMENT:
                barrier.wait()
            with lock:
                order.append(key)
            artifacts = tuple(
                evidence_artifact(
                    context,
                    RequirableArtifact(ArtifactKind(artifact_type)),
                    content=f"{artifact_type}:{key}",
                    step_name="observe",
                )
                for artifact_type in context.milestone.required_artifacts
            )
            return MilestoneSucceeded(result_summary=f"{key} done", artifacts=artifacts)

    set_engine(
        WorkUnitEngine(
            runtime=ObservingRuntime(),
            approval_wait_seconds=0.0,
            approval_poll_seconds=0.01,
        )
    )
    compiled = compile_acceptance_doc()
    assert compiled.compiled_plan_revision_id is not None

    start_inline(compiled.compiled_plan_revision_id)

    assert order[0] == "a"
    assert set(order[1:3]) == {"b", "c"}
    assert order.index("d") > max(order.index("b"), order.index("c"))


def test_6_the_review_survives_a_full_process_restart_while_waiting(
    acceptance: dict[str, str],
) -> None:
    pending_before = service.pending_operator_decisions(acceptance["work_unit_id"])

    # A brand new engine object with no memory of the first run, which is what a
    # restarted process has.
    install_simulated_engine()
    unit = repo.get_work_unit(acceptance["work_unit_id"])
    execute_work_unit(
        unit.work_unit_id,
        unit.design_doc_revision_id,
        unit.compiled_plan_revision_id,
        unit.compiled_plan_hash,
        unit.lifecycle_profile_version,
    )

    pending_after = service.pending_operator_decisions(acceptance["work_unit_id"])
    assert [item["request_id"] for item in pending_after] == [
        item["request_id"] for item in pending_before
    ]


def test_7_f_cannot_run_before_the_approval(acceptance: dict[str, str]) -> None:
    view = service.get_work_unit(acceptance["work_unit_id"])

    deliver = next(item for item in view.milestones if item.stable_key == "f")
    assert deliver.status is MilestoneExecutionStatus.PENDING
    assert deliver.produced_artifacts == ()
    phases = {item.phase: item.status for item in view.phases}
    assert phases[LifecyclePhase.DELIVER] is PhaseStatus.PENDING

    request_id = view.pending_decisions[0].request_id
    service.submit_work_unit_decision(
        acceptance["work_unit_id"],
        request_id,
        "APPROVED",
        "idem-acceptance",
    )
    service.resume_work_unit(acceptance["work_unit_id"], delivery=EnqueueDelivery.INLINE)

    after = service.get_work_unit(acceptance["work_unit_id"])
    assert next(item for item in after.milestones if item.stable_key == "f").status is (
        MilestoneExecutionStatus.SUCCEEDED
    )


def test_8_every_milestone_produced_its_required_artifacts(acceptance: dict[str, str]) -> None:
    request_id = service.pending_operator_decisions(acceptance["work_unit_id"])[0]["request_id"]
    service.submit_work_unit_decision(
        acceptance["work_unit_id"],
        str(request_id),
        "APPROVED",
        "idem-acceptance",
    )
    service.resume_work_unit(acceptance["work_unit_id"], delivery=EnqueueDelivery.INLINE)

    view = service.get_work_unit(acceptance["work_unit_id"])

    assert view.status is WorkUnitStatus.SUCCEEDED
    for milestone in view.milestones:
        assert set(milestone.required_artifacts) <= set(milestone.produced_artifacts), (
            f"milestone {milestone.stable_key} succeeded without its required evidence"
        )
    for artifact in view.artifacts:
        assert artifact.content_hash


def test_9_the_cockpit_shows_phase_and_milestone_state_without_transcripts(
    acceptance: dict[str, str],
) -> None:
    view = service.get_work_unit(acceptance["work_unit_id"])

    assert [item.phase.value for item in view.phases] == [
        "CLARIFY",
        "VALIDATE",
        "PLAN",
        "IMPLEMENT",
        "VERIFY",
        "REVIEW",
        "DELIVER",
    ]
    assert view.blocking.kind == "OPERATOR_DECISION"
    assert view.blocking.milestone_keys == ("e",)


def test_10_restarting_the_service_does_not_repeat_completed_work(
    acceptance: dict[str, str],
) -> None:
    unit = repo.get_work_unit(acceptance["work_unit_id"])
    events_before = len(repo.list_work_unit_events(unit.work_unit_id, limit=1000))
    runtime = install_simulated_engine()

    execute_work_unit(
        unit.work_unit_id,
        unit.design_doc_revision_id,
        unit.compiled_plan_revision_id,
        unit.compiled_plan_hash,
        unit.lifecycle_profile_version,
    )

    assert runtime.started == [], "no already-completed milestone may execute again"
    # The only new events are the ones the resumed root records about itself.
    events_after = repo.list_work_unit_events(unit.work_unit_id, limit=1000)
    new_types = {item.event_type.value for item in events_after[events_before:]}
    assert new_types <= {
        "WORK_UNIT_STARTED",
        "WORK_UNIT_BLOCKED",
        "PHASE_STARTED",
        "PHASE_BLOCKED",
        "MILESTONE_READY",
        "MILESTONE_WAITING_FOR_OPERATOR",
        "APPROVAL_REQUESTED",
    }


def test_11_submitting_the_same_start_request_does_not_create_another_root_workflow(
    acceptance: dict[str, str],
) -> None:
    again = start_inline(acceptance["compiled_plan_revision_id"])

    assert again["created"] is False
    assert again["work_unit_id"] == acceptance["work_unit_id"]
    assert again["root_workflow_id"] == acceptance["root_workflow_id"]
    with tx() as c:
        total = c.execute("SELECT COUNT(*) AS n FROM work_units").fetchone()
    assert dict(total)["n"] == 1


def test_12_the_full_state_is_explainable_from_the_event_log(
    acceptance: dict[str, str],
) -> None:
    request_id = service.pending_operator_decisions(acceptance["work_unit_id"])[0]["request_id"]
    service.submit_work_unit_decision(
        acceptance["work_unit_id"],
        str(request_id),
        "APPROVED",
        "idem-acceptance",
    )
    service.resume_work_unit(acceptance["work_unit_id"], delivery=EnqueueDelivery.INLINE)
    unit = repo.get_work_unit(acceptance["work_unit_id"])
    revision = repo.get_compiled_plan_revision(unit.compiled_plan_revision_id)

    rebuilt = rebuild_from_events(
        revision.plan,
        repo.list_work_unit_events(unit.work_unit_id, limit=1000),
    )

    assert rebuilt.status is WorkUnitStatus.SUCCEEDED
    assert rebuilt.milestone_statuses == dict.fromkeys(
        ["a", "b", "c", "d", "e", "f"],
        MilestoneExecutionStatus.SUCCEEDED,
    )
    assert rebuilt.phase_statuses[LifecyclePhase.CLARIFY] is PhaseStatus.SKIPPED
    assert rebuilt.phase_statuses[LifecyclePhase.DELIVER] is PhaseStatus.SUCCEEDED
