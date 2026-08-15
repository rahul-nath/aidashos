# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A wait-elapsed milestone may adopt its own dispatch once that settled DONE.

`dispatch_wait_elapsed` parks the milestone while the dispatch keeps running.
When the dispatch later settles DONE, its evidence is complete and checkable
but no lifecycle state can reach it: a resume mints a rival attempt, so work
that reliably outlives its compiled bound is re-spent forever. These tests pin
the narrow repair: only that block reason, only the milestone's own intent,
only a DONE settlement, and only evidence the normal translation accepts.
"""

from __future__ import annotations

import json

import pytest
from work_unit_support import compile_acceptance_doc

from local_first_agent_os.coordination.dispatch import (
    claim_next_dispatch_intent,
    complete_dispatch_intent,
    submit_dispatch_intent,
)
from local_first_agent_os.work_units import dispatch_adoption
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.events import MilestoneTransition, WorkUnitTransition
from local_first_agent_os.work_units.lifecycle import (
    FailureClass,
    LifecyclePhase,
    MilestoneExecutionStatus,
    WorkUnitStatus,
)


def _runner_result(*, changed_files: tuple[str, ...] = ("feature.py",)) -> str:
    return json.dumps(
        {
            "schema_version": "dispatch_runner_result.v1",
            "intent_id": "unused-by-translation",
            "run_result": {
                "status": "COMPLETED",
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
        target_project_id="target",
    )
    intent_id = str(submitted["intent_id"])
    claim_next_dispatch_intent("test-worker", "senior")
    if status != "CLAIMED":
        complete_dispatch_intent(
            intent_id,
            status,
            result=_runner_result() if result is None else result,
            error=None if status == "DONE" else "senior turn failed",
        )
    return intent_id


def _wait_elapsed_milestone(
    intent_id: str, *, failure_code: str = "dispatch_wait_elapsed"
) -> tuple[str, str]:
    compiled = compile_acceptance_doc(design_doc_id="settled_adoption")
    assert compiled.compiled_plan_revision_id is not None
    started = repo.start_work_unit(compiled.compiled_plan_revision_id)
    work_unit_id = started.work_unit.work_unit_id
    milestone = next(
        item
        for item in repo.list_milestone_executions(work_unit_id)
        if item.phase is LifecyclePhase.IMPLEMENT
    )
    repo.record_fact(
        work_unit_id,
        WorkUnitTransition(status=WorkUnitStatus.RUNNING, current_phase=LifecyclePhase.IMPLEMENT),
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
                phase=LifecyclePhase.IMPLEMENT,
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
        WorkUnitTransition(status=WorkUnitStatus.BLOCKED, current_phase=LifecyclePhase.IMPLEMENT),
    )
    return work_unit_id, milestone.stable_key


def test_adopting_the_settled_dispatch_credits_the_milestone_once() -> None:
    intent_id = _settled_intent()
    work_unit_id, milestone_key = _wait_elapsed_milestone(intent_id)

    first = dispatch_adoption.adopt_settled_dispatch(work_unit_id, milestone_key)
    replay = dispatch_adoption.adopt_settled_dispatch(work_unit_id, milestone_key)

    milestone = next(
        item
        for item in repo.list_milestone_executions(work_unit_id)
        if item.stable_key == milestone_key
    )
    source_patch = next(
        artifact
        for artifact in repo.list_work_unit_artifacts(work_unit_id)
        if artifact.artifact_type.value == "source_patch"
    )
    assert first.applied is True
    assert first.attempt == 2
    assert first.intent_id == intent_id
    assert replay.applied is False
    assert replay.intent_id == intent_id
    assert milestone.status is MilestoneExecutionStatus.SUCCEEDED
    assert milestone.attempt == 2
    assert source_patch.metadata["dispatch_intent_id"] == intent_id


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
        match="only a milestone blocked by dispatch_wait_elapsed",
    ) as refusal:
        dispatch_adoption.adopt_settled_dispatch(work_unit_id, milestone_key)
    assert refusal.value.code == "settled_adoption_not_wait_elapsed"


def test_adoption_refuses_a_result_it_cannot_check() -> None:
    intent_id = _settled_intent(result="completed by hand, trust me")
    work_unit_id, milestone_key = _wait_elapsed_milestone(intent_id)

    with pytest.raises(
        dispatch_adoption.DispatchAdoptionRefused,
        match="does not carry adoptable evidence",
    ) as refusal:
        dispatch_adoption.adopt_settled_dispatch(work_unit_id, milestone_key)
    assert refusal.value.code == "unverifiable_dispatch_result"


def test_adoption_refuses_evidence_missing_a_required_artifact() -> None:
    intent_id = _settled_intent(result=_runner_result(changed_files=()))
    work_unit_id, milestone_key = _wait_elapsed_milestone(intent_id)

    with pytest.raises(
        dispatch_adoption.DispatchAdoptionRefused,
        match="does not carry adoptable evidence",
    ) as refusal:
        dispatch_adoption.adopt_settled_dispatch(work_unit_id, milestone_key)
    assert refusal.value.code == "missing_required_artifacts"
