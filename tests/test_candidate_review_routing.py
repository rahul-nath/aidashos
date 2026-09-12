# SPDX-License-Identifier: AGPL-3.0-or-later
"""Host source evidence survives a browser-only model dependency edge."""

from pathlib import Path

import pytest
from staffing_support import repo_bench

from local_first_agent_os.coordination.contracts import DispatchKind
from local_first_agent_os.pow_wow.executor import CliPowWowExecutor, _CodeWorktreeLease
from local_first_agent_os.pow_wow.git_ops import WorktreeAllocation
from local_first_agent_os.pow_wow.protocol import TaskPurpose
from local_first_agent_os.pow_wow.types import (
    PowWowExecutionContext,
    PowWowTaskResult,
    PowWowTaskSpec,
)
from local_first_agent_os.project_access import AccessMode, ProjectAccessPolicy
from local_first_agent_os.project_center import LinkedProject
from local_first_agent_os.staffing import JudgmentRole
from local_first_agent_os.vocabulary import DispatchTier


def test_browser_review_receives_source_without_widening_prose_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = LinkedProject(
        id="website",
        kind="test",
        path=tmp_path,
        status="active",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="routing fixture",
    )
    context = PowWowExecutionContext(
        saga_id="saga",
        goal="review website",
        directive="review website",
        target_project_id=target.id,
        target_project_path=str(tmp_path),
        target_project_kind=target.kind,
        target_project_status=target.status,
        target_project_read_only=False,
    )
    tasks = (
        PowWowTaskSpec(
            "unrelated",
            "implementer",
            "Other worktree",
            purpose=TaskPurpose.IMPLEMENTATION,
            dispatch_kind=DispatchKind.CODE,
            worktree_group="other",
            judgment=JudgmentRole(name="implementer", tier=DispatchTier.SENIOR),
        ),
        PowWowTaskSpec(
            "implement",
            "implementer",
            "Build website",
            purpose=TaskPurpose.IMPLEMENTATION,
            dispatch_kind=DispatchKind.CODE,
            worktree_group="website",
            blocked_by=("unrelated",),
            judgment=JudgmentRole(name="implementer", tier=DispatchTier.SENIOR),
        ),
        PowWowTaskSpec(
            "browser",
            "browser",
            "Inspect website",
            purpose=TaskPurpose.BROWSER_ACCEPTANCE,
            dispatch_kind=DispatchKind.ADVISORY,
            worktree_group="website",
            blocked_by=("implement",),
        ),
        PowWowTaskSpec(
            "review",
            "reviewer",
            "Review website",
            purpose=TaskPurpose.REVIEW,
            dispatch_kind=DispatchKind.CODE,
            worktree_group="website",
            blocked_by=("browser",),
            judgment=JudgmentRole(name="reviewer", tier=DispatchTier.STAFF),
        ),
    )
    observations: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}

    def agent_port(self, *, task, dependency_results=(), candidate_results=(), **kwargs):
        observations[task.task_name] = (
            tuple(result.task_name for result in dependency_results),
            tuple(result.task_name for result in candidate_results),
        )
        return PowWowTaskResult(task.task_name, task.role, "completed", "fixture result")

    def browser_port(self, *, task, **kwargs):
        return PowWowTaskResult(task.task_name, task.role, "completed", "browser fixture")

    monkeypatch.setattr(CliPowWowExecutor, "_run_agent_task", agent_port)
    monkeypatch.setattr(CliPowWowExecutor, "_run_browser_acceptance_task", browser_port)
    executor = CliPowWowExecutor(worktree_root=tmp_path, bench=repo_bench())
    leases = {
        group: _CodeWorktreeLease(
            group,
            WorktreeAllocation(
                str(tmp_path), str(tmp_path / group), "a" * 40, f"codex/{group}", "preserve", True
            ),
        )
        for group in ("other", "website")
    }
    results = executor._run_dependency_scheduled_tasks(
        pow_wow_id="pow",
        target_project=target,
        tasks=tasks,
        context=context,
        code_worktrees=leases,
    )
    assert all(result.status == "completed" for result in results)
    assert observations["review"] == (("browser",), ("implement",))
