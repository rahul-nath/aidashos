# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Typed host observations keep retry blame through the durable completion owner."""

from __future__ import annotations

import json

import pytest
from test_worktree_loss import _lost_worktree_runs
from work_unit_support import compile_acceptance_doc, start_inline

from local_first_agent_os.coordination.dispatch import (
    claim_next_dispatch_intent,
    complete_dispatch_intent,
    submit_dispatch_intent,
)
from local_first_agent_os.coordination.failures import expected_failure
from local_first_agent_os.coordination.outcomes import TerminalOutcome
from local_first_agent_os.coordination.store import rowdict, tx
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units import service
from local_first_agent_os.work_units.execution import (
    DispatchBackedExecutorRuntime,
    _failure_class_for_outcome,
)
from local_first_agent_os.work_units.lifecycle import FailureClass, MilestoneExecutionStatus
from local_first_agent_os.work_units.retry import (
    ChargedFailure,
    attempt_charge,
    count_charged_failures,
)
from local_first_agent_os.work_units.root_workflow import (
    EnqueueDelivery,
    WorkUnitEngine,
    set_engine,
)


def _task(outcome: TerminalOutcome, *, status: str = "failed") -> dict:
    return {
        "task_name": "failure-owner",
        "role": "implementer",
        "status": status,
        "failure": expected_failure(
            outcome, operation="test_worker", message="unchanged original diagnostic"
        ).to_dict(),
    }


def _complete(tasks: list[dict], *, different_subject: bool = False) -> tuple[dict, dict]:
    submitted = submit_dispatch_intent(
        "senior",
        "exercise typed failure admission",
        kind="code",
        target_project_id="local-first-agent-os",
    )
    assert submitted["ok"]
    intent_id = submitted["intent_id"]
    claimed = claim_next_dispatch_intent("failure-admission-test")
    assert claimed["intent"]["intent_id"] == intent_id
    result = json.dumps(
        {
            "schema_version": "dispatch_runner_result.v1",
            "intent_id": "other-intent" if different_subject else intent_id,
            "target_project_id": "local-first-agent-os",
            "result_origin": "AUTOMATED",
            "result_state": "FAILED",
            "promotion_state": "RESULT_RECORDED",
            "run_result": {"status": "FAILED", "tasks": tasks},
        }
    )
    completed = complete_dispatch_intent(
        intent_id,
        "FAILED",
        result=result,
        error="unchanged original diagnostic",
    )
    with tx() as connection:
        row = rowdict(
            connection.execute(
                "SELECT * FROM dispatch_intents WHERE intent_id=?", (intent_id,)
            ).fetchone()
        )
    return completed, row


@pytest.mark.parametrize(
    "outcome", [TerminalOutcome.EXECUTION_ENVIRONMENT_LOST, TerminalOutcome.LOCAL_MODEL_NOT_LOADED]
)
def test_completion_preserves_typed_cause_and_original_diagnostic(outcome) -> None:
    completed, row = _complete([_task(outcome), _task(outcome, status="blocked")])
    assert completed["ok"]
    assert row["outcome"] == outcome.value
    assert row["error"] == "unchanged original diagnostic"


@pytest.mark.parametrize(
    "other", [TerminalOutcome.VERIFICATION_FAILED, TerminalOutcome.UNKNOWN_FAILURE]
)
def test_environment_failure_cannot_erase_a_separate_failed_judgment(other) -> None:
    completed, row = _complete([_task(TerminalOutcome.LOCAL_MODEL_NOT_LOADED), _task(other)])
    assert completed["ok"]
    assert row["outcome"] == TerminalOutcome.UNKNOWN_FAILURE.value
    assert isinstance(attempt_charge(_failure_class_for_outcome(row["outcome"])), ChargedFailure)


@pytest.mark.parametrize(
    "mutation",
    ["missing_failure", "unknown_code", "mismatched_code", "wrong_category", "successful_task"],
)
def test_unrecognized_or_inconsistent_task_failure_does_not_exempt_work(mutation) -> None:
    task = _task(TerminalOutcome.LOCAL_MODEL_NOT_LOADED)
    match mutation:
        case "missing_failure":
            del task["failure"]
        case "unknown_code":
            task["failure"]["terminal_outcome"] = "FUTURE_UNQUALIFIED_OUTCOME"
        case "mismatched_code":
            task["failure"]["error_code"] = TerminalOutcome.VERIFICATION_FAILED.value
        case "wrong_category":
            task["failure"]["category"] = "BUSINESS"
        case "successful_task":
            task["status"] = "completed"
    completed, row = _complete([task])
    assert completed["ok"]
    assert row["outcome"] == TerminalOutcome.UNKNOWN_FAILURE.value
    assert isinstance(attempt_charge(_failure_class_for_outcome(row["outcome"])), ChargedFailure)


def test_failure_from_another_dispatch_cannot_be_admitted() -> None:
    completed, row = _complete(
        [_task(TerminalOutcome.EXECUTION_ENVIRONMENT_LOST)], different_subject=True
    )
    assert not completed["ok"]
    assert row["status"] == "CLAIMED"
    assert row["outcome"] is None


def test_real_worktree_loss_retries_once_then_parks_with_both_receipts(
    tmp_path, monkeypatch, work_unit_ledger
) -> None:
    runs = iter(_lost_worktree_runs(tmp_path, monkeypatch))
    dispatches: list[str] = []

    def settle(*args, **kwargs):
        submitted = submit_dispatch_intent(*args, **kwargs)
        intent_id = submitted["intent_id"]
        claimed = claim_next_dispatch_intent("worktree-loss-test")
        assert claimed["intent"]["intent_id"] == intent_id
        run = next(runs)
        failure = run.tasks[0].failure
        assert failure is not None
        dispatches.append(intent_id)
        report = json.dumps(
            {
                "schema_version": "dispatch_runner_result.v1",
                "intent_id": intent_id,
                "target_project_id": claimed["intent"]["target_project_id"],
                "result_origin": "AUTOMATED",
                "result_state": "FAILED",
                "promotion_state": "RESULT_RECORDED",
                "run_result": {"status": "FAILED", "tasks": [run.tasks[0].to_payload()]},
            }
        )
        completed = complete_dispatch_intent(
            intent_id,
            "FAILED",
            result=report,
            error="; ".join(run.risks) or run.output_summary,
        )
        assert completed["ok"]
        return submitted

    compiled = compile_acceptance_doc(design_doc_id="real-worktree-loss")
    assert compiled.compiled_plan_revision_id is not None
    plan = repo.get_compiled_plan_revision(compiled.compiled_plan_revision_id).plan
    set_engine(
        WorkUnitEngine(
            runtime=DispatchBackedExecutorRuntime(
                intent_submitter=settle,
                target_project_id=plan.target_project_id,
                poll_interval_seconds=0,
            )
        )
    )
    started = start_inline(compiled.compiled_plan_revision_id)
    work_unit_id = started["work_unit_id"]
    assert isinstance(work_unit_id, str)

    def milestone():
        return next(
            row for row in repo.list_milestone_executions(work_unit_id) if row.stable_key == "a"
        )

    assert milestone().status is MilestoneExecutionStatus.BLOCKED
    assert milestone().failure_class is FailureClass.TRANSIENT
    first = repo.list_milestone_failure_attempts(work_unit_id)
    resumed = service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.INLINE)
    assert not resumed["exhausted"]
    repeated = milestone()
    assert repeated.status is MilestoneExecutionStatus.WAITING_FOR_OPERATOR
    assert repeated.failure_class is FailureClass.REQUIRES_OPERATOR
    assert repeated.failure_summary is not None
    assert "attempts 1 and 2" in repeated.failure_summary
    assert "Allocated worktree was lost before cleanup" in repeated.failure_summary
    failures = repo.list_milestone_failure_attempts(work_unit_id)
    assert failures[:-1] == first
    assert len(failures) == len(dispatches) == 2
    assert count_charged_failures(f.failure_class for f in failures) == 0
    artifacts = repo.list_work_unit_artifacts(work_unit_id)
    assert len(artifacts) == 2
