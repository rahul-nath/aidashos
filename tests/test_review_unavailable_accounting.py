# SPDX-License-Identifier: AGPL-3.0-or-later
"""A real wrapper refusal keeps its host cause through durable retry accounting."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_agent_execution_supervisor import _coord
from work_unit_support import compile_acceptance_doc, start_inline

from local_first_agent_os.agent_execution_supervisor import (
    StreamingCommandSupervisor,
    SupervisedCommandResult,
)
from local_first_agent_os.codex_review_failure import (
    inspection_failure_event,
    inspection_process_failure,
)
from local_first_agent_os.coordination import CompleteExecutionLease, parse_coordination_result
from local_first_agent_os.coordination.dispatch import (
    claim_next_dispatch_intent,
    complete_dispatch_intent,
    submit_dispatch_intent,
)
from local_first_agent_os.coordination.execution import (
    complete_execution_lease,
    open_execution_lease,
)
from local_first_agent_os.coordination.outcomes import (
    CheckpointReason,
    InfrastructureFailure,
    PersistenceStatus,
    SupervisorStatus,
    TerminalOutcome,
)
from local_first_agent_os.coordination.store import rowdict, tx
from local_first_agent_os.pow_wow import CliPowWowExecutor
from local_first_agent_os.pow_wow.executor import _harness_failure
from local_first_agent_os.pow_wow.types import ExecutionAttemptLease, PowWowTaskResult
from local_first_agent_os.runtime import AppRuntime
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units import service
from local_first_agent_os.work_units.auto_resume import sweep_transient_blocked
from local_first_agent_os.work_units.execution import DispatchBackedExecutorRuntime
from local_first_agent_os.work_units.lifecycle import FailureClass, MilestoneExecutionStatus
from local_first_agent_os.work_units.retry import ChargedFailureBudget, count_charged_failures
from local_first_agent_os.work_units.root_workflow import (
    EnqueueDelivery,
    WorkUnitEngine,
    set_engine,
)


def _command(request: Path) -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "local_first_agent_os.codex_review_launch",
        "--request",
        str(request),
        "--request-sha256",
        "0" * 64,
    )


def _coordinate(value: Any) -> Any:
    if isinstance(value, CompleteExecutionLease):
        payload = complete_execution_lease(
            value.lease_id,
            value.status,
            json.dumps(value.result),
            value.error,
        )
        assert payload["ok"], payload
        return parse_coordination_result(value, payload)
    return _coord(value)


@pytest.fixture
def observed_refusal(
    runtime: AppRuntime, work_unit_ledger: Path, tmp_path: Path
) -> tuple[SupervisedCommandResult, PowWowTaskResult]:
    request = tmp_path / "inspection-request.json"
    request.write_text("{}")
    command = _command(request)
    opened = open_execution_lease("inspection-refusal", "reviewer-test", timeout_seconds=30)
    lease_id = opened["lease"]["lease_id"]
    attempt = ExecutionAttemptLease(
        idempotency_key="inspection-refusal",
        worker_id="reviewer-test",
        lease_id=lease_id,
        created=True,
        open_status="ACTIVE",
    )

    result = asyncio.run(
        StreamingCommandSupervisor(
            coordination_command=_coordinate,
            artifact_writer=runtime.artifact_store,
        ).run(
            command,
            tmp_path,
            lease=attempt,
            harness="codex",
            timeout_seconds=20,
            env={
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                # A real stderr warning, unrelated to the wrapper refusal.
                "PYTHONWARNINGS": "not_a_warning_action",
            },
        )
    )
    assert result.capture.exit_code == 125
    assert "Invalid -W option" in result.capture.stderr
    assert "prepared inspection request identity changed" in result.capture.stdout
    assert result.agent_failure == TerminalOutcome.REVIEW_UNAVAILABLE
    assert result.persistence_status is PersistenceStatus.COMPLETED
    assert result.supervisor_status is SupervisorStatus.COMPLETED
    assert result.transcript_artifact_id is not None
    transcript = runtime.artifact_store.read_text(result.transcript_artifact_id)
    finished = [
        json.loads(line)["payload"]
        for line in transcript.splitlines()
        if json.loads(line)["kind"] == "agent.finished"
    ]
    assert finished[0]["failure"] == TerminalOutcome.REVIEW_UNAVAILABLE

    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "worktrees", coordination_command=_coordinate
    )
    executor._complete_execution_attempt_lease(
        attempt,
        capture=result.capture,
        dirty_worktree={},
        supervised_result=result,
    )
    assert attempt.complete_error is None
    with tx() as connection:
        retained = rowdict(
            connection.execute(
                "SELECT * FROM agent_execution_leases WHERE lease_id=?", (lease_id,)
            ).fetchone()
        )
    assert retained["outcome"] == TerminalOutcome.REVIEW_UNAVAILABLE
    assert retained["agent_failure"] == TerminalOutcome.REVIEW_UNAVAILABLE
    assert retained["agent_failure_category"] == "INFRASTRUCTURE"
    assert "prepared inspection request identity changed" in retained["error"]
    assert "Invalid -W option" not in retained["error"]
    classification = _harness_failure(
        result.capture,
        operation="run_advisory_agent",
        supervised_result=result,
        unknown_classifier=lambda _: pytest.fail("typed refusal must not invoke a classifier"),
    )
    stored = json.loads(retained["result_json"])
    assert stored["agent_failure_record"] == classification.failure.to_dict()
    task = PowWowTaskResult(
        task_name="staff_independent_reading",
        role="reviewer",
        status="failed",
        summary=classification.failure.message,
        failure=classification.failure,
    )
    return result, task


def test_cancelled_inspection_frame_does_not_prevent_lease_terminalization(
    observed_refusal: tuple[SupervisedCommandResult, PowWowTaskResult], tmp_path: Path
) -> None:
    original, _ = observed_refusal
    opened = open_execution_lease("inspection-cancel", "reviewer-test", timeout_seconds=30)
    attempt = ExecutionAttemptLease(
        idempotency_key="inspection-cancel",
        worker_id="reviewer-test",
        lease_id=opened["lease"]["lease_id"],
        created=True,
        open_status="ACTIVE",
    )
    capture = replace(
        original.capture,
        stdout=json.dumps(
            inspection_failure_event(TerminalOutcome.OPERATOR_CANCELED, "inspection cancelled")
        ),
        exit_code=130,
    )
    result = replace(
        original,
        capture=capture,
        cancel_requested=True,
        checkpoint_reason=CheckpointReason.OPERATOR_CANCEL,
        agent_failure=TerminalOutcome.OPERATOR_CANCELED.value,
        agent_failure_category=None,
    )
    executor = CliPowWowExecutor(worktree_root=tmp_path / "wt", coordination_command=_coordinate)
    executor._complete_execution_attempt_lease(
        attempt, capture=capture, dirty_worktree={}, supervised_result=result
    )
    assert attempt.complete_error is None
    assert attempt.complete_status == "CANCELED"
    with tx() as connection:
        row = rowdict(
            connection.execute(
                "SELECT * FROM agent_execution_leases WHERE lease_id=?", (attempt.lease_id,)
            ).fetchone()
        )
    assert row["outcome"] == "OPERATOR_CANCELED"
    assert row["agent_failure"] == "OPERATOR_CANCELED"
    assert row["agent_failure_category"] is None


def test_real_inspection_refusal_does_not_spend_the_judgment_budget(
    observed_refusal: tuple[SupervisedCommandResult, PowWowTaskResult],
) -> None:
    _, task = observed_refusal
    judged_failure = False

    def settle_dispatch(*args: Any, **kwargs: Any) -> dict[str, Any]:
        submitted = submit_dispatch_intent(*args, **kwargs)
        intent_id = submitted["intent_id"]
        claimed = claim_next_dispatch_intent("inspection-refusal-test")
        assert claimed["intent"]["intent_id"] == intent_id
        blocked_failure = CliPowWowExecutor._dependency_block_failure(
            [task], reason="final review needs the independent staff reading"
        )
        blocked = replace(
            task, task_name="final_staff_review", status="blocked", failure=blocked_failure
        )
        completed = complete_dispatch_intent(
            intent_id,
            "FAILED",
            result=(
                None
                if judged_failure
                else json.dumps(
                    {
                        "schema_version": "dispatch_runner_result.v1",
                        "intent_id": intent_id,
                        "target_project_id": claimed["intent"]["target_project_id"],
                        "result_origin": "AUTOMATED",
                        "result_state": "FAILED",
                        "promotion_state": "RESULT_RECORDED",
                        "run_result": {
                            "status": "FAILED",
                            "tasks": [task.to_payload(), blocked.to_payload()],
                        },
                    }
                )
            ),
            error="verification failed" if judged_failure else "irrelevant stderr warning",
        )
        assert completed["ok"], completed
        return submitted

    compiled = compile_acceptance_doc(design_doc_id="inspection-refusal-budget")
    assert compiled.compiled_plan_revision_id is not None
    plan = repo.get_compiled_plan_revision(compiled.compiled_plan_revision_id).plan
    set_engine(
        WorkUnitEngine(
            runtime=DispatchBackedExecutorRuntime(
                intent_submitter=settle_dispatch,
                target_project_id=plan.target_project_id,
                poll_interval_seconds=0.0,
            )
        )
    )
    started = start_inline(compiled.compiled_plan_revision_id)
    work_unit_id = str(started["work_unit_id"])
    milestone = next(
        row for row in repo.list_milestone_executions(work_unit_id) if row.stable_key == "a"
    )
    assert milestone.status is MilestoneExecutionStatus.BLOCKED
    assert milestone.failure_code == TerminalOutcome.REVIEW_UNAVAILABLE
    failures = repo.list_milestone_failure_attempts(work_unit_id)
    assert [row.failure_class for row in failures] == [FailureClass.SCHEDULING]
    assert count_charged_failures(row.failure_class for row in failures) == 0
    assert sweep_transient_blocked() == ()

    policy = plan.milestone("a").failure_policy.retry_policy
    assert isinstance(policy, ChargedFailureBudget)
    judged_failure = True
    for _ in range(policy.max_charged_failures):
        resumed = service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.INLINE)
        assert not resumed["exhausted"]
    failures = repo.list_milestone_failure_attempts(work_unit_id)
    assert (
        count_charged_failures(row.failure_class for row in failures) == policy.max_charged_failures
    )
    assert service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.INLINE)["exhausted"]


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"agent_failure": TerminalOutcome.VERIFICATION_FAILED.value}, "VERIFICATION_FAILED"),
        (
            {
                "persistence_status": PersistenceStatus.FAILED,
                "persistence_failure": InfrastructureFailure.ARTIFACT_WRITE_FAILED.value,
            },
            "ARTIFACT_WRITE_FAILED",
        ),
        (
            {"cancel_requested": True, "checkpoint_reason": CheckpointReason.OPERATOR_CANCEL},
            "OPERATOR_CANCELED",
        ),
        (
            {"deadline_reached": True, "checkpoint_reason": CheckpointReason.DEADLINE},
            "DEADLINE_EXCEEDED",
        ),
    ],
)
def test_inspection_frame_cannot_erase_other_failure_dimensions(
    observed_refusal: tuple[SupervisedCommandResult, PowWowTaskResult], changes, expected
) -> None:
    original, _ = observed_refusal
    result = replace(original, **changes)
    failure = _harness_failure(
        result.capture, operation="run_advisory_agent", supervised_result=result
    )
    assert failure.failure.error_code == expected


@pytest.mark.parametrize(
    "variant",
    [
        "model_text",
        "other_command",
        "success",
        "wrong_category",
        "conflicting_frames",
        "legacy_untyped",
        "stderr",
    ],
)
def test_only_structural_wrapper_failures_receive_the_typed_outcome(
    tmp_path: Path, variant: str
) -> None:
    frame = inspection_failure_event(TerminalOutcome.REVIEW_UNAVAILABLE, "fixture runtime refused")
    command = _command(tmp_path / "request")
    exit_code = 125
    stdout = json.dumps(frame)
    if variant == "model_text":
        stdout = json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": stdout}}
        )
    elif variant == "other_command":
        command = ("codex", "exec", "--json", "review")
    elif variant == "success":
        exit_code = 0
    elif variant == "wrong_category":
        frame["failure"]["category"] = "BUSINESS"
        stdout = json.dumps(frame)
    elif variant == "conflicting_frames":
        stdout += "\n" + json.dumps(
            inspection_failure_event(TerminalOutcome.UNKNOWN_FAILURE, "host bug")
        )
    elif variant == "legacy_untyped":
        del frame["failure"]
        stdout = json.dumps(frame)
    elif variant == "stderr":
        stdout = json.dumps({"text": stdout, "raw_sha256": "a" * 64})
    assert (
        inspection_process_failure(shlex.join(command), stdout=stdout, exit_code=exit_code) is None
    )


@pytest.mark.parametrize("different_worker", [False, True])
def test_lease_record_preserves_worker_and_persistence_dimensions(different_worker: bool) -> None:
    frame = inspection_failure_event(TerminalOutcome.REVIEW_UNAVAILABLE, "runtime unavailable")
    opened = open_execution_lease("inspection-dimensions", "reviewer-test", timeout_seconds=30)
    lease_id = opened["lease"]["lease_id"]
    result = complete_execution_lease(
        lease_id,
        "FAILED",
        json.dumps(
            {
                "agent_status": "FAILED",
                "agent_failure": "VERIFICATION_FAILED"
                if different_worker
                else "REVIEW_UNAVAILABLE",
                "agent_failure_category": "BUSINESS" if different_worker else "INFRASTRUCTURE",
                "agent_failure_record": frame["failure"],
                "supervisor_status": "COMPLETED",
                "persistence_status": "FAILED",
                "persistence_failure": "ARTIFACT_WRITE_FAILED",
            }
        ),
        error="artifact persistence failed",
    )
    if different_worker:
        assert not result["ok"]
        assert result["error"] == "invalid_agent_failure_record"
    else:
        assert result["ok"]
        assert result["lease"]["outcome"] == "REVIEW_UNAVAILABLE"
        assert result["lease"]["agent_failure"] == "REVIEW_UNAVAILABLE"
        assert result["lease"]["persistence_failure"] == "ARTIFACT_WRITE_FAILED"
