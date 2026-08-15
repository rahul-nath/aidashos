# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

from local_first_agent_os.coordination.contracts import (
    AcknowledgementResult,
    CollectionResult,
    CoordinationCommandName,
    LatestRepoAudit,
    LedgerRecord,
    SubmitArtifact,
)
from local_first_agent_os.decomposition import RuleBasedDecompositionPlanner
from local_first_agent_os.pow_wow import (
    CliPowWowExecutor,
    PowWowArtifact,
    PowWowExecutionContext,
    PowWowTaskResult,
)
from local_first_agent_os.pow_wow.planning import (
    persist_repo_audit,
    validate_planning_visibility_contract,
)
from local_first_agent_os.pow_wow.prompts import build_agent_task_prompt
from local_first_agent_os.pow_wow.protocol import PlanningPhase
from local_first_agent_os.pow_wow.repo_audit import RepoAudit
from local_first_agent_os.project_access import AccessMode, ProjectAccessPolicy
from local_first_agent_os.project_center import LinkedProject
from local_first_agent_os.staffing import Tier


def _target(path: Path) -> LinkedProject:
    path.mkdir(parents=True, exist_ok=True)
    return LinkedProject(
        id="target",
        kind="repo",
        path=path,
        status="active",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="test target",
        verification_commands=[],
    )


def _plan(target: LinkedProject):
    return RuleBasedDecompositionPlanner().plan(
        intent_id="planning-contract",
        tier=Tier.SENIOR,
        kind="code",
        prompt="implement the raw contract",
        target_project=target,
        intent={},
    )


def _context(target: LinkedProject, task_names: tuple[str, ...]) -> PowWowExecutionContext:
    return PowWowExecutionContext(
        saga_id="saga-planning",
        goal="the complete raw operator contract",
        directive="/saga test",
        target_project_id=target.id,
        target_project_path=str(target.expanded_path),
        target_project_kind=target.kind,
        target_project_status=target.status,
        target_project_read_only=target.read_only,
        task_ids_by_name={name: f"task-{index}" for index, name in enumerate(task_names)},
    )


def _run_git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _init_git_repo(path: Path) -> str:
    _run_git(path, "init", "-b", "main")
    _run_git(path, "config", "user.email", "test@example.com")
    _run_git(path, "config", "user.name", "Test User")
    (path / "stable.py").write_text("STABLE = True\n", encoding="utf-8")
    (path / "changed.py").write_text("VALUE = 1\n", encoding="utf-8")
    _run_git(path, "add", "stable.py", "changed.py")
    _run_git(path, "commit", "-m", "initial")
    return _run_git(path, "rev-parse", "HEAD")


def test_code_plan_has_typed_independent_visibility_graph(tmp_path: Path) -> None:
    plan = _plan(_target(tmp_path / "target"))

    validate_planning_visibility_contract(plan.tasks, required=True)
    by_phase = {task.planning_phase: task for task in plan.tasks}

    assert by_phase[PlanningPhase.SENIOR_INDEPENDENT_READING].blocked_by == ()
    assert by_phase[PlanningPhase.STAFF_INDEPENDENT_READING].blocked_by == ()
    assert by_phase[PlanningPhase.JUNIOR_VERIFICATION_PLAN].blocked_by == (
        by_phase[PlanningPhase.SENIOR_INDEPENDENT_READING].task_name,
    )
    assert set(by_phase[PlanningPhase.SENIOR_OWNED_PLAN].blocked_by) == {
        by_phase[PlanningPhase.SENIOR_INDEPENDENT_READING].task_name,
        by_phase[PlanningPhase.JUNIOR_VERIFICATION_PLAN].task_name,
    }


def test_independent_prompt_has_raw_contract_but_no_junior_output(tmp_path: Path) -> None:
    target = _target(tmp_path / "target")
    plan = _plan(target)
    task = next(
        task
        for task in plan.tasks
        if task.planning_phase is PlanningPhase.SENIOR_INDEPENDENT_READING
    )

    prompt = build_agent_task_prompt(
        task,
        _context(target, tuple(candidate.task_name for candidate in plan.tasks)),
    )

    assert "the complete raw operator contract" in prompt
    assert "No junior conclusion is visible in this turn" in prompt
    assert "Completed dependency outputs:" not in prompt


def test_scheduler_persists_each_phase_before_revealing_it(tmp_path: Path) -> None:
    target = _target(tmp_path / "target")
    plan = _plan(target)
    task_names = tuple(task.task_name for task in plan.tasks)
    context = _context(target, task_names)
    events: list[tuple[str, str]] = []
    dependency_names: dict[str, tuple[str, ...]] = {}
    lock = threading.Lock()

    def coordination(command):
        assert isinstance(command, SubmitArtifact)
        with lock:
            events.append(("persist", command.artifact_type))
        return AcknowledgementResult(
            command.name,
            LedgerRecord({"artifact_id": f"artifact-{command.artifact_type}"}),
        )

    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        coordination_command=coordination,
    )

    def fake_run_scheduled_task(**kwargs):
        task = kwargs["task"]
        deps = kwargs["dependency_results"]
        with lock:
            events.append(("start", task.planning_phase.value))
            dependency_names[task.planning_phase.value] = tuple(dep.task_name for dep in deps)
        artifact_type = (
            "delegated_task_run"
            if task.planning_phase is PlanningPhase.JUNIOR_VERIFICATION_PLAN
            else "cli_agent_run"
        )
        return PowWowTaskResult(
            task_name=task.task_name,
            role=task.role,
            status="completed",
            summary="phase complete",
            artifacts=(
                PowWowArtifact(
                    artifact_type=artifact_type,
                    content={"output": f"evidence from {task.planning_phase.value}"},
                    task_name=task.task_name,
                ),
            ),
        )

    executor._run_scheduled_task = fake_run_scheduled_task  # type: ignore[method-assign]
    results = executor._run_dependency_scheduled_tasks(
        pow_wow_id="pow-planning",
        target_project=target,
        tasks=plan.tasks,
        context=context,
        code_worktrees={},
    )

    senior_read = next(
        task
        for task in plan.tasks
        if task.planning_phase is PlanningPhase.SENIOR_INDEPENDENT_READING
    )
    junior = next(
        task for task in plan.tasks if task.planning_phase is PlanningPhase.JUNIOR_VERIFICATION_PLAN
    )
    assert events.index(("persist", "senior_independent_reading")) < events.index(
        ("start", PlanningPhase.JUNIOR_VERIFICATION_PLAN.value)
    )
    assert dependency_names[PlanningPhase.JUNIOR_VERIFICATION_PLAN.value] == (
        senior_read.task_name,
    )
    assert dependency_names[PlanningPhase.SENIOR_INDEPENDENT_READING.value] == ()
    assert dependency_names[PlanningPhase.STAFF_INDEPENDENT_READING.value] == ()
    junior_result = next(result for result in results if result.task_name == junior.task_name)
    evidence = next(
        artifact
        for artifact in junior_result.artifacts
        if artifact.artifact_type == "junior_verification_plan"
    )
    assert evidence.schema_version == "planning_evidence.v1"
    assert evidence.content["non_exhaustive"] is True
    assert evidence.persisted_artifact_id == "artifact-junior_verification_plan"


def test_scheduler_persists_reading_audit_with_host_supplied_sha(tmp_path: Path) -> None:
    target = _target(tmp_path / "target")
    head_sha = _init_git_repo(target.expanded_path)
    plan = _plan(target)
    task_names = tuple(task.task_name for task in plan.tasks)
    context = _context(target, task_names)
    submitted: list[SubmitArtifact] = []

    def coordination(command):
        assert isinstance(command, SubmitArtifact)
        submitted.append(command)
        return AcknowledgementResult(
            command.name,
            LedgerRecord({"artifact_id": f"artifact-{len(submitted)}"}),
        )

    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        coordination_command=coordination,
    )

    def fake_run_scheduled_task(**kwargs):
        task = kwargs["task"]
        output = f"evidence from {task.planning_phase.value}"
        if task.planning_phase is PlanningPhase.SENIOR_INDEPENDENT_READING:
            output += (
                "\n```repo_audit.v1\n"
                '{"claims": [{"claim": "stable is enabled", '
                '"file": "stable.py", "line_start": 1}]}\n'
                "```"
            )
        artifact_type = (
            "delegated_task_run"
            if task.planning_phase is PlanningPhase.JUNIOR_VERIFICATION_PLAN
            else "cli_agent_run"
        )
        return PowWowTaskResult(
            task_name=task.task_name,
            role=task.role,
            status="completed",
            summary="phase complete",
            artifacts=(
                PowWowArtifact(
                    artifact_type=artifact_type,
                    content={"output": output},
                    task_name=task.task_name,
                ),
            ),
        )

    executor._run_scheduled_task = fake_run_scheduled_task  # type: ignore[method-assign]
    executor._run_dependency_scheduled_tasks(
        pow_wow_id="pow-planning",
        target_project=target,
        tasks=plan.tasks,
        context=context,
        code_worktrees={},
    )

    audit_submission = next(item for item in submitted if item.artifact_type == "repo_audit")
    audit = RepoAudit.from_payload(json.loads(audit_submission.content))
    assert audit.target_project_id == target.id
    assert audit.commit_sha == head_sha
    assert audit.claims[0].file == "stable.py"
    assert audit_submission.schema_version == "repo_audit.v1"


def test_next_same_tier_prompt_partitions_audit_against_real_git_diff(
    tmp_path: Path,
) -> None:
    target = _target(tmp_path / "target")
    audited_sha = _init_git_repo(target.expanded_path)
    (target.expanded_path / "changed.py").write_text("VALUE = 2\n", encoding="utf-8")
    _run_git(target.expanded_path, "add", "changed.py")
    _run_git(target.expanded_path, "commit", "-m", "change assumption")
    head_sha = _run_git(target.expanded_path, "rev-parse", "HEAD")
    audit_content = json.dumps(
        {
            "schema_version": "repo_audit.v1",
            "target_project_id": target.id,
            "commit_sha": audited_sha,
            "claims": [
                {"claim": "stable is enabled", "file": "stable.py", "line_start": 1},
                {"claim": "value remains one", "file": "changed.py", "line_start": 1},
            ],
            "phase": PlanningPhase.SENIOR_INDEPENDENT_READING.value,
            "tier": Tier.SENIOR.value,
            "task_name": "prior_reading",
        }
    )

    def coordination(command):
        assert isinstance(command, LatestRepoAudit)
        assert command.target_project_id == target.id
        assert command.tier == Tier.SENIOR.value
        return CollectionResult(
            CoordinationCommandName.LATEST_REPO_AUDIT,
            "artifacts",
            (LedgerRecord({"content": audit_content}),),
        )

    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        coordination_command=coordination,
    )
    plan = _plan(target)
    task = next(
        item
        for item in plan.tasks
        if item.planning_phase is PlanningPhase.SENIOR_INDEPENDENT_READING
    )
    audit_block = executor._audit_context_block_for(  # noqa: SLF001 - contract seam
        task,
        target_project=target,
        repo_path=target.expanded_path,
    )
    prompt = build_agent_task_prompt(
        task,
        _context(target, tuple(item.task_name for item in plan.tasks)),
        audit_context_block=audit_block,
    )

    assert f"read at {audited_sha}; this worktree is at {head_sha}" in prompt
    assert "Files changed since then: changed.py" in prompt
    assert "Verified pointers" in prompt
    assert "stable is enabled [stable.py:1]" in prompt
    assert "Hypotheses" in prompt
    assert "value remains one" in prompt


def test_reviewer_prompt_never_inherits_repo_audit(tmp_path: Path) -> None:
    target = _target(tmp_path / "target")
    plan = _plan(target)
    review = next(
        item for item in plan.tasks if item.planning_phase is PlanningPhase.STAFF_FINAL_REVIEW
    )

    def coordination(command):
        raise AssertionError(f"reviewer must not query an audit: {command}")

    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        coordination_command=coordination,
    )
    audit_block = executor._audit_context_block_for(  # noqa: SLF001 - independence seam
        review,
        target_project=target,
        repo_path=target.expanded_path,
    )
    prompt = build_agent_task_prompt(
        review,
        _context(target, tuple(item.task_name for item in plan.tasks)),
        audit_context_block=audit_block,
    )

    assert audit_block == ""
    assert "Prior repository audit" not in prompt


def test_missing_or_malformed_audit_never_fails_the_reading(tmp_path: Path) -> None:
    target = _target(tmp_path / "target")
    plan = _plan(target)
    reading = next(
        item
        for item in plan.tasks
        if item.planning_phase is PlanningPhase.SENIOR_INDEPENDENT_READING
    )
    context = _context(target, tuple(item.task_name for item in plan.tasks))

    def result_with(output: str) -> PowWowTaskResult:
        return PowWowTaskResult(
            task_name=reading.task_name,
            role=reading.role,
            status="completed",
            summary="complete",
            artifacts=(
                PowWowArtifact(
                    artifact_type="cli_agent_run",
                    content={"output": output},
                    task_name=reading.task_name,
                ),
            ),
        )

    missing = result_with("ordinary prose")
    unchanged = persist_repo_audit(
        pow_wow_id="pow",
        task=reading,
        result=missing,
        context=context,
        resolve_head_sha=lambda: "must not be called",
        coordination_command=None,
    )
    assert unchanged is missing

    malformed = persist_repo_audit(
        pow_wow_id="pow",
        task=reading,
        result=result_with("```repo_audit.v1\n{not json}\n```"),
        context=context,
        resolve_head_sha=lambda: "a" * 40,
        coordination_command=None,
    )
    assert malformed.status == "completed"
    assert malformed.risks == (
        "repo_audit.v1 block is malformed and was not persisted: "
        "repo_audit.v1 block is not valid JSON: "
        "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
    )


def test_executor_fails_closed_on_invalid_visibility_graph(tmp_path: Path) -> None:
    target = _target(tmp_path / "target")
    plan = _plan(target)
    tasks = list(plan.tasks)
    junior_index = next(
        index
        for index, task in enumerate(tasks)
        if task.planning_phase is PlanningPhase.JUNIOR_VERIFICATION_PLAN
    )
    junior = tasks[junior_index]
    tasks[junior_index] = type(junior)(
        **{**junior.__dict__, "blocked_by": ()},
    )
    executor = CliPowWowExecutor(worktree_root=tmp_path / "worktrees")

    result = executor.dispatch_pow_wow(
        "pow-invalid",
        target,
        tasks,
        _context(target, tuple(task.task_name for task in tasks)),
    )

    assert result.status == "BLOCKED"
    assert result.external_agents_started is False
    assert "junior verification plan must wait" in result.output_summary
    assert result.tasks == ()
