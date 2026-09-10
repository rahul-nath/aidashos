# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest
from staffing_support import repo_bench
from test_pow_wow_executor import _context, _init_git_repo, _target

from local_first_agent_os.agent_execution_supervisor import SupervisedCommandResult
from local_first_agent_os.coordination import (
    CompleteExecutionLease,
    CoordinationCommand,
    CoordinationResult,
    DispatchKind,
    parse_coordination_result,
)
from local_first_agent_os.coordination.outcomes import (
    AgentStatus,
    CheckpointReason,
    InfrastructureFailure,
    PersistenceStatus,
    SupervisorStatus,
)
from local_first_agent_os.pow_wow import executor as module
from local_first_agent_os.pow_wow.git_ops import WorktreeAllocation, run_git_command_for_output
from local_first_agent_os.pow_wow.types import (
    CommandRunCapture,
    ExecutionAttemptLease,
    PowWowTaskSpec,
)
from local_first_agent_os.staffing import FrontierHarness, JudgmentRole
from local_first_agent_os.vocabulary import DispatchTier

FailureCase = Literal["valid", "checkpoint", "persistence", "supervisor"]


def _supervision(capture: CommandRunCapture, case: FailureCase) -> SupervisedCommandResult:
    result = SupervisedCommandResult(
        capture=capture,
        deadline_reached=False,
        cancel_requested=False,
        transcript_artifact_id="retained-transcript",
        checkpoint_id=None,
        checkpoint_artifact_ids=(),
        checkpoint_reason=None,
        preserve_worktree=False,
        event_count=4,
        agent_status=AgentStatus.COMPLETED,
        supervisor_status=SupervisorStatus.COMPLETED,
        persistence_status=PersistenceStatus.COMPLETED,
    )
    if case == "checkpoint":
        return replace(
            result,
            checkpoint_reason=CheckpointReason.SUPERVISOR_ERROR,
            preserve_worktree=True,
            persistence_status=PersistenceStatus.FAILED,
            persistence_failure=InfrastructureFailure.CHECKPOINT_WRITE_FAILED.value,
        )
    if case == "persistence":
        return replace(
            result,
            persistence_status=PersistenceStatus.FAILED,
            persistence_failure=InfrastructureFailure.EVENT_WRITE_FAILED.value,
        )
    if case == "supervisor":
        return replace(
            result,
            supervisor_status=SupervisorStatus.FAILED,
            supervisor_failure="retained supervision failure",
        )
    return result


@pytest.mark.parametrize("path", ["code", "advisory", "fallback"])
@pytest.mark.parametrize("case", ["valid", "checkpoint", "persistence", "supervisor"])
def test_frontier_paths_require_durable_supervision_before_commit_or_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: Literal["code", "advisory", "fallback"],
    case: FailureCase,
) -> None:
    repository = tmp_path / "repository"
    _init_git_repo(repository)
    project = replace(_target(repository), verification_commands=["registered fixture gate"])
    context = _context(project)
    if path == "advisory":
        context = replace(context, dispatch_kind=DispatchKind.ADVISORY)
    task = PowWowTaskSpec(
        task_name="implementation",
        role="implementer",
        judgment=JudgmentRole(name="implementer", tier=DispatchTier.SENIOR),
        description="Exercise the supervision completion boundary",
    )
    head = run_git_command_for_output(repository, ("rev-parse", "HEAD")).strip()
    branch = run_git_command_for_output(repository, ("branch", "--show-current")).strip()
    worktree = WorktreeAllocation(str(repository), str(repository), head, branch, "preserve", True)
    attempt = ExecutionAttemptLease(
        "attempt-idempotency", "fixture-worker", lease_id="fixture-lease", created=True
    )
    captures: list[CommandRunCapture] = []
    completions: list[CompleteExecutionLease] = []
    verification_calls: list[str] = []
    commit_calls: list[str] = []

    def coordination(command: CoordinationCommand) -> CoordinationResult:
        assert isinstance(command, CompleteExecutionLease)
        completions.append(command)
        return parse_coordination_result(
            command,
            {"ok": True, "lease": {"lease_id": command.lease_id, "status": command.status.value}},
        )

    runner = module.CliPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        cleanup_policy="preserve",
        claude_bin="/usr/bin/true",
        coordination_command=coordination,
    )
    monkeypatch.setattr(runner, "_authorize_spawn", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner, "_resolve_frontier_harness", lambda _: FrontierHarness.CLAUDE)
    monkeypatch.setattr(runner, "_open_execution_attempt_lease", lambda **kwargs: attempt)
    monkeypatch.setattr(runner, "_audit_context_block_for", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        runner,
        "_select_alternate_frontier_slot",
        lambda _: (FrontierHarness.CLAUDE, repo_bench()[DispatchTier.SENIOR]),
    )

    def frontier(
        *args: object, **kwargs: object
    ) -> tuple[CommandRunCapture, SupervisedCommandResult]:
        if path != "advisory":
            (repository / "agent.txt").write_text("retained process change\n")
        capture = CommandRunCapture(
            "fixture process", str(repository), "retained process output", "", 0
        )
        captures.append(capture)
        return capture, _supervision(capture, case)

    def verification(command: str, cwd: Path, **kwargs: object) -> CommandRunCapture:
        verification_calls.append(command)
        return CommandRunCapture(command, str(cwd), "verification passed", "", 0)

    original_commit = module._commit_worktree_checkpoint_at_lifecycle_boundary

    def commit(
        allocation: WorktreeAllocation, *, task_name: str
    ) -> module.WorktreeCommitCheckpoint:
        commit_calls.append(task_name)
        return original_commit(allocation, task_name=task_name)

    monkeypatch.setattr(runner, "_run_frontier_command", frontier)
    monkeypatch.setattr(module, "run_captured_shell_command", verification)
    monkeypatch.setattr(module, "_commit_worktree_checkpoint_at_lifecycle_boundary", commit)
    if path == "code":
        result = runner._run_agent_task(
            pow_wow_id="pow-completion",
            target_project=project,
            task=task,
            context=context,
            worktree=worktree,
            cleanup_worktree=False,
        )
    elif path == "advisory":
        result = runner._run_advisory_agent_task(
            pow_wow_id="pow-completion", target_project=project, task=task, context=context
        )
    else:
        result = runner._execute_frontier_fallback_task(
            pow_wow_id="pow-completion",
            target_project=project,
            task=task,
            context=context,
            failed_harness=FrontierHarness.CODEX,
            failed_model="fixture-model",
            failure_reason="usage_limit",
            failed_capture=CommandRunCapture("primary", str(repository), "", "usage limit", 1),
            failed_attempt=None,
            failed_supervised_result=None,
            is_review=False,
            worktree=worktree,
        )
    assert result is not None
    assert len(captures) == 1 and captures[0].exit_code == 0
    assert captures[0].stdout == "retained process output" and captures[0].stderr == ""
    assert len(completions) == 1
    completion = completions[0]
    assert completion.result is not None
    recorded_capture = completion.result["command_capture"]
    assert isinstance(recorded_capture, Mapping)
    assert recorded_capture["exit_code"] == 0
    assert recorded_capture["stdout_tail"] == captures[0].stdout
    if case == "valid":
        assert result.status == "completed"
        assert completion.status.value == "COMPLETED"
        assert result.failure is None
        assert len(commit_calls) == (0 if path == "advisory" else 1)
        assert len(verification_calls) == (0 if path == "advisory" else 1)
    else:
        assert result.status == "failed"
        assert completion.status.value == "FAILED"
        assert result.failure is not None
        expected = {
            "checkpoint": InfrastructureFailure.CHECKPOINT_WRITE_FAILED,
            "persistence": InfrastructureFailure.EVENT_WRITE_FAILED,
            "supervisor": InfrastructureFailure.SUPERVISOR_FAILED,
        }[case]
        assert result.failure.error_code == expected.value
        assert completion.result["agent_status"] == AgentStatus.COMPLETED.value
        assert commit_calls == [] and verification_calls == []
        assert run_git_command_for_output(repository, ("rev-parse", "HEAD")).strip() == head
        assert completion.error and "supervised completion refused" in completion.error


def test_unsupervised_completion_retains_existing_exit_contract() -> None:
    capture = CommandRunCapture("fixture", "/repo", "process output", "", 0)
    assert module._allows_task_completion(capture, None)
    assert not module._allows_task_completion(replace(capture, exit_code=71), None)


def test_nonzero_process_failure_keeps_primary_evidence_with_failed_checkpoint() -> None:
    capture = CommandRunCapture("fixture", "/repo", "", "401 unauthorized", 71)
    supervised = _supervision(capture, "checkpoint")
    failure = module._failed_cli_task(capture, operation="fixture", supervised_result=supervised)
    assert failure.failure.error_code == InfrastructureFailure.AUTHENTICATION_FAILED.value
    assert failure.failure.message == "401 unauthorized"
    assert capture.exit_code == 71 and capture.stderr == "401 unauthorized"
