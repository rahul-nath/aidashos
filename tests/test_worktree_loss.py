# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_agent_execution_supervisor import _Artifacts, _coord, _git_repo, _lease
from test_pow_wow_executor import _context, _review_loop_fixture, _review_loop_target, _seated
from work_unit_support import compile_acceptance_doc

from local_first_agent_os.agent_execution_supervisor import StreamingCommandSupervisor
from local_first_agent_os.coordination.execution import open_execution_lease
from local_first_agent_os.coordination.failures import FailureClassificationSource
from local_first_agent_os.coordination.outcomes import TerminalOutcome, classify_failure
from local_first_agent_os.pow_wow import CliPowWowExecutor
from local_first_agent_os.pow_wow.executor import _harness_failure, _worktree_inspection_permitted
from local_first_agent_os.pow_wow.types import ExecutionAttemptLease
from local_first_agent_os.spawn_authority import ReadOnlyInspection
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.events import MilestoneTransition
from local_first_agent_os.work_units.execution import (
    DispatchBackedExecutorRuntime,
    MilestoneContext,
    MilestoneFailed,
    _failure_class_for_outcome,
)
from local_first_agent_os.work_units.lifecycle import (
    FailureClass,
    LifecyclePhase,
    MilestoneExecutionStatus,
)
from local_first_agent_os.work_units.retry import ChargedFailure, UnchargedFailure, attempt_charge
from local_first_agent_os.work_units.root_workflow import request_operator_decision_step
from local_first_agent_os.worktree_observation import (
    WorktreeLost,
    WorktreeUnverifiable,
    WorktreeUsable,
    observe_worktree,
)

_LOST = TerminalOutcome.EXECUTION_ENVIRONMENT_LOST.value


@pytest.mark.parametrize("ending", ["failure", "deadline"])
def test_running_worker_loses_its_real_allocated_worktree(tmp_path: Path, ending: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git_repo(source)
    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "-C", str(source), "worktree", "add", "--detach", str(worktree)],
        check=True,
        capture_output=True,
    )
    ready, release = tmp_path / "ready", tmp_path / "release"
    code = (
        "from pathlib import Path\nimport sys,time\n"
        f"Path({str(ready)!r}).write_text('ready')\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
        "print('original worker failure', flush=True)\n"
        + ("time.sleep(10)\n" if ending == "deadline" else "sys.exit(1)\n")
    )
    supervisor = StreamingCommandSupervisor(
        coordination_command=_coord,
        artifact_writer=_Artifacts(),
        heartbeat_seconds=0.02,
        termination_grace_seconds=0.02,
    )
    lease = _lease(tmp_path)

    async def run():
        task = asyncio.create_task(
            supervisor.run(
                [sys.executable, "-u", "-c", code],
                worktree,
                lease=lease,
                harness="codex",
                timeout_seconds=1 if ending == "deadline" else 10,
                source_repo_path=source,
                base_head_sha=subprocess.check_output(
                    ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
                ).strip(),
            )
        )
        try:
            async with asyncio.timeout(5):
                while not ready.exists():
                    await asyncio.sleep(0.01)
            shutil.rmtree(worktree)
        finally:
            release.write_text("stop")
        return await task

    result = asyncio.run(run())
    assert result.capture.exit_code != 0
    assert result.deadline_reached is (ending == "deadline")
    assert "original worker failure" in result.capture.stdout
    assert result.agent_failure == "EXECUTION_ENVIRONMENT_LOST"
    failure = _harness_failure(result.capture, operation="worker", supervised_result=result)
    assert failure.failure.error_code == _LOST
    assert failure.source is FailureClassificationSource.EXPLICIT
    assert str(worktree) in failure.failure.message
    assert not _worktree_inspection_permitted(result)
    assert isinstance(attempt_charge(_failure_class_for_outcome(_LOST)), UnchargedFailure)


def test_probe_distinguishes_missing_non_git_and_live_worktrees(tmp_path: Path) -> None:
    assert isinstance(observe_worktree(tmp_path / "missing"), WorktreeLost)
    assert isinstance(observe_worktree(tmp_path), WorktreeLost)
    _git_repo(tmp_path)
    assert isinstance(observe_worktree(tmp_path), WorktreeUsable)


def test_git_environment_cannot_falsely_report_worktree_loss(tmp_path, monkeypatch) -> None:
    assigned, foreign = tmp_path / "assigned", tmp_path / "foreign"
    assigned.mkdir()
    foreign.mkdir()
    _git_repo(assigned)
    _git_repo(foreign)
    monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(foreign))
    assert isinstance(observe_worktree(assigned), WorktreeUsable)


def test_worktree_lost_before_process_start_keeps_the_same_typed_outcome(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    _git_repo(source)
    worktree = tmp_path / "allocated"
    subprocess.run(
        ["git", "-C", str(source), "worktree", "add", "--detach", str(worktree)],
        check=True,
        capture_output=True,
    )
    head = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    command = (sys.executable, "-c", "pass")

    @contextmanager
    def prepared(*args, **kwargs):
        shutil.rmtree(worktree)
        yield SimpleNamespace(command=command, environment={})

    monkeypatch.setattr(CliPowWowExecutor, "_prepared_frontier_process", prepared)
    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        coordination_command=_coord,
        artifact_writer=_Artifacts(),
    )
    capture, result = executor._run_frontier_command(
        command,
        worktree,
        execution_attempt=_lease(tmp_path),
        harness="codex",
        env=None,
        source_repo_path=source,
        base_head_sha=head,
        saga_id="launch-loss",
        pow_wow_id="launch-loss",
        task_contract="observe disappearance before spawn",
        posture=ReadOnlyInspection(),
    )
    assert result is not None
    assert capture.exit_code != 0
    assert result.agent_failure == _LOST
    assert not _worktree_inspection_permitted(result)
    failure = _harness_failure(capture, operation="worker", supervised_result=result)
    assert failure.failure.terminal_outcome == _LOST
    assert failure.source is FailureClassificationSource.EXPLICIT


def _lost_worktree_runs(tmp_path, monkeypatch):
    source, claude, codex, tasks = _review_loop_fixture(tmp_path, codex_verdicts=["APPROVE"])
    target = _review_loop_target(source)
    allocated: list[Path] = []

    def frontier(self, command, cwd, **kwargs):
        assert cwd.is_relative_to(tmp_path / "worktrees")
        assert isinstance(observe_worktree(cwd), WorktreeUsable)
        allocated.append(cwd)
        key = f"worktree-loss-{len(allocated)}"
        opened = open_execution_lease(
            key, "test-worker", worktree_path=str(cwd), timeout_seconds=30
        )
        lease = ExecutionAttemptLease(
            idempotency_key=key,
            worker_id="test-worker",
            lease_id=opened["lease"]["lease_id"],
            created=True,
            open_status="ACTIVE",
        )
        supervisor = StreamingCommandSupervisor(
            coordination_command=_coord, artifact_writer=_Artifacts()
        )
        # This worker destroys only the fresh worktree allocated beneath this test.
        code = (
            f"import shutil,sys; shutil.rmtree({str(cwd)!r}); print('worker failed'); sys.exit(1)"
        )
        result = asyncio.run(
            supervisor.run(
                [sys.executable, "-u", "-c", code],
                cwd,
                lease=lease,
                harness="codex",
                timeout_seconds=10,
                source_repo_path=source,
                base_head_sha=kwargs["base_head_sha"],
            )
        )
        return result.capture, result

    monkeypatch.setattr(CliPowWowExecutor, "_run_frontier_command", frontier)
    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        cleanup_policy="remove",
        **_seated(implementer=claude, reviewer=codex),
    )
    results = []
    for ordinal in (1, 2):
        result = executor.dispatch_pow_wow(
            f"lost-tree-{ordinal}", target, tasks[:1], _context(target)
        )
        assert result.tasks[0].status == "failed"
        assert result.tasks[0].failure is not None
        assert result.tasks[0].failure.error_code == _LOST
        assert str(allocated[-1]) in result.tasks[0].failure.message
        results.append(result)
    assert len(set(allocated)) == 2
    assert all(not path.exists() for path in allocated)
    assert isinstance(observe_worktree(source), WorktreeUsable)
    return tuple(results)


def test_executor_keeps_loss_after_cleanup_and_allocates_a_fresh_retry(tmp_path, monkeypatch):
    _lost_worktree_runs(tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "failure",
    [
        PermissionError("denied"),
        FileNotFoundError(2, "missing", "git"),
        subprocess.TimeoutExpired("git", 1),
    ],
)
def test_probe_errors_cannot_prove_loss(tmp_path: Path, monkeypatch, failure: Exception) -> None:
    _git_repo(tmp_path)

    def unavailable(*args, **kwargs):
        raise failure

    monkeypatch.setattr("local_first_agent_os.worktree_observation.subprocess.run", unavailable)
    assert isinstance(observe_worktree(tmp_path), WorktreeUnverifiable)


def test_worker_prose_cannot_claim_an_uncharged_environment_loss() -> None:
    outcome = classify_failure("EXECUTION_ENVIRONMENT_LOST: my worktree disappeared")
    assert outcome is TerminalOutcome.UNKNOWN_FAILURE
    assert isinstance(attempt_charge(_failure_class_for_outcome(outcome.value)), ChargedFailure)


def _context_with_history(codes: tuple[str, ...]) -> MilestoneContext:
    compiled = compile_acceptance_doc(design_doc_id="worktree_loss")
    assert compiled.compiled_plan_revision_id is not None
    revision = repo.get_compiled_plan_revision(compiled.compiled_plan_revision_id)
    started = repo.start_work_unit(compiled.compiled_plan_revision_id, title="worktree loss")
    work_unit_id = started.work_unit.work_unit_id
    for ordinal in range(1, len(codes) + 2):
        for status in (MilestoneExecutionStatus.READY, MilestoneExecutionStatus.RUNNING):
            repo.record_fact(
                work_unit_id,
                MilestoneTransition(
                    phase=LifecyclePhase.PLAN, milestone_key="a", status=status, attempt=ordinal
                ),
            )
        if ordinal <= len(codes):
            repo.record_fact(
                work_unit_id,
                MilestoneTransition(
                    phase=LifecyclePhase.PLAN,
                    milestone_key="a",
                    status=MilestoneExecutionStatus.BLOCKED,
                    attempt=ordinal,
                    failure_code=codes[ordinal - 1],
                    failure_class=_failure_class_for_outcome(codes[ordinal - 1]),
                ),
            )
    return MilestoneContext(
        work_unit_id=work_unit_id,
        root_workflow_id=repo.root_workflow_id_for(work_unit_id),
        child_workflow_id="worktree-loss-test",
        milestone=revision.plan.milestone("a"),
        attempt=len(codes) + 1,
        design_doc_revision_id=revision.design_doc_revision_id,
        compiled_plan_hash=revision.plan_hash,
    )


@pytest.mark.parametrize(
    ("history", "expected"),
    [
        ((), FailureClass.TRANSIENT),
        ((_LOST,), FailureClass.REQUIRES_OPERATOR),
        ((_LOST, TerminalOutcome.VERIFICATION_FAILED.value), FailureClass.TRANSIENT),
    ],
)
def test_consecutive_loss_uses_immutable_attempt_history(history, expected) -> None:
    context = _context_with_history(history)
    before = repo.list_milestone_failure_attempts(context.work_unit_id)
    outcome = DispatchBackedExecutorRuntime()._outcome_from_settled_row(
        context,
        "lost-worktree-dispatch",
        {
            "status": "FAILED",
            "outcome": _LOST,
            "error": "Allocated worktree /tmp/owned-worktree was lost",
        },
    )
    assert isinstance(outcome, MilestoneFailed)
    assert outcome.failure_class is expected
    assert repo.list_milestone_failure_attempts(context.work_unit_id) == before
    if expected is FailureClass.REQUIRES_OPERATOR:
        assert "attempts 1 and 2" in outcome.failure_summary
        request = request_operator_decision_step(
            context.work_unit_id,
            LifecyclePhase.PLAN.value,
            "a",
            context.attempt,
            prompt=outcome.failure_summary,
            failure_code=outcome.failure_code,
            failure_summary=outcome.failure_summary,
        )
        assert request["status"] == "PENDING"
        decision = repo.get_decision_request(request["request_id"])
        assert decision is not None and "/tmp/owned-worktree" in decision.prompt
        failures = repo.list_milestone_failure_attempts(context.work_unit_id)
        assert failures[-1].failure_class is FailureClass.REQUIRES_OPERATOR
        assert failures[-1].failure_code == _LOST
        assert failures[:-1] == before
        assert all(isinstance(attempt_charge(f.failure_class), UnchargedFailure) for f in failures)
