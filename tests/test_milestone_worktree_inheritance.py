# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The dependency edge is a pipe: a chained milestone builds on its dependency.

The executable proof of docs/completed/milestone_worktree_inheritance_gawd.md.
Before
schema 21, `_allocate_worktree` seeded every worktree from HEAD, so a milestone
that declared `Depends on: m2` received a checkout without m2's work and the
chain could only advance through a manual operator merge per milestone
(LyricPlayer m3, WorkUnit d31b7eaebde0dcbb5bf730699e299e06).
"""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from work_unit_support import compile_acceptance_doc

from local_first_agent_os.coordination.failures import DurableFailureError
from local_first_agent_os.coordination.store import tx
from local_first_agent_os.pow_wow.executor import CliPowWowExecutor
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.execution import (
    DEPENDENCY_BASES_DIVERGED,
    MilestoneContext,
    resolve_dependency_base_commit,
)


def _git(repo_path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _scratch_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A repo where an unmerged branch carries the dependency's work.

    Returns (repo, head_sha_of_main, settled_commit_on_the_branch). The branch
    is deliberately not merged: main only advances at the CODE_MERGE gate, so
    this is exactly the state a chained milestone dispatches into.
    """

    repo_path = tmp_path / "target"
    repo_path.mkdir()
    _git(repo_path, "init", "--initial-branch=main")
    _git(repo_path, "config", "user.email", "test@example.com")
    _git(repo_path, "config", "user.name", "test")
    (repo_path / "README.md").write_text("seed\n", encoding="utf-8")
    _git(repo_path, "add", "README.md")
    _git(repo_path, "commit", "-m", "initial")
    main_sha = _git(repo_path, "rev-parse", "HEAD")
    _git(repo_path, "switch", "-c", "agent/m1-work")
    (repo_path / "m1.txt").write_text("the predecessor's work\n", encoding="utf-8")
    _git(repo_path, "add", "m1.txt")
    _git(repo_path, "commit", "-m", "m1: settled work")
    settled_sha = _git(repo_path, "rev-parse", "HEAD")
    _git(repo_path, "switch", "main")
    return repo_path, main_sha, settled_sha


def _settle_dependency(work_unit_id: str, milestone_key: str, commit_sha: str) -> None:
    """A DONE intent whose result carries the checkpoint artifact, as the drainer writes it."""

    result = json.dumps(
        {
            "run_result": {
                "status": "COMPLETED",
                "artifacts": [
                    {
                        "artifact_type": "worktree_commit_checkpoint",
                        "content": {
                            "branch_name": f"agent/{milestone_key}-work",
                            "base_head_sha": "unused",
                            "commit_sha": commit_sha,
                        },
                    }
                ],
            }
        },
        sort_keys=True,
    )
    with tx() as c:
        c.execute(
            "INSERT INTO dispatch_intents("
            "intent_id, tier, kind, prompt, source, status, created_at, completed_at, result"
            ") VALUES (?, 'senior', 'code', 'p', ?, 'DONE', 1.0, 2.0, ?)",
            (
                str(uuid.uuid4()),
                f"work_unit:{work_unit_id}:milestone_execution:{milestone_key}",
                result,
            ),
        )


def _context(work_unit_id: str, dependencies: tuple[str, ...]) -> MilestoneContext:
    compiled = compile_acceptance_doc(design_doc_id=f"inheritance_{uuid.uuid4().hex[:8]}")
    assert compiled.compiled_plan_revision_id is not None
    plan = repo.get_compiled_plan_revision(compiled.compiled_plan_revision_id).plan
    milestone = replace(plan.ordered_milestones()[0], dependencies=dependencies)
    return MilestoneContext(
        work_unit_id=work_unit_id,
        root_workflow_id=f"work-unit:{work_unit_id}",
        child_workflow_id=f"work-unit:{work_unit_id}:milestone:{milestone.stable_key}:1",
        milestone=milestone,
        attempt=1,
        design_doc_revision_id="ddr-1",
        compiled_plan_hash=plan.plan_hash(),
    )


def test_a_seeded_worktree_contains_the_dependencys_work(tmp_path: Path) -> None:
    """The pipe end to end at the executor: branch from the settled commit.

    The second allocation is the old behavior run deliberately: seeding from
    HEAD produces a checkout without the predecessor's file, which is the
    defect this feature removes and the assertion that fails on the old code.
    """

    repo_path, main_sha, settled_sha = _scratch_repo(tmp_path)
    executor = CliPowWowExecutor(worktree_root=tmp_path / "worktrees")

    seeded = executor._allocate_worktree(
        source_repo=repo_path,
        pow_wow_id="pw-seeded",
        task_name="m2",
        base_commit_sha=settled_sha,
    )
    assert seeded.head_sha == settled_sha
    assert (Path(seeded.worktree_path) / "m1.txt").exists()

    unseeded = executor._allocate_worktree(
        source_repo=repo_path,
        pow_wow_id="pw-unseeded",
        task_name="m2",
    )
    assert unseeded.head_sha == main_sha
    assert not (Path(unseeded.worktree_path) / "m1.txt").exists()


def test_a_declared_seed_the_repository_lacks_refuses(tmp_path: Path) -> None:
    """Fail closed, never fall back to HEAD: a silent wrong seed is the worst case."""

    repo_path, _, _ = _scratch_repo(tmp_path)
    executor = CliPowWowExecutor(worktree_root=tmp_path / "worktrees")
    with pytest.raises(RuntimeError, match="is not in"):
        executor._allocate_worktree(
            source_repo=repo_path,
            pow_wow_id="pw",
            task_name="m2",
            base_commit_sha="deadbeef" * 5,
        )


def test_resolution_reads_the_dependencys_settled_result(tmp_path: Path) -> None:
    _, _, settled_sha = _scratch_repo(tmp_path)
    work_unit_id = f"wu-{uuid.uuid4().hex[:8]}"
    _settle_dependency(work_unit_id, "m1", settled_sha)

    assert resolve_dependency_base_commit(_context(work_unit_id, ("m1",))) == settled_sha
    # No dependencies: seed from HEAD, exactly the old behavior.
    assert resolve_dependency_base_commit(_context(work_unit_id, ())) is None
    # A dependency that never settled contributes nothing rather than failing:
    # ordering is the lifecycle's job, this function only carries state.
    assert resolve_dependency_base_commit(_context(work_unit_id, ("m0",))) is None


def test_divergent_dependency_commits_fail_closed() -> None:
    """Fork has one parent; merging lineages belongs to the operator gate."""

    work_unit_id = f"wu-{uuid.uuid4().hex[:8]}"
    _settle_dependency(work_unit_id, "m1", "a" * 40)
    _settle_dependency(work_unit_id, "m2", "b" * 40)

    with pytest.raises(DurableFailureError) as excinfo:
        resolve_dependency_base_commit(_context(work_unit_id, ("m1", "m2")))
    assert excinfo.value.failure.error_code == DEPENDENCY_BASES_DIVERGED
    assert "a" * 40 in excinfo.value.failure.message
    assert "b" * 40 in excinfo.value.failure.message
