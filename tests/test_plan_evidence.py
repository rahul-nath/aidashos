# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real dispatcher persistence and settlement, with an explicit model-output fixture."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from test_dispatch_backed_runtime import _context
from work_unit_support import write_test_project_registry

from local_first_agent_os.coordination import DispatchKind
from local_first_agent_os.coordination.dispatch import submit_dispatch_intent
from local_first_agent_os.coordination.execution import (
    complete_execution_lease,
    open_execution_lease,
)
from local_first_agent_os.coordination.store import rowdict, tx
from local_first_agent_os.dispatcher import Dispatched, LedgerDispatcher
from local_first_agent_os.dispatcher_runner import DispatcherIntentRunner
from local_first_agent_os.ids import sha256_text
from local_first_agent_os.pow_wow import PowWowArtifact, PowWowRunResult, PowWowTaskResult
from local_first_agent_os.runtime import AppRuntime
from local_first_agent_os.work_units.execution import (
    DispatchBackedExecutorRuntime,
    MilestoneFailed,
    MilestoneSucceeded,
)
from local_first_agent_os.work_units.plan_evidence import (
    PLAN_REPORT_INSTRUCTION,
    PlanEvidenceCause,
    PlanEvidenceUnavailable,
    observe_local_plan_source,
    parse_plan_report,
)
from local_first_agent_os.work_units.retry import ChargedFailure, UnchargedFailure, attempt_charge

PLAN = (
    "Inspect label_index.py, replace sorting with stable first-seen deduplication, "
    "and run the precommitted tests."
)
REPORT = json.dumps(
    {"schema_version": "plan_result.v1", "status": "PLANNED", "plan_markdown": PLAN}
)
GENERIC_SUMMARY = (
    "CLI executor ran 2 agent task(s); status=COMPLETED; auto-merge remained disabled."
)


def _dispatch(
    tmp_path: Path, runtime: AppRuntime, output: str, *, target_project_id: str = "target"
) -> dict:
    target = tmp_path / "plan-target"
    target.mkdir()
    subprocess.run(["git", "init", str(target)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "fixture source",
        ],
        check=True,
        capture_output=True,
    )
    revision = observe_local_plan_source(target)
    write_test_project_registry(runtime.settings.config_dir, target_project_id, target)
    runtime.settings.coordination_root = tmp_path / "coord"
    runtime.settings.saga_worktree_root = tmp_path / "worktrees"

    class FixtureExecutor:
        def dispatch_pow_wow(self, pow_wow_id, target_project, tasks, context):
            results = []
            for task in tasks:
                task_id = context.task_ids_by_name[task.task_name]
                opened = open_execution_lease(
                    task_id,
                    "fixture-worker",
                    intent_id=context.dispatch_intent_id,
                    task_id=task_id,
                    target_project_id=target_project.id,
                    source_revision=revision,
                )
                assert opened["ok"], opened
                lease_id = opened["lease"]["lease_id"]
                assert complete_execution_lease(lease_id, "COMPLETED")["ok"]
                report_output = output if task.blocked_by else REPORT
                results.append(
                    PowWowTaskResult(
                        task_name=task.task_name,
                        role=task.role,
                        status="completed",
                        summary="fixture execution completed",
                        artifacts=(
                            PowWowArtifact(
                                artifact_type="cli_agent_run",
                                schema_version="cli_agent_run.v1",
                                task_name=task.task_name,
                                content={
                                    "schema_version": "cli_agent_run.v1",
                                    "task": task.to_payload(),
                                    "target_project_id": target_project.id,
                                    "execution_lease": {"lease_id": lease_id, "task_id": task_id},
                                    "output": report_output,
                                },
                            ),
                        ),
                    )
                )
            return PowWowRunResult(
                executor="explicit-model-output-fixture",
                mode="cli",
                pow_wow_id=pow_wow_id,
                target_project_id=target_project.id,
                target_project_path=str(target),
                status="COMPLETED",
                output_summary=GENERIC_SUMMARY,
                tasks=tuple(results),
            )

    submitted = submit_dispatch_intent(
        "senior", PLAN_REPORT_INSTRUCTION, kind="advisory", target_project_id=target_project_id
    )
    assert submitted["ok"], submitted
    runner = DispatcherIntentRunner(
        runtime,
        executor_factory=lambda _bench, _ceiling: FixtureExecutor(),  # type: ignore[arg-type]
    )
    dispatched = LedgerDispatcher(runner, settings=runtime.settings).poll_once()
    assert isinstance(dispatched, Dispatched) and dispatched.status == "DONE", dispatched
    with tx() as connection:
        return rowdict(
            connection.execute(
                "SELECT * FROM dispatch_intents WHERE intent_id = ?", (submitted["intent_id"],)
            ).fetchone()
        )


def _settle(row: dict):
    return DispatchBackedExecutorRuntime(
        kind=DispatchKind.ADVISORY, target_project_id="target"
    )._outcome_from_settled_row(_context(), row["intent_id"], row)


def test_actual_dispatch_payload_retains_plan_not_generic_summary(
    tmp_path: Path, runtime: AppRuntime
) -> None:
    row = _dispatch(tmp_path, runtime, REPORT)
    outcome = _settle(row)
    assert isinstance(outcome, MilestoneSucceeded), outcome
    (artifact,) = outcome.artifacts
    evidence = artifact.metadata["implementation_plan"]
    assert evidence["report"]["plan_markdown"] == PLAN
    assert evidence["report_sha256"] == sha256_text(REPORT)
    assert evidence["plan_sha256"] == sha256_text(PLAN)
    assert artifact.content_hash == sha256_text(json.dumps(evidence, sort_keys=True))
    assert artifact.content_hash != sha256_text(GENERIC_SUMMARY)
    assert evidence["dispatch_intent_id"] == row["intent_id"]
    with tx() as connection:
        retained = connection.execute(
            "SELECT task_id,content FROM task_artifacts WHERE artifact_id = ?",
            (evidence["source_artifact_id"],),
        ).fetchone()
        lease = connection.execute(
            "SELECT task_id,intent_id,source_revision FROM agent_execution_leases "
            "WHERE lease_id = ?",
            (evidence["origin"]["lease_id"],),
        ).fetchone()
    assert retained["task_id"] == evidence["task_id"] == lease["task_id"]
    assert lease["intent_id"] == row["intent_id"]
    assert lease["source_revision"] == evidence["source_revision"]
    assert json.loads(retained["content"])["content"]["output"] == REPORT


@pytest.mark.parametrize(
    "output",
    [
        GENERIC_SUMMARY,
        "CANNOT_REVIEW: source repository was unavailable",
        json.dumps(
            {
                "schema_version": "plan_result.v1",
                "status": "UNAVAILABLE",
                "reason": "source unavailable",
            }
        ),
        json.dumps({"schema_version": "plan_result.v1", "status": "PLANNED", "plan_markdown": " "}),
    ],
)
def test_completed_runner_and_valid_intermediate_plan_cannot_credit_missing_final_plan(
    tmp_path: Path, runtime: AppRuntime, output: str
) -> None:
    outcome = _settle(_dispatch(tmp_path, runtime, output))
    assert isinstance(outcome, MilestoneFailed)
    unavailable = '"status": "UNAVAILABLE"' in output
    assert outcome.failure_code == (
        PlanEvidenceCause.REPORT_UNAVAILABLE if unavailable else PlanEvidenceCause.REPORT_INVALID
    )
    assert isinstance(
        attempt_charge(outcome.failure_class), UnchargedFailure if unavailable else ChargedFailure
    )
    assert outcome.artifacts == ()


@pytest.mark.parametrize(
    "damage",
    [
        "unretained_output",
        "missing_capture",
        "wrong_lease_target",
        "missing_source",
        "malformed_host_capture",
    ],
)
def test_plan_requires_exact_retained_capture_and_execution_subject(
    tmp_path: Path, runtime: AppRuntime, damage: str
) -> None:
    row = _dispatch(tmp_path, runtime, REPORT)
    payload = json.loads(row["result"])
    capture = payload["run_result"]["tasks"][-1]["artifacts"][0]["content"]
    lease_id = capture["execution_lease"]["lease_id"]
    task_id = capture["execution_lease"]["task_id"]
    with tx() as connection:
        if damage == "unretained_output":
            capture["output"] = REPORT.replace("stable", "invented")
            row["result"] = json.dumps(payload)
        elif damage == "missing_capture":
            connection.execute("DELETE FROM task_artifacts WHERE task_id = ?", (task_id,))
        elif damage == "wrong_lease_target":
            connection.execute(
                "UPDATE agent_execution_leases SET target_project_id = 'another' "
                "WHERE lease_id = ?",
                (lease_id,),
            )
        elif damage == "missing_source":
            connection.execute(
                "UPDATE agent_execution_leases SET source_revision = NULL WHERE lease_id = ?",
                (lease_id,),
            )
        else:
            connection.execute(
                "UPDATE task_artifacts SET content = 'invalid JSON' WHERE task_id = ?",
                (task_id,),
            )
    outcome = _settle(row)
    assert isinstance(outcome, MilestoneFailed)
    assert outcome.failure_code == PlanEvidenceCause.HOST_EVIDENCE_UNAVAILABLE
    assert isinstance(attempt_charge(outcome.failure_class), UnchargedFailure)


def test_duplicate_plan_status_is_refused() -> None:
    with pytest.raises(PlanEvidenceUnavailable, match="duplicate"):
        parse_plan_report(
            '{"schema_version":"plan_result.v1","status":"UNAVAILABLE","status":"PLANNED","plan_markdown":"invented"}'
        )


def test_plan_milestone_requests_produced_report_contract() -> None:
    assert PLAN_REPORT_INSTRUCTION in DispatchBackedExecutorRuntime()._prompt(_context())
