# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import pytest

from local_first_agent_os.constants import CLI_AGENT_RUN_ARTIFACT_TYPE
from local_first_agent_os.coordination.contracts import (
    CoordinationCommand,
    CoordinationResult,
    DispatchKind,
    EntityResult,
    FindAgentContinuation,
    LedgerRecord,
)
from local_first_agent_os.pow_wow.executor import (
    CliPowWowExecutor,
    ReaderToImplementationModelTransition,
    ReadOnlyToImplementation,
    ResumeExisting,
    StartFreshBounded,
)
from local_first_agent_os.pow_wow.prompts import build_resumed_senior_implementation_prompt
from local_first_agent_os.pow_wow.protocol import PlanningPhase, TaskPurpose
from local_first_agent_os.pow_wow.types import (
    PowWowArtifact,
    PowWowExecutionContext,
    PowWowTaskResult,
    PowWowTaskSpec,
)
from local_first_agent_os.project_access import AccessMode, ProjectAccessPolicy
from local_first_agent_os.project_center import LinkedProject
from local_first_agent_os.spawn_authority import UnattendedImplementation
from local_first_agent_os.staffing import (
    BenchSlot,
    FrontierHarness,
    Harness,
    JudgmentRole,
    JudgmentWorkload,
    WorkloadModelProfile,
)
from local_first_agent_os.vocabulary import DispatchTier


def _target(path: Path) -> LinkedProject:
    return LinkedProject(
        id="local_first_agent_os",
        kind="agent_os",
        path=path,
        status="active",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="agent operating system",
        verification_commands=[],
    )


def _context(target: LinkedProject) -> PowWowExecutionContext:
    return PowWowExecutionContext(
        saga_id="saga-1",
        goal="Implement the bounded change",
        directive="/saga Implement the bounded change",
        target_project_id=target.id,
        target_project_path=str(target.expanded_path),
        target_project_kind=target.kind,
        target_project_status=target.status,
        target_project_read_only=False,
        task_ids_by_name={
            "senior_read": "task-reader",
            "junior_plan": "task-junior",
            "senior_implementation": "task-implementation",
        },
    )


def _result(
    task_name: str,
    phase: PlanningPhase,
    summary: str,
) -> PowWowTaskResult:
    artifacts = [
        PowWowArtifact(
            artifact_type=CLI_AGENT_RUN_ARTIFACT_TYPE,
            task_name=task_name,
            schema_version="cli_agent_run.v1",
            content={
                "schema_version": "cli_agent_run.v1",
                "task": {"planning_phase": phase.value},
                "output": summary,
            },
        )
    ]
    if phase is PlanningPhase.SENIOR_INDEPENDENT_READING:
        artifacts.append(
            PowWowArtifact(
                artifact_type="senior_independent_reading",
                task_name=task_name,
                schema_version="planning_evidence.v1",
                content={
                    "schema_version": "planning_evidence.v1",
                    "phase": phase.value,
                    "model_output": summary,
                },
                persisted_artifact_id="artifact-reader",
            )
        )
    return PowWowTaskResult(
        task_name=task_name,
        role="reader" if phase is PlanningPhase.SENIOR_INDEPENDENT_READING else "planner",
        status="completed",
        summary=summary,
        artifacts=tuple(artifacts),
    )


def _implementation_task() -> PowWowTaskSpec:
    return PowWowTaskSpec(
        task_name="senior_implementation",
        role="implementer",
        description="Implement the smallest safe change.",
        success_criteria=("Focused tests pass.",),
        capabilities=("read_repository", "write_repository", "run_command", "invoke_model"),
        purpose=TaskPurpose.IMPLEMENTATION,
        judgment=JudgmentRole(name="implementer", tier=DispatchTier.SENIOR),
        dispatch_kind=DispatchKind.CODE,
        blocked_by=("senior_read", "junior_plan"),
        worktree_group="code",
        planning_phase=PlanningPhase.SENIOR_OWNED_PLAN,
    )


def _codex_bench() -> dict[DispatchTier, BenchSlot]:
    return {
        DispatchTier.SENIOR: BenchSlot(
            harness=Harness.CODEX,
            model="gpt-5.6-sol",
            workload_profiles=(
                WorkloadModelProfile(
                    workload=JudgmentWorkload.INDEPENDENT_READING,
                    model="gpt-5.6-terra",
                    reasoning_effort="medium",
                ),
            ),
        ),
    }


def test_compatible_reader_thread_becomes_resume_existing(tmp_path: Path) -> None:
    target = _target(tmp_path / "repo")
    context = _context(target)
    thread_id = "01a00bac-e60b-7321-8d47-50ee11829924"

    def coordinate(command: CoordinationCommand) -> CoordinationResult:
        assert isinstance(command, FindAgentContinuation)
        assert command.source_task_id == "task-reader"
        assert command.source_model == "gpt-5.6-terra"
        source_permission = executor._authority_sha256(
            executor._task_spawn_authority(
                PowWowTaskSpec(
                    task_name="reader",
                    role="independent_reader",
                    description="read",
                    purpose=TaskPurpose.ADVISORY,
                )
            )
        )
        return EntityResult(
            command=command.name,
            field="continuation",
            entity=LedgerRecord(
                {
                    "thread_id": thread_id,
                    "permission_envelope_sha256": source_permission,
                }
            ),
            metadata=LedgerRecord({"ok": True, "compatible": True, "reason": "compatible"}),
        )

    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        coordination_command=coordinate,
        bench=_codex_bench(),
    )
    decision = executor._frontier_launch_decision(
        pow_wow_id="pow-wow-1",
        target_project=target,
        task=_implementation_task(),
        context=context,
        dependency_results=(
            _result(
                "senior_read",
                PlanningPhase.SENIOR_INDEPENDENT_READING,
                "large independent reading",
            ),
            _result(
                "junior_plan",
                PlanningPhase.JUNIOR_VERIFICATION_PLAN,
                "new junior hypotheses",
            ),
        ),
        harness=FrontierHarness.CODEX,
        model="gpt-5.6-sol",
        source_revision="b" * 40,
    )

    expected_source_permission = executor._authority_sha256(
        executor._task_spawn_authority(
            PowWowTaskSpec(
                task_name="reader",
                role="independent_reader",
                description="read",
                purpose=TaskPurpose.ADVISORY,
            )
        )
    )
    expected_target_permission = executor._permission_envelope_sha256(_implementation_task())
    assert decision == ResumeExisting(
        thread_id=thread_id,
        source_task_name="senior_read",
        source_task_id="task-reader",
        authority_transition=ReadOnlyToImplementation(
            source_permission_envelope_sha256=expected_source_permission,
            target_permission_envelope_sha256=expected_target_permission,
        ),
        model_transition=ReaderToImplementationModelTransition(
            source_model="gpt-5.6-terra",
            target_model="gpt-5.6-sol",
        ),
    )


def test_resume_refuses_an_unrecognized_source_permission_envelope(tmp_path: Path) -> None:
    target = _target(tmp_path / "repo")
    context = _context(target)

    def coordinate(command: CoordinationCommand) -> CoordinationResult:
        assert isinstance(command, FindAgentContinuation)
        return EntityResult(
            command=command.name,
            field="continuation",
            entity=LedgerRecord(
                {
                    "thread_id": "01a00bac-e60b-7321-8d47-50ee11829924",
                    "permission_envelope_sha256": "f" * 64,
                }
            ),
            metadata=LedgerRecord({"ok": True, "compatible": True, "reason": "compatible"}),
        )

    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        coordination_command=coordinate,
        bench=_codex_bench(),
    )
    decision = executor._frontier_launch_decision(
        pow_wow_id="pow-wow-1",
        target_project=target,
        task=_implementation_task(),
        context=context,
        dependency_results=(
            _result(
                "senior_read",
                PlanningPhase.SENIOR_INDEPENDENT_READING,
                "independent reading",
            ),
        ),
        harness=FrontierHarness.CODEX,
        model="gpt-5.6-sol",
        source_revision="b" * 40,
    )

    assert decision == StartFreshBounded("continuation source permission envelope mismatch")


def test_resume_command_keeps_reading_as_typed_disputable_evidence(tmp_path: Path) -> None:
    target = _target(tmp_path / "repo")
    context = _context(target)
    task = _implementation_task()
    reading = _result(
        "senior_read",
        PlanningPhase.SENIOR_INDEPENDENT_READING,
        "large independent reading",
    )
    junior = _result(
        "junior_plan",
        PlanningPhase.JUNIOR_VERIFICATION_PLAN,
        "new junior hypotheses",
    )
    prompt = build_resumed_senior_implementation_prompt(
        task,
        context,
        dependency_results=(reading, junior),
    )
    thread_id = "01a00bac-e60b-7321-8d47-50ee11829924"
    executor = CliPowWowExecutor(worktree_root=tmp_path / "worktrees", codex_bin="codex")
    command = executor._build_agent_cli_command(
        FrontierHarness.CODEX,
        "gpt-5.6-sol",
        prompt,
        UnattendedImplementation(),
        reasoning_effort="high",
        continuation_thread_id=thread_id,
    )

    assert command[:3] == ("codex", "exec", "resume")
    assert command[-2:] == (thread_id, prompt)
    assert "new junior hypotheses" in prompt
    assert "large independent reading" in prompt
    assert "planning_evidence.v1" in prompt
    assert "disputable evidence" in prompt
    assert "skills/agent-startup/SKILL.md" not in prompt
    assert "Do not repeat that exploration" in prompt
    assert str(target.expanded_path) in prompt


def test_resume_prompt_requires_persisted_reading_evidence(tmp_path: Path) -> None:
    target = _target(tmp_path / "repo")
    junior = _result(
        "junior_plan",
        PlanningPhase.JUNIOR_VERIFICATION_PLAN,
        "new junior hypotheses",
    )

    with pytest.raises(ValueError, match="persisted senior_independent_reading"):
        build_resumed_senior_implementation_prompt(
            _implementation_task(),
            _context(target),
            dependency_results=(junior,),
        )
