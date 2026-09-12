# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A wait-elapsed milestone may adopt its own dispatch once that settled DONE.

`DEADLINE_EXCEEDED` parks the milestone while the dispatch keeps running, while
historical `dispatch_wait_elapsed` rows remain readable. When the dispatch later
settles DONE, its evidence is complete and checkable but no lifecycle state can
reach it: a resume mints a rival attempt, so work that reliably outlives its
compiled bound is re-spent forever. These tests pin the narrow repair: only that
block reason, only the milestone's own intent, only a DONE settlement, and only
evidence the normal translation accepts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_plan_evidence import PLAN, REPORT, _dispatch
from work_unit_support import compile_acceptance_doc

from local_first_agent_os.coordination.dispatch import (
    claim_next_dispatch_intent,
    complete_dispatch_intent,
    submit_dispatch_intent,
)
from local_first_agent_os.coordination.dispatch_diagnostics import DISPATCH_CONTRACT_VIOLATION_EVENT
from local_first_agent_os.coordination.execution import list_ledger_events
from local_first_agent_os.coordination.store import tx
from local_first_agent_os.dispatch_contracts import DispatchIngressFailureCode
from local_first_agent_os.runtime import AppRuntime
from local_first_agent_os.work_units import dispatch_adoption
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.events import MilestoneTransition, WorkUnitTransition
from local_first_agent_os.work_units.execution import DISPATCH_WAIT_FAILURE_CODE
from local_first_agent_os.work_units.lifecycle import (
    FailureClass,
    LifecyclePhase,
    MilestoneExecutionStatus,
    WorkUnitStatus,
)
from local_first_agent_os.work_units.plan import CompiledWorkPlan


def _settled_plan() -> CompiledWorkPlan:
    compiled = compile_acceptance_doc(design_doc_id="settled_adoption")
    assert compiled.compiled_plan_revision_id is not None
    return repo.get_compiled_plan_revision(compiled.compiled_plan_revision_id).plan


def _runner_result(
    *, changed_files: tuple[str, ...] = ("feature.py",), intent_id: str = "fixture-intent"
) -> str:
    target_project_id = _settled_plan().target_project_id
    return json.dumps(
        {
            "schema_version": "dispatch_runner_result.v1",
            "intent_id": intent_id,
            "target_project_id": target_project_id,
            "run_result": {
                "status": "COMPLETED",
                "target_project_id": target_project_id,
                "output_summary": "implemented and verified the milestone contract",
                "changed_files": list(changed_files),
                "verification_commands": ["uv run pytest -q"],
                "verification_output": ["all tests passed"],
                "tasks": [],
            },
        }
    )


def _settled_intent(*, status: str = "DONE", result: str | None = None) -> str:
    submitted = submit_dispatch_intent(
        "senior",
        "implement the milestone",
        kind="code",
        target_project_id=_settled_plan().target_project_id,
        permitted_capabilities=("read_repository", "run_command"),
    )
    intent_id = str(submitted["intent_id"])
    claim_next_dispatch_intent("test-worker", "senior")
    if status != "CLAIMED":
        if result is not None:
            try:
                payload = json.loads(result)
                if isinstance(payload, dict) and payload.get("intent_id") == "fixture-intent":
                    payload["intent_id"] = intent_id
                    result = json.dumps(payload)
            except ValueError:
                pass
        completed = complete_dispatch_intent(
            intent_id,
            status,
            result=_runner_result(intent_id=intent_id) if result is None else result,
            error=None if status == "DONE" else "senior turn failed",
        )
        assert completed["ok"], completed
    return intent_id


def _legacy_settled_intent(*, result: str | None) -> str:
    """Seed retained pre-enforcement history only in the isolated test ledger.

    Fresh public completion now refuses malformed reports, so recovery tests
    must explicitly construct historical corruption rather than ignore that
    refusal and pretend the current public writer accepted it.
    """

    intent_id = _settled_intent(status="CLAIMED")
    if result is not None:
        try:
            payload = json.loads(result)
            if isinstance(payload, dict) and payload.get("intent_id") == "fixture-intent":
                payload["intent_id"] = intent_id
                result = json.dumps(payload)
        except ValueError:
            pass
    with tx() as connection:
        connection.execute(
            "UPDATE dispatch_intents SET status='DONE', result=? WHERE intent_id=?",
            (result, intent_id),
        )
    return intent_id


def _wait_elapsed_milestone(
    intent_id: str,
    *,
    failure_code: str = DISPATCH_WAIT_FAILURE_CODE,
    phase: LifecyclePhase = LifecyclePhase.IMPLEMENT,
) -> tuple[str, str]:
    compiled = compile_acceptance_doc(design_doc_id="settled_adoption")
    assert compiled.compiled_plan_revision_id is not None
    started = repo.start_work_unit(compiled.compiled_plan_revision_id)
    work_unit_id = started.work_unit.work_unit_id
    milestone = next(
        item for item in repo.list_milestone_executions(work_unit_id) if item.phase is phase
    )
    repo.record_fact(
        work_unit_id,
        WorkUnitTransition(status=WorkUnitStatus.RUNNING, current_phase=phase),
    )
    for status in (
        MilestoneExecutionStatus.READY,
        MilestoneExecutionStatus.RUNNING,
        MilestoneExecutionStatus.BLOCKED,
    ):
        blocked = status is MilestoneExecutionStatus.BLOCKED
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=phase,
                milestone_key=milestone.stable_key,
                status=status,
                attempt=1,
                dispatch_intent_id=intent_id,
                failure_class=FailureClass.CORRECTABLE if blocked else None,
                failure_code=failure_code if blocked else None,
                failure_summary=(
                    f"dispatch intent {intent_id!r} was still CLAIMED after 3600s"
                    if blocked
                    else None
                ),
            ),
        )
    repo.record_fact(
        work_unit_id,
        WorkUnitTransition(status=WorkUnitStatus.BLOCKED, current_phase=phase),
    )
    return work_unit_id, milestone.stable_key


def test_adopting_the_settled_dispatch_credits_the_milestone_once(
    tmp_path: Path, runtime: AppRuntime
) -> None:
    row = _dispatch(tmp_path, runtime, REPORT, target_project_id=_settled_plan().target_project_id)
    intent_id = row["intent_id"]
    work_unit_id, milestone_key = _wait_elapsed_milestone(intent_id, phase=LifecyclePhase.PLAN)

    first = dispatch_adoption.adopt_settled_dispatch(work_unit_id, milestone_key)
    replay = dispatch_adoption.adopt_settled_dispatch(work_unit_id, milestone_key)

    milestone = next(
        item
        for item in repo.list_milestone_executions(work_unit_id)
        if item.stable_key == milestone_key
    )
    implementation_plan = next(
        artifact
        for artifact in repo.list_work_unit_artifacts(work_unit_id)
        if artifact.artifact_type.value == "implementation_plan"
    )
    assert first.applied is True
    assert first.attempt == 2
    assert first.intent_id == intent_id
    assert replay.applied is False
    assert replay.intent_id == intent_id
    assert milestone.status is MilestoneExecutionStatus.SUCCEEDED
    assert milestone.attempt == 2
    assert implementation_plan.metadata["dispatch_intent_id"] == intent_id
    assert implementation_plan.metadata["implementation_plan"]["report"]["plan_markdown"] == PLAN


@pytest.mark.parametrize("promotion", [None, "RESULT_RECORDED", "MERGE_PENDING"])
def test_source_patch_cannot_be_adopted_before_recorded_integration(promotion) -> None:
    payload = json.loads(_runner_result())
    if promotion is not None:
        payload["promotion_state"] = promotion
    intent_id = _settled_intent(result=json.dumps(payload))
    work_unit_id, milestone_key = _wait_elapsed_milestone(intent_id)
    before = tuple(repo.list_work_unit_events(work_unit_id))
    with pytest.raises(dispatch_adoption.DispatchAdoptionRefused) as refusal:
        dispatch_adoption.adopt_settled_dispatch(work_unit_id, milestone_key)
    assert refusal.value.code == "settled_adoption_requires_integrated_row"
    assert tuple(repo.list_work_unit_events(work_unit_id)) == before
    assert not repo.list_work_unit_artifacts(work_unit_id)


@pytest.mark.parametrize(
    "fields",
    [
        {"result_state": "FAILED"},
        {"result_state": "not-a-result-state"},
        {"promotion_state": "MERGED"},
    ],
)
def test_done_row_cannot_override_invalid_or_failed_runner_evidence(fields) -> None:
    payload = json.loads(_runner_result())
    payload.update(fields)
    intent_id = _legacy_settled_intent(result=json.dumps(payload))
    work_unit_id, milestone_key = _wait_elapsed_milestone(intent_id, phase=LifecyclePhase.PLAN)
    before = tuple(repo.list_work_unit_events(work_unit_id))
    with pytest.raises(dispatch_adoption.DispatchAdoptionRefused) as refusal:
        dispatch_adoption.adopt_settled_dispatch(work_unit_id, milestone_key)
    assert refusal.value.code == DispatchIngressFailureCode.REPORT_CONTRACT_VIOLATION
    assert tuple(repo.list_work_unit_events(work_unit_id)) == before
    assert not repo.list_work_unit_artifacts(work_unit_id)
    diagnostics = [
        event
        for event in list_ledger_events()["events"]
        if event["event_type"] == DISPATCH_CONTRACT_VIOLATION_EVENT
    ]
    assert len(diagnostics) == 1
    assert diagnostics[0]["aggregate_id"] == intent_id
    assert diagnostics[0]["event_id"] in str(refusal.value)
    with pytest.raises(dispatch_adoption.DispatchAdoptionRefused):
        dispatch_adoption.adopt_settled_dispatch(work_unit_id, milestone_key)
    assert (
        len(
            [
                event
                for event in list_ledger_events()["events"]
                if event["event_type"] == DISPATCH_CONTRACT_VIOLATION_EVENT
            ]
        )
        == 1
    )


def test_adoption_refuses_a_dispatch_that_is_still_running() -> None:
    intent_id = _settled_intent(status="CLAIMED")
    work_unit_id, milestone_key = _wait_elapsed_milestone(intent_id)

    with pytest.raises(
        dispatch_adoption.DispatchAdoptionRefused,
        match="wait for it to settle",
    ) as refusal:
        dispatch_adoption.adopt_settled_dispatch(work_unit_id, milestone_key)
    assert refusal.value.code == "settled_adoption_dispatch_still_active"


def test_adoption_refuses_a_dispatch_that_settled_failed() -> None:
    intent_id = _settled_intent(status="FAILED")
    work_unit_id, milestone_key = _wait_elapsed_milestone(intent_id)

    with pytest.raises(
        dispatch_adoption.DispatchAdoptionRefused,
        match="the normal retry path owns settled failures",
    ) as refusal:
        dispatch_adoption.adopt_settled_dispatch(work_unit_id, milestone_key)
    assert refusal.value.code == "settled_adoption_dispatch_not_done"


def test_adoption_refuses_any_other_block_reason() -> None:
    intent_id = _settled_intent()
    work_unit_id, milestone_key = _wait_elapsed_milestone(intent_id, failure_code="USAGE_LIMIT")

    with pytest.raises(
        dispatch_adoption.DispatchAdoptionRefused,
        match="only a milestone blocked by an exhausted dispatch wait",
    ) as refusal:
        dispatch_adoption.adopt_settled_dispatch(work_unit_id, milestone_key)
    assert refusal.value.code == "settled_adoption_not_wait_elapsed"


def test_adoption_refuses_a_result_it_cannot_check() -> None:
    intent_id = _legacy_settled_intent(result="completed by hand, trust me")
    work_unit_id, milestone_key = _wait_elapsed_milestone(intent_id)

    with pytest.raises(
        dispatch_adoption.DispatchAdoptionRefused,
        match="does not carry adoptable evidence",
    ) as refusal:
        dispatch_adoption.adopt_settled_dispatch(work_unit_id, milestone_key)
    assert refusal.value.code == DispatchIngressFailureCode.REPORT_CONTRACT_VIOLATION


def test_adoption_preserves_the_distinction_between_absent_and_corrupt_evidence() -> None:
    intent_id = _legacy_settled_intent(result=None)
    work_unit_id, milestone_key = _wait_elapsed_milestone(intent_id)
    with pytest.raises(dispatch_adoption.DispatchAdoptionRefused) as refusal:
        dispatch_adoption.adopt_settled_dispatch(work_unit_id, milestone_key)
    assert refusal.value.code == "unverifiable_dispatch_result"
    assert not [
        event
        for event in list_ledger_events()["events"]
        if event["event_type"] == DISPATCH_CONTRACT_VIOLATION_EVENT
    ]


def test_adoption_refuses_evidence_missing_a_required_artifact() -> None:
    intent_id = _settled_intent(result=_runner_result(changed_files=()))
    work_unit_id, milestone_key = _wait_elapsed_milestone(intent_id)

    with pytest.raises(
        dispatch_adoption.DispatchAdoptionRefused,
        match="does not carry adoptable evidence",
    ) as refusal:
        dispatch_adoption.adopt_settled_dispatch(work_unit_id, milestone_key)
    assert refusal.value.code == "missing_required_artifacts"
