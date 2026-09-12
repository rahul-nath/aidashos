# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Local readiness retains its typed cause through delegation and retry accounting."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest
from work_unit_support import compile_acceptance_doc, start_inline

from local_first_agent_os.contracts import ModelRole
from local_first_agent_os.coordination import DispatchKind
from local_first_agent_os.coordination.dispatch import (
    claim_next_dispatch_intent,
    complete_dispatch_intent,
    submit_dispatch_intent,
)
from local_first_agent_os.coordination.outcomes import TerminalOutcome
from local_first_agent_os.local_delegate import build_resident_local_delegate
from local_first_agent_os.model_manager import ModelNotLoadedError
from local_first_agent_os.pow_wow import (
    CliPowWowExecutor,
    PowWowExecutionContext,
    PowWowTaskSpec,
)
from local_first_agent_os.pow_wow.types import PowWowTaskResult
from local_first_agent_os.project_access import AccessMode, ProjectAccessPolicy
from local_first_agent_os.project_center import LinkedProject
from local_first_agent_os.runtime import AppRuntime
from local_first_agent_os.staffing import BenchSlot, Harness, JudgmentRole
from local_first_agent_os.vocabulary import DispatchTier
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units import service
from local_first_agent_os.work_units.auto_resume import sweep_transient_blocked
from local_first_agent_os.work_units.execution import (
    DispatchBackedExecutorRuntime,
    _failure_class_for_outcome,
)
from local_first_agent_os.work_units.lifecycle import FailureClass, MilestoneExecutionStatus
from local_first_agent_os.work_units.retry import ChargedFailureBudget, count_charged_failures
from local_first_agent_os.work_units.root_workflow import (
    EnqueueDelivery,
    WorkUnitEngine,
    set_engine,
)


def _refused_local_task(
    runtime: AppRuntime, monkeypatch: pytest.MonkeyPatch, path: Path
) -> PowWowTaskResult:
    def unavailable(role: ModelRole, **_kwargs: Any) -> None:
        raise ModelNotLoadedError(role)

    def inference_must_not_start(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("an unavailable model must not start an inference")

    monkeypatch.setattr(runtime.model_manager, "ensure_loaded", unavailable)
    monkeypatch.setattr(runtime.model_manager, "_mock_text_for", inference_must_not_start)
    target = LinkedProject(
        id="local-first-agent-os",
        kind="test",
        path=path,
        status="active_product_repo",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="local readiness boundary",
    )
    executor = CliPowWowExecutor(
        worktree_root=path / "worktrees",
        bench={DispatchTier.JUNIOR: BenchSlot(harness=Harness.PI, model="gemma4", capacity=1)},
        delegate_fn=build_resident_local_delegate(runtime),
    )
    return executor._run_scheduled_task(
        pow_wow_id="local-readiness",
        target_project=target,
        task=PowWowTaskSpec(
            task_name="local_judgment",
            role="analyst",
            description="Inspect the requested design.",
            judgment=JudgmentRole(name="analyst", tier=DispatchTier.JUNIOR),
            dispatch_kind=DispatchKind.ADVISORY,
        ),
        context=PowWowExecutionContext(
            saga_id="local-readiness",
            goal="Inspect the requested design.",
            directive="/pow-wow",
            target_project_id=target.id,
            target_project_path=str(path),
            target_project_kind=target.kind,
            target_project_status=target.status,
            target_project_read_only=False,
        ),
        dependency_results=(),
        candidate_results=(),
        code_worktrees={},
        code_worktree_lock=threading.Lock(),
    )


def test_real_local_readiness_exception_keeps_its_type_through_the_resident_delegate(
    runtime: AppRuntime, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    task = _refused_local_task(runtime, monkeypatch, tmp_path)
    assert task.status == "failed"
    assert task.failure is not None
    assert task.failure.error_code == TerminalOutcome.LOCAL_MODEL_NOT_LOADED
    assert task.failure.terminal_outcome == TerminalOutcome.LOCAL_MODEL_NOT_LOADED
    assert task.artifacts[0].content["attempts"] == 1
    assert task.artifacts[0].content["tokens_used"] == 0


def test_local_readiness_is_parked_instead_of_automatically_retried() -> None:
    assert _failure_class_for_outcome(TerminalOutcome.LOCAL_MODEL_NOT_LOADED.value) is (
        FailureClass.SCHEDULING
    )


def test_unavailable_local_model_does_not_spend_the_work_budget(
    runtime: AppRuntime,
    work_unit_ledger: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    task = _refused_local_task(runtime, monkeypatch, tmp_path)
    assert task.failure is not None
    readiness_message = task.failure.message
    judged_failure = False
    dispatches: list[str] = []

    def settle_dispatch(*args: Any, **kwargs: Any) -> dict[str, Any]:
        submitted = submit_dispatch_intent(*args, **kwargs)
        intent_id = str(submitted["intent_id"])
        claimed = claim_next_dispatch_intent("readiness-test")
        assert claimed["intent"]["intent_id"] == intent_id
        dispatches.append(intent_id)
        report = (
            None
            if judged_failure
            else json.dumps(
                {
                    "schema_version": "dispatch_runner_result.v1",
                    "intent_id": intent_id,
                    "target_project_id": str(claimed["intent"]["target_project_id"]),
                    "result_origin": "AUTOMATED",
                    "result_state": "FAILED",
                    "promotion_state": "RESULT_RECORDED",
                    "run_result": {"status": "FAILED", "tasks": [task.to_payload()]},
                }
            )
        )
        completion = complete_dispatch_intent(
            intent_id,
            status="FAILED",
            result=report,
            error="verification failed" if judged_failure else readiness_message,
        )
        assert completion["ok"] is True
        return submitted

    compiled = compile_acceptance_doc(design_doc_id="local-model-readiness")
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
    unit = repo.get_work_unit(work_unit_id)
    policy = (
        repo.get_compiled_plan_revision(unit.compiled_plan_revision_id)
        .plan.milestone("a")
        .failure_policy.retry_policy
    )
    assert isinstance(policy, ChargedFailureBudget)

    for ordinal in range(1, policy.max_charged_failures + 2):
        milestone = next(
            row for row in repo.list_milestone_executions(work_unit_id) if row.stable_key == "a"
        )
        assert milestone.status is MilestoneExecutionStatus.BLOCKED
        assert milestone.attempt == ordinal
        assert milestone.failure_code == TerminalOutcome.LOCAL_MODEL_NOT_LOADED.value
        failures = repo.list_milestone_failure_attempts(work_unit_id)
        assert all(row.failure_class is FailureClass.SCHEDULING for row in failures)
        assert count_charged_failures(row.failure_class for row in failures) == 0
        assert sweep_transient_blocked() == ()
        assert len(dispatches) == ordinal
        if ordinal <= policy.max_charged_failures:
            resumed = service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.INLINE)
            assert not resumed["exhausted"]

    # Once judgment can run, its ordinary failures still exhaust the unchanged budget.
    judged_failure = True
    for _ in range(policy.max_charged_failures):
        resumed = service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.INLINE)
        assert not resumed["exhausted"]
    failures = repo.list_milestone_failure_attempts(work_unit_id)
    assert (
        count_charged_failures(row.failure_class for row in failures) == policy.max_charged_failures
    )
    refused = service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.INLINE)
    assert refused["exhausted"]
    assert len(dispatches) == 2 * policy.max_charged_failures + 1
