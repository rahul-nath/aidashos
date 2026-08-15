# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from staffing_support import repo_bench, seat_agent_name

from local_first_agent_os.constants import DEFAULT_VERIFICATION_COMMAND_TIMEOUT_SECONDS
from local_first_agent_os.coordination import (
    ClaimTask,
    CompleteExecutionLease,
    CoordinationCommand,
    CoordinationResult,
    OpenExecutionLease,
    SubmitArtifact,
    parse_coordination_result,
)
from local_first_agent_os.coordination.store import tx
from local_first_agent_os.engineering_doctrine import CURRENT_ENGINEERING_DOCTRINE
from local_first_agent_os.lifecycle_failure_harness import (
    LifecycleFailureHarness,
    LifecycleFailureScenario,
    LifecycleFault,
    LifecycleFaultAction,
    LifecycleFaultInvocation,
    LifecycleFrontierFixture,
    LifecycleProjectFixture,
    LifecycleScenarioExpected,
    LifecycleTransitionPoint,
    lifecycle_failure_harness,
)
from local_first_agent_os.pow_wow import (
    CommandRunCapture,
    DryRunPowWowExecutor,
    FakeProcessPowWowExecutor,
    PowWowExecutionContext,
    PowWowTaskSpec,
    build_agent_task_prompt,
    build_default_saga_tasks,
    persist_pow_wow_run_result,
    run_coordination_command,
)
from local_first_agent_os.progress_events import progress_event_sink
from local_first_agent_os.project_access import AccessMode, ProjectAccessPolicy
from local_first_agent_os.project_action import ProjectActionKind
from local_first_agent_os.project_center import LinkedProject
from local_first_agent_os.staffing import Bench, BenchSlot, Harness, Tier


def _run_git_command(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


_FAKE_AGENT_PREAMBLE = (
    "#!/usr/bin/env python3\n"
    "import json as _json, sys\n"
    # `codex login status` is asked before any codex-seated task spawns, so a
    # fake standing in for whichever vendor holds a seat has to answer it. Every
    # fake carries this rather than only the ones seated as codex today, because
    # which vendor a given fake plays is decided by the bench, not here.
    "if 'login' in sys.argv:\n"
    "    print('logged in')\n"
    "    raise SystemExit(0)\n"
    # `emit` writes the answer in the dialect of the seat this fake is filling.
    # `extract_agent_cli_output` reads both, so this is for the fakes' own
    # benefit: it lets one script stand in for either vendor without every test
    # knowing which one it will be spawned as.
    "def emit(text):\n"
    "    print(_json.dumps({'type': 'result', 'result': text}))\n"
)


def _seated(
    *,
    implementer: str | None = None,
    reviewer: str | None = None,
    bench: Bench | None = None,
) -> dict[str, Any]:
    """The executor kwargs that put each fake in the seat it is written for.

    The fakes here behave like an implementer (writes a file, reports a result)
    or like a reviewer (prints a verdict). Which *vendor* plays each role is
    `configs/staffing.toml`'s business and has changed twice. Binding the scripts
    to `claude_bin` and `codex_bin` by name made a dozen of these tests fail the
    day the seating swapped, for a reason none of them is about: the reviewer
    script ran as the implementer and wrote no file.

    The bench comes back with the binaries because the two are one fact. A
    binary is chosen for the seat its fake fills, and the seat is whatever the
    executor's bench says, so a caller that could set one without the other
    could - and did - hand the executor a seating that disagreed with the
    binaries. `CliPowWowExecutor` defaults its bench to `DEFAULT_BENCH`, which is
    the no-config fallback and is deliberately not equal to this repo's config,
    while `capability_gate.policy_principal` resolves an agent's vendor name to a
    seat by reading that config. Left apart, the executor spawned the fallback's
    senior and the gate judged it by the config's, so an implementer was denied
    `run_command` as the reviewer's vendor. Production has no such gap:
    `dispatcher_runner` builds the executor with `load_bench(...)`.

    Passing `bench` overrides the seating for a test that is about the bench
    itself - capacities, say - and the binaries follow it rather than the config.

    Leaving a seat's fake unnamed leaves the executor with a binary that does not
    exist, which is what the tests passing only one script already relied on.
    """

    seating = bench if bench is not None else repo_bench()
    senior = seating[Tier.SENIOR].harness

    def spawned_as(vendor: Harness) -> str:
        seated = implementer if senior is vendor else reviewer
        return seated or vendor.value

    return {
        "bench": seating,
        "claude_bin": spawned_as(Harness.CLAUDE),
        "codex_bin": spawned_as(Harness.CODEX),
    }


def _senior_vendor() -> str:
    return seat_agent_name(Tier.SENIOR)


def _staff_vendor() -> str:
    return seat_agent_name(Tier.STAFF)


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True)
    _run_git_command(["git", "init"], path)
    _run_git_command(["git", "config", "user.email", "test@example.com"], path)
    _run_git_command(["git", "config", "user.name", "Test User"], path)
    (path / "README.md").write_text("# target\n", encoding="utf-8")
    _run_git_command(["git", "add", "README.md"], path)
    _run_git_command(["git", "commit", "-m", "initial"], path)


def _target(path: Path) -> LinkedProject:
    return LinkedProject(
        id="ai_business_portfolio",
        kind="business_factory",
        path=path,
        status="active_product_repo",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="portfolio repo",
        verification_commands=[
            f'{shlex.quote(sys.executable)} -c "from pathlib import Path; '
            "print(Path('fake_agent_output.txt').read_text(encoding='utf-8'))\""
        ],
    )


def _context(target: LinkedProject) -> PowWowExecutionContext:
    return PowWowExecutionContext(
        saga_id="saga-1",
        goal="Implement the next gated portfolio task",
        directive="/saga Implement the next gated portfolio task",
        target_project_id=target.id,
        target_project_path=str(target.expanded_path),
        target_project_kind=target.kind,
        target_project_status=target.status,
        target_project_read_only=target.read_only,
        verification_commands=tuple(target.verification_commands),
        evidence_project_ids=("ai_business_portfolio_analysis",),
        memory_project_id="ai_stack_local",
    )


def test_task_prompt_points_agents_to_startup_skill(tmp_path: Path) -> None:
    target = _target(tmp_path / "repo")
    task = PowWowTaskSpec(
        task_name="review_startup",
        role="reviewer",
        description="review the current architecture",
        dispatch_kind="advisory",
    )

    prompt = build_agent_task_prompt(task, _context(target))

    assert "skills/agent-startup/SKILL.md" in prompt
    assert "advisory task only" in prompt
    assert "Do not edit files" in prompt


def test_code_task_prompt_keeps_worktree_constraint(tmp_path: Path) -> None:
    target = _target(tmp_path / "repo")
    task = PowWowTaskSpec(
        task_name="implement_startup",
        role="implementer",
        description="change the code",
        dispatch_kind="code",
    )

    prompt = build_agent_task_prompt(task, _context(target))

    assert "skills/agent-startup/SKILL.md" in prompt
    assert "assigned worktree" in prompt
    assert "minimal necessary change" in prompt


def test_senior_and_staff_prompts_include_context_discipline(tmp_path: Path) -> None:
    from local_first_agent_os.staffing import JudgmentRole, Tier

    target = _target(tmp_path / "repo")
    context = _context(target)
    senior = PowWowTaskSpec(
        task_name="senior_plan",
        role="planner",
        description="plan the work",
        judgment=JudgmentRole(name="planner", tier=Tier.SENIOR),
        dispatch_kind="advisory",
    )
    staff = PowWowTaskSpec(
        task_name="staff_review",
        role="reviewer",
        description="review the plan",
        judgment=JudgmentRole(name="reviewer", tier=Tier.STAFF),
        dispatch_kind="advisory",
    )
    junior = PowWowTaskSpec(
        task_name="junior_scan",
        role="scanner",
        description="scan the files",
        judgment=JudgmentRole(name="scanner", tier=Tier.JUNIOR),
        dispatch_kind="advisory",
    )

    senior_prompt = build_agent_task_prompt(senior, context)
    staff_prompt = build_agent_task_prompt(staff, context)
    junior_prompt = build_agent_task_prompt(junior, context)

    assert "Context/token discipline:" in senior_prompt
    assert "Context/token discipline:" in staff_prompt
    assert "Start with a focused repo audit" in senior_prompt
    assert "Do not re-litigate accepted architecture" in staff_prompt
    assert "Context/token discipline:" not in junior_prompt
    assert "Version: engineering_doctrine.v2" in senior_prompt
    assert CURRENT_ENGINEERING_DOCTRINE.sha256 in senior_prompt
    assert CURRENT_ENGINEERING_DOCTRINE.sha256 in staff_prompt
    assert "concrete violations of this contract may BLOCK approval" in staff_prompt
    assert "unsupported style preference" in staff_prompt
    assert "concrete violations of this contract may BLOCK approval" not in senior_prompt
    assert "Engineering doctrine contract:" not in junior_prompt


def test_dry_run_pow_wow_executor_returns_deterministic_plan(tmp_path: Path) -> None:
    target = LinkedProject(
        id="ai_business_portfolio",
        kind="business_factory",
        path=tmp_path / "portfolio",
        status="active_product_repo",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="portfolio repo",
        verification_commands=["pnpm check", "pnpm e2e"],
    )
    tasks = build_default_saga_tasks("Implement the next gated portfolio task", target)
    context = PowWowExecutionContext(
        saga_id="saga-1",
        goal="Implement the next gated portfolio task",
        directive="/saga Implement the next gated portfolio task",
        target_project_id=target.id,
        target_project_path=str(target.expanded_path),
        target_project_kind=target.kind,
        target_project_status=target.status,
        target_project_read_only=target.read_only,
        verification_commands=tuple(target.verification_commands),
        evidence_project_ids=("ai_business_portfolio_analysis",),
        memory_project_id="ai_stack_local",
    )

    result = DryRunPowWowExecutor().dispatch_pow_wow(
        "pow-1",
        target,
        tasks,
        context,
    )

    assert result.status == "DRY_RUN_COMPLETED"
    assert result.external_agents_started is False
    assert result.auto_merge is False
    assert result.changed_files == ()
    assert result.verification_commands == ("pnpm check", "pnpm e2e")
    assert [task.task_name for task in result.tasks] == [
        "implement_next_gated_portfolio_task",
        "review_and_verify_next_gated_portfolio_task",
    ]
    assert all(task.status == "completed" for task in result.tasks)
    json.dumps(result.to_payload())


def test_fake_process_executor_removes_worktree_and_keeps_source_repo_clean(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "target"
    _init_git_repo(repo)
    target = _target(repo)
    tasks = (
        PowWowTaskSpec(
            task_name="implement_fixture",
            role="implementation_agent",
            description="write fake output",
        ),
        PowWowTaskSpec(
            task_name="review_fixture",
            role="review_test_agent",
            description="review fake output",
        ),
    )

    result = FakeProcessPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        cleanup_policy="remove",
    ).dispatch_pow_wow("pow-1", target, tasks, _context(target))

    task_result = result.tasks[0]
    external_artifact = next(
        artifact
        for artifact in task_result.artifacts
        if artifact.artifact_type == "external_agent_run"
    )
    checkpoint = next(
        artifact
        for artifact in task_result.artifacts
        if artifact.artifact_type == "worktree_commit_checkpoint"
    )
    worktree_path = Path(external_artifact.content["worktree"]["worktree_path"])

    assert result.status == "COMPLETED"
    assert [task.status for task in result.tasks] == ["completed", "planned"]
    assert result.external_agents_started is True
    assert result.auto_merge is False
    assert task_result.changed_files == ("fake_agent_output.txt",)
    assert external_artifact.content["command"]["exit_code"] == 0
    assert "fake external agent wrote" in external_artifact.content["command"]["stdout"]
    assert external_artifact.content["command"]["cwd"] == str(worktree_path)
    assert external_artifact.content["verification"][0]["exit_code"] == 0
    assert checkpoint.content["branch_name"].startswith("agent/pow-1-implement_fixture-")
    assert checkpoint.content["commit_created"] is True
    assert checkpoint.content["checkpointed_files"] == ("fake_agent_output.txt",)
    assert not worktree_path.exists()
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", checkpoint.content["branch_name"]],
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
        == checkpoint.content["commit_sha"]
    )
    assert not (repo / "fake_agent_output.txt").exists()
    assert (repo / "README.md").read_text(encoding="utf-8") == "# target\n"


def test_fake_process_executor_exposes_the_post_commit_boundary(tmp_path: Path) -> None:
    repo = tmp_path / "target"
    _init_git_repo(repo)
    target = _target(repo)
    task = PowWowTaskSpec(
        task_name="implement_failure_boundary",
        role="implementation_agent",
        description="write fake output",
    )
    scenario = LifecycleFailureScenario(
        name="observe-post-commit-boundary",
        project_fixture=LifecycleProjectFixture.DISPOSABLE_GIT_REPO,
        frontier_fixture=LifecycleFrontierFixture.FAKE_CLAUDE_THEN_FAKE_CODEX,
        faults=(
            LifecycleFault(
                at=LifecycleTransitionPoint.AFTER_CHECKPOINT_GIT_COMMIT,
                action=LifecycleFaultAction.DROP_DATABASE_CONNECTION,
            ),
        ),
        restart=False,
        expected=LifecycleScenarioExpected(
            action_state=ProjectActionKind.WORKING,
            preserved_commit=True,
            preserved_findings=False,
            duplicate_intents=0,
            merge_performed=False,
            next_action="continue_test",
        ),
    )
    seen: list[LifecycleFaultInvocation] = []
    harness = LifecycleFailureHarness(
        scenario,
        {LifecycleFaultAction.DROP_DATABASE_CONNECTION: seen.append},
    )

    with lifecycle_failure_harness(harness):
        result = FakeProcessPowWowExecutor(
            worktree_root=tmp_path / "worktrees",
            cleanup_policy="remove",
        ).dispatch_pow_wow("pow-boundary", target, (task,), _context(target))

    harness.assert_all_faults_triggered()
    checkpoint = next(
        artifact
        for artifact in result.tasks[0].artifacts
        if artifact.artifact_type == "worktree_commit_checkpoint"
    )
    assert seen[0].transition.facts["commit_sha"] == checkpoint.content["commit_sha"]
    assert seen[0].transition.facts["base_head_sha"] == checkpoint.content["base_head_sha"]


def test_fake_process_executor_can_preserve_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "target"
    _init_git_repo(repo)
    target = _target(repo)
    task = PowWowTaskSpec(
        task_name="implement_preserved_fixture",
        role="implementation_agent",
        description="write fake output",
    )

    result = FakeProcessPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        cleanup_policy="preserve",
    ).dispatch_pow_wow("pow-preserve", target, (task,), _context(target))

    external_artifact = next(
        artifact
        for artifact in result.tasks[0].artifacts
        if artifact.artifact_type == "external_agent_run"
    )
    worktree_path = Path(external_artifact.content["worktree"]["worktree_path"])
    try:
        assert worktree_path.exists()
        assert (worktree_path / "fake_agent_output.txt").exists()
        assert not (repo / "fake_agent_output.txt").exists()
        assert "Worktree preserved" in result.tasks[0].risks[0]
        assert external_artifact.content["worktree"]["preserved"] is True
    finally:
        _run_git_command(["git", "worktree", "remove", "--force", str(worktree_path)], repo)


def test_fake_process_run_persists_external_artifacts_to_ledger(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "target"
    _init_git_repo(repo)
    target = _target(repo)
    context = _context(target)
    task = PowWowTaskSpec(
        task_name="implement_persisted_fixture",
        role="implementation_agent",
        description="write fake output",
    )
    monkeypatch.setenv("AGENT_COORDINATION_ROOT", str(tmp_path / "coord-root"))
    saga = run_coordination_command(["create_saga", context.goal])
    pow_wow = run_coordination_command(
        [
            "create_pow_wow",
            saga["saga_id"],
            "IMPLEMENTATION",
            context.goal,
            "--exit-criteria",
            "capture fake external run",
        ]
    )
    task_record = run_coordination_command(
        [
            "claim_task",
            pow_wow["pow_wow_id"],
            task.task_name,
            task.description,
        ]
    )
    result = FakeProcessPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        cleanup_policy="remove",
    ).dispatch_pow_wow(pow_wow["pow_wow_id"], target, (task,), context)

    persist_pow_wow_run_result(
        pow_wow["pow_wow_id"],
        {task.task_name: task_record["task_id"]},
        result,
    )

    with tx() as conn:
        artifacts = [
            dict(row)
            for row in conn.execute(
                """
                SELECT artifact_type, task_id, content
                FROM task_artifacts
                ORDER BY created_at
                """
            ).fetchall()
        ]
        task_status = dict(
            conn.execute(
                "SELECT status FROM saga_tasks WHERE task_id = ?",
                (task_record["task_id"],),
            ).fetchone()
        )["status"]

    by_type = {row["artifact_type"]: row for row in artifacts}
    external = json.loads(by_type["external_agent_run"]["content"])

    assert task_status == "COMPLETED"
    assert by_type["worktree_allocation"]["task_id"] == task_record["task_id"]
    assert by_type["external_agent_run"]["task_id"] == task_record["task_id"]
    assert "pow_wow_dispatch_summary" in by_type
    assert external["content"]["command"]["exit_code"] == 0
    assert external["content"]["changed_files"] == ["fake_agent_output.txt"]
    assert external["content"]["verification"][0]["exit_code"] == 0


def test_large_artifact_uses_file_transport_and_is_persisted(tmp_path: Path) -> None:
    root = tmp_path / "coord-root"
    saga = run_coordination_command(["create_saga", "Large artifact transport"], root=root)
    pow_wow = run_coordination_command(
        ["create_pow_wow", saga["saga_id"], "IMPLEMENTATION", "Persist a large artifact"],
        root=root,
    )
    content = "x" * (70 * 1024)

    submitted = run_coordination_command(
        ["submit_artifact", pow_wow["pow_wow_id"], "large_test", content],
        root=root,
    )
    artifact = run_coordination_command(["get_artifact", submitted["artifact_id"]], root=root)

    assert artifact["artifact"]["content"] == content
    assert not list((root / ".agent_coordination").glob("coordination-payload-*.txt"))


def test_failed_run_records_task_failure_in_ledger(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "target"
    _init_git_repo(repo)
    target = _target(repo)
    context = _context(target)
    task = PowWowTaskSpec(
        task_name="implement_failing_fixture",
        role="implementation_agent",
        description="write fake output but fail verification",
    )
    monkeypatch.setenv("AGENT_COORDINATION_ROOT", str(tmp_path / "coord-root"))
    saga = run_coordination_command(["create_saga", context.goal])
    pow_wow = run_coordination_command(
        [
            "create_pow_wow",
            saga["saga_id"],
            "IMPLEMENTATION",
            context.goal,
            "--exit-criteria",
            "capture failed external run",
        ]
    )
    task_record = run_coordination_command(
        [
            "claim_task",
            pow_wow["pow_wow_id"],
            task.task_name,
            task.description,
        ]
    )
    result = FakeProcessPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        cleanup_policy="remove",
        verification_commands=(f'{sys.executable} -c "import sys; sys.exit(7)"',),
    ).dispatch_pow_wow(pow_wow["pow_wow_id"], target, (task,), context)

    events = persist_pow_wow_run_result(
        pow_wow["pow_wow_id"],
        {task.task_name: task_record["task_id"]},
        result,
    )

    with tx() as conn:
        row = dict(
            conn.execute(
                "SELECT status, retry_count FROM saga_tasks WHERE task_id = ?",
                (task_record["task_id"],),
            ).fetchone()
        )
    task_status, retry_count = row["status"], row["retry_count"]

    assert result.status == "VERIFICATION_FAILED"
    assert result.tasks[0].status == "failed"
    assert task_status == "PENDING"
    assert retry_count == 1
    fail_events = [event for event in events if event.get("reason")]
    assert fail_events and fail_events[0]["reason"].startswith("failed: ")


def test_junior_task_routes_through_delegate_not_frontier_cli(tmp_path: Path) -> None:
    from local_first_agent_os.pow_wow import CliPowWowExecutor
    from local_first_agent_os.staffing import JudgmentRole, Tier

    repo = tmp_path / "target"
    _init_git_repo(repo)
    target = _target(repo)
    calls: list[dict] = []

    def fake_delegate(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "output": "PONG", "metadata": {"adapter": "local_llama"}}

    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        # binaries that would fail loudly if a junior task ever hit a frontier CLI
        claude_bin="definitely-not-claude",
        codex_bin="definitely-not-codex",
        delegate_fn=fake_delegate,
    )
    junior = PowWowTaskSpec(
        task_name="ocr_the_thing",
        role="junior",
        description="run OCR on the provided file",
        judgment=JudgmentRole(name="junior", tier=Tier.JUNIOR),
    )
    result = executor.dispatch_pow_wow("pow-junior", target, (junior,), _context(target))

    # delegate was called once with the junior model; no frontier CLI was launched
    assert len(calls) == 1
    assert calls[0]["tier"] == "junior"
    assert calls[0]["model"] == "gemma4"
    task_result = result.tasks[0]
    assert task_result.status == "completed"
    art = task_result.artifacts[0]
    assert art.artifact_type == "delegated_task_run"
    assert art.content["output"] == "PONG"
    assert art.content["mode"] == "delegate"
    # no worktree allocated for a delegate-only pow-wow
    assert not any(a.artifact_type == "worktree_allocation" for a in task_result.artifacts)
    assert result.status == "COMPLETED"


def test_changed_files_excludes_scratch_and_ephemeral_build_output(tmp_path: Path) -> None:
    from local_first_agent_os.pow_wow import build_worktree_code_patch, list_changed_worktree_files

    repo = tmp_path / "wt"
    _init_git_repo(repo)
    (repo / ".gitignore").write_text(
        ".codex-tmp/\n.next/\nnode_modules/\n",
        encoding="utf-8",
    )
    _run_git_command(["git", "add", ".gitignore"], repo)
    _run_git_command(["git", "commit", "-m", "ignore build output"], repo)
    # the real work
    (repo / "NEXT_STEP.md").write_text("- add feature X\n", encoding="utf-8")
    # codex dumping its home dir into the worktree
    scratch = repo / ".codex-tmp" / "codex-home-abc"
    scratch.mkdir(parents=True)
    (scratch / "auth.json").write_text("{}", encoding="utf-8")
    (scratch / "config.toml").write_text("x=1", encoding="utf-8")
    (repo / ".next").mkdir()
    (repo / ".next" / "build-manifest.json").write_text("{}", encoding="utf-8")
    installed = repo / "node_modules" / "fixture"
    installed.mkdir(parents=True)
    (installed / "index.js").write_text("export {};\n", encoding="utf-8")

    changed = list_changed_worktree_files(repo)
    assert changed == ("NEXT_STEP.md",)  # scratch/build output excluded, source kept
    head_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    patch = build_worktree_code_patch(repo, group="test", head_sha=head_sha)["patch"]
    assert "NEXT_STEP.md" in patch
    assert "node_modules" not in patch
    assert ".next" not in patch


def test_project_environment_uses_exact_nvmrc_node(tmp_path: Path) -> None:
    from local_first_agent_os.toolchains import project_environment

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".nvmrc").write_text("22.19.0\n", encoding="utf-8")
    nvm_dir = tmp_path / "nvm"
    node_bin = nvm_dir / "versions" / "node" / "v22.19.0" / "bin"
    node_bin.mkdir(parents=True)
    (node_bin / "node").write_text("", encoding="utf-8")

    env = project_environment(
        repo,
        {"NVM_DIR": str(nvm_dir), "PATH": "/usr/bin"},
    )

    assert env["PATH"].split(":", 1)[0] == str(node_bin)
    assert env["LOCAL_AGENT_NODE_VERSION"] == "22.19.0"


def test_cli_executor_runs_claude_and_codex_directly(tmp_path: Path) -> None:
    import os

    from local_first_agent_os.pow_wow import CliPowWowExecutor
    from local_first_agent_os.staffing import JudgmentRole, Tier

    repo = tmp_path / "target"
    _init_git_repo(repo)
    target = _target(repo)
    claude = tmp_path / "fake_claude.py"
    claude.write_text(
        _FAKE_AGENT_PREAMBLE + "import os, sys, json\n"
        "from pathlib import Path\n"
        "prompt = sys.argv[-1]\n"
        "if 'reate a file' in prompt:\n"
        "    Path('NEXT_STEP.md').write_text('- add feature X\\n')\n"
        "assigned = os.environ.get('LOCAL_AGENT_ASSIGNED_WORKTREE', '')\n"
        "payload = {'type':'result','result':'created NEXT_STEP.md; assigned=' + assigned}\n"
        "emit(payload['result'])\n",
        encoding="utf-8",
    )
    os.chmod(claude, 0o755)
    codex = tmp_path / "fake_codex.py"
    codex.write_text(
        _FAKE_AGENT_PREAMBLE + "import sys\n"
        "emit('VERDICT: APPROVE - NEXT_STEP.md present with one bullet')\n",
        encoding="utf-8",
    )
    os.chmod(codex, 0o755)

    tasks = (
        PowWowTaskSpec(
            task_name="implement_next_step",
            role="implementer",
            judgment=JudgmentRole(name="implementer", tier=Tier.SENIOR),
            dispatch_kind="code",
            worktree_group="default",
            description="Create a file NEXT_STEP.md with one bullet.",
        ),
        PowWowTaskSpec(
            task_name="review_next_step",
            role="reviewer",
            judgment=JudgmentRole(name="reviewer", tier=Tier.STAFF, stance="evaluator"),
            dispatch_kind="code",
            blocked_by=("implement_next_step",),
            worktree_group="default",
            description="Review the change and give a one-line verdict.",
        ),
    )
    result = CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        **_seated(implementer=str(claude), reviewer=str(codex)),
        verification_commands=("test -f NEXT_STEP.md",),
    ).dispatch_pow_wow("pow-cli", target, tasks, _context(target))

    impl = next(t for t in result.tasks if t.task_name == "implement_next_step")
    rev = next(t for t in result.tasks if t.task_name == "review_next_step")
    implementer_run_capture = next(
        a.content for a in impl.artifacts if a.artifact_type == "cli_agent_run"
    )
    reviewer_run_capture = next(
        a.content for a in rev.artifacts if a.artifact_type == "cli_agent_run"
    )

    assert result.status == "COMPLETED"
    assert impl.status == "completed" and implementer_run_capture["harness"] == _senior_vendor()
    assert implementer_run_capture["changed_files"] == ["NEXT_STEP.md"]
    assert implementer_run_capture["is_review"] is False
    assert (
        implementer_run_capture["worktree"]["worktree_path"]
        in implementer_run_capture["command"]["command"]
    )
    assert str(repo) not in implementer_run_capture["command"]["command"]
    assert implementer_run_capture["worktree"]["worktree_path"] in (
        implementer_run_capture["output"] or ""
    )
    checkpoint = next(
        artifact
        for artifact in impl.artifacts
        if artifact.artifact_type == "worktree_commit_checkpoint"
    )
    assert checkpoint.content["commit_created"] is True
    assert checkpoint.content["branch_name"].startswith("agent/pow-cli-default-")
    assert checkpoint.content["checkpointed_files"] == ("NEXT_STEP.md",)
    assert rev.status == "completed" and reviewer_run_capture["harness"] == _staff_vendor()
    assert reviewer_run_capture["is_review"] is True
    assert "APPROVE" in (reviewer_run_capture["verdict"] or "")
    # implementer and reviewer shared one worktree
    assert (
        reviewer_run_capture["worktree"]["worktree_path"]
        == implementer_run_capture["worktree"]["worktree_path"]
    )
    patch = next(
        artifact for artifact in result.artifacts if artifact.artifact_type == "code_patch"
    )
    assert patch.schema_version == "code_patch.v2"
    assert patch.content["branch_name"] == checkpoint.content["branch_name"]
    assert patch.content["commit_sha"] == checkpoint.content["commit_sha"]
    assert "NEXT_STEP.md" in patch.content["patch"]
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", checkpoint.content["branch_name"]],
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
        == checkpoint.content["commit_sha"]
    )
    assert not (repo / "NEXT_STEP.md").exists()  # source repo untouched


def test_cli_executor_records_harness_commit_and_keeps_merge_gate_signal(tmp_path: Path) -> None:
    import os

    from local_first_agent_os.pow_wow import CliPowWowExecutor
    from local_first_agent_os.staffing import JudgmentRole, Tier

    repo = tmp_path / "target"
    _init_git_repo(repo)
    base_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()
    claude = tmp_path / "committing_claude.py"
    claude.write_text(
        _FAKE_AGENT_PREAMBLE + "import json, subprocess\n"
        "from pathlib import Path\n"
        "Path('DIRECT_COMMIT.md').write_text('checkpointed by harness\\n', encoding='utf-8')\n"
        "subprocess.run(['git', 'add', 'DIRECT_COMMIT.md'], check=True)\n"
        "subprocess.run(['git', 'commit', '-m', 'harness checkpoint'], check=True)\n"
        "emit('created a direct commit')\n",
        encoding="utf-8",
    )
    os.chmod(claude, 0o755)
    task = PowWowTaskSpec(
        task_name="implement_direct_commit",
        role="implementer",
        judgment=JudgmentRole(name="implementer", tier=Tier.SENIOR),
        dispatch_kind="code",
        description="Create DIRECT_COMMIT.md and commit it.",
    )

    result = CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        **_seated(implementer=str(claude)),
        verification_commands=("test -f DIRECT_COMMIT.md",),
    ).dispatch_pow_wow("pow-direct-commit", _target(repo), (task,), _context(_target(repo)))

    task_result = result.tasks[0]
    checkpoint = next(
        artifact
        for artifact in task_result.artifacts
        if artifact.artifact_type == "worktree_commit_checkpoint"
    )
    assert result.status == "COMPLETED"
    assert result.changed_files == ("DIRECT_COMMIT.md",)
    assert checkpoint.content["commit_created"] is False
    assert checkpoint.content["changed_from_base"] is True
    assert checkpoint.content["commit_sha"]
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
        == base_sha
    )
    assert not (repo / "DIRECT_COMMIT.md").exists()


def test_cli_executor_advisory_runs_without_worktree(tmp_path: Path) -> None:
    import os

    from local_first_agent_os.pow_wow import CliPowWowExecutor
    from local_first_agent_os.staffing import JudgmentRole, Tier

    repo = tmp_path / "target"
    _init_git_repo(repo)
    target = _target(repo)
    claude = tmp_path / "fake_claude.py"
    claude.write_text(
        _FAKE_AGENT_PREAMBLE + "import json, sys\n"
        "print(json.dumps({'type':'result','result':'advisory answer', 'argv': sys.argv[1:]}))\n",
        encoding="utf-8",
    )
    os.chmod(claude, 0o755)

    task = PowWowTaskSpec(
        task_name="advise_next_step",
        role="advisor",
        judgment=JudgmentRole(name="advisor", tier=Tier.SENIOR),
        description="Answer without modifying files.",
    )
    context = replace(_context(target), dispatch_kind="advisory")
    result = CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        **_seated(implementer=str(claude)),
    ).dispatch_pow_wow("pow-advisory", target, (task,), context)

    run = next(a.content for a in result.tasks[0].artifacts if a.artifact_type == "cli_agent_run")
    assert result.status == "COMPLETED"
    assert result.changed_files == ()
    assert run["dispatch_kind"] == "advisory"
    assert run["worktree"] is None
    assert run["changed_files"] == []
    assert "--dangerously-skip-permissions" not in run["command"]["command"]
    assert not (tmp_path / "wt").exists()


def test_cli_executor_records_execution_lease_dirty_worktree(tmp_path: Path) -> None:
    import os

    from local_first_agent_os.pow_wow import CliPowWowExecutor
    from local_first_agent_os.staffing import JudgmentRole, Tier

    repo = tmp_path / "target"
    _init_git_repo(repo)
    # A passing no-op rather than an empty list. This test is about the execution
    # lease's dirty-worktree payload, and an empty list now refuses to certify the
    # run at all - which is `pow_wow.verification`'s whole point and would make
    # this assert the wrong thing.
    target = replace(
        _target(repo),
        verification_commands=[f'{shlex.quote(sys.executable)} -c "pass"'],
    )
    claude = tmp_path / "fake_claude.py"
    claude.write_text(
        _FAKE_AGENT_PREAMBLE + "import json\n"
        "from pathlib import Path\n"
        "Path('CREATED.txt').write_text('created by fake claude\\n', encoding='utf-8')\n"
        "emit('created CREATED.txt')\n",
        encoding="utf-8",
    )
    os.chmod(claude, 0o755)
    calls: list[CoordinationCommand] = []
    completions: list[dict[str, object]] = []

    def fake_coordination_command(command: CoordinationCommand) -> CoordinationResult:
        calls.append(command)
        if isinstance(command, OpenExecutionLease):
            return parse_coordination_result(
                command,
                {
                    "ok": True,
                    "created": True,
                    "lease": {
                        "lease_id": "lease-dirty",
                        "status": "ACTIVE",
                        "result": {},
                    },
                },
            )
        if isinstance(command, CompleteExecutionLease):
            payload = dict(command.result or {})
            completion = {
                "status": command.status.value,
                "payload": payload,
                "error": command.error,
            }
            completions.append(completion)
            return parse_coordination_result(
                command,
                {
                    "ok": True,
                    "lease": {
                        "lease_id": command.lease_id,
                        "status": command.status.value,
                        "result": payload,
                    },
                },
            )
        raise AssertionError(f"unexpected coordination command: {command}")

    task = PowWowTaskSpec(
        task_name="implement_created_file",
        role="implementer",
        judgment=JudgmentRole(name="implementer", tier=Tier.SENIOR),
        dispatch_kind="code",
        description="Create CREATED.txt.",
    )
    result = CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        **_seated(implementer=str(claude)),
        coordination_command=fake_coordination_command,
    ).dispatch_pow_wow("pow-lease-dirty", target, (task,), _context(target))

    assert result.status == "COMPLETED"
    open_command = next(command for command in calls if isinstance(command, OpenExecutionLease))
    compensation = open_command.compensation
    assert compensation is not None
    assert compensation["strategy"] == "remove_or_reset_leased_worktree"
    assert compensation["auto_reverse_patch"] is False
    assert compensation["engineering_doctrine"] == (
        CURRENT_ENGINEERING_DOCTRINE.provenance_payload()
    )
    assert len(completions) == 1
    assert completions[0]["status"] == "COMPLETED"
    payload = completions[0]["payload"]
    assert isinstance(payload, dict)
    dirty = payload["dirty_worktree"]
    assert dirty["changed_files"] == ["CREATED.txt"]
    assert dirty["cleanup_policy"] == "remove"
    assert dirty["cleanup_deferred"] is True
    assert dirty["cleanup_applied"] is False
    assert "CREATED.txt" in dirty["diff_summary"]["changed_files"]
    run = next(
        artifact.content
        for artifact in result.tasks[0].artifacts
        if artifact.artifact_type == "cli_agent_run"
    )
    assert run["execution_lease"]["lease_id"] == "lease-dirty"
    assert run["execution_lease"]["complete_status"] == "COMPLETED"


def test_cli_executor_records_usage_limit_execution_lease(tmp_path: Path) -> None:
    import os

    from local_first_agent_os.pow_wow import CliPowWowExecutor
    from local_first_agent_os.staffing import JudgmentRole, Tier

    repo = tmp_path / "target"
    _init_git_repo(repo)
    target = _target(repo)
    claude = tmp_path / "fake_claude.py"
    claude.write_text(
        _FAKE_AGENT_PREAMBLE + "import sys\n"
        "print('usage limit reached', file=sys.stderr)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    os.chmod(claude, 0o755)
    codex = tmp_path / "fake_codex.py"
    codex.write_text(
        _FAKE_AGENT_PREAMBLE + "import sys\n"
        "print('alternate provider failed', file=sys.stderr)\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    os.chmod(codex, 0o755)
    completions: list[dict[str, object]] = []

    def fake_coordination_command(command: CoordinationCommand) -> CoordinationResult:
        if isinstance(command, OpenExecutionLease):
            return parse_coordination_result(
                command,
                {
                    "ok": True,
                    "created": True,
                    "lease": {
                        "lease_id": "lease-usage",
                        "status": "ACTIVE",
                        "result": {},
                    },
                },
            )
        if isinstance(command, CompleteExecutionLease):
            payload = dict(command.result or {})
            completion = {
                "status": command.status.value,
                "payload": payload,
                "error": command.error,
            }
            completions.append(completion)
            return parse_coordination_result(
                command,
                {
                    "ok": True,
                    "lease": {
                        "lease_id": command.lease_id,
                        "status": command.status.value,
                        "result": payload,
                    },
                },
            )
        raise AssertionError(f"unexpected coordination command: {command}")

    task = PowWowTaskSpec(
        task_name="senior_limited",
        role="advisor",
        judgment=JudgmentRole(name="advisor", tier=Tier.SENIOR),
        dispatch_kind="advisory",
        description="Advise until usage limit.",
    )
    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        **_seated(implementer=str(claude), reviewer=str(codex)),
        coordination_command=fake_coordination_command,
    )
    executor._codex_auth_ok_cache = True
    run = executor.dispatch_pow_wow(
        "pow-lease-usage",
        target,
        (task,),
        replace(_context(target), dispatch_kind="advisory"),
    )

    assert run.status == "FAILED"
    assert len(completions) == 2
    primary = next(item for item in completions if item["error"] == "usage_limit")
    assert primary["status"] == "FAILED"
    payload = primary["payload"]
    assert isinstance(payload, dict)
    assert payload["failure_reason"] == "usage_limit"
    assert payload["next_action"] == "SWITCH_TO_FALLBACK"
    assert payload["replacement_policy"] == "other_frontier_provider"


def test_cli_executor_fans_out_advisory_senior_and_staff_by_capacity(
    tmp_path: Path,
) -> None:
    import os

    from local_first_agent_os.pow_wow import CliPowWowExecutor
    from local_first_agent_os.staffing import JudgmentRole, Tier

    repo = tmp_path / "target"
    _init_git_repo(repo)
    target = _target(repo)
    events = tmp_path / "events.jsonl"

    def _fake_cli(path: Path, *, codex: bool) -> None:
        path.write_text(
            _FAKE_AGENT_PREAMBLE + "import json, sys, time\n"
            "from pathlib import Path\n"
            f"events = Path({str(events)!r})\n"
            "label = Path(sys.argv[0]).stem\n"
            "start = time.monotonic()\n"
            "with events.open('a', encoding='utf-8') as fh:\n"
            "    fh.write(json.dumps({'label': label, 'event': 'start', 't': start}) + '\\n')\n"
            "deadline = time.monotonic() + 1.5\n"
            "while time.monotonic() < deadline:\n"
            "    try:\n"
            "        started = sum(\n"
            "            1 for line in events.read_text(encoding='utf-8').splitlines()\n"
            "            if json.loads(line).get('event') == 'start'\n"
            "        )\n"
            "    except Exception:\n"
            "        started = 0\n"
            "    if started >= 4:\n"
            "        break\n"
            "    time.sleep(0.01)\n"
            "time.sleep(0.25)\n"
            "end = time.monotonic()\n"
            "with events.open('a', encoding='utf-8') as fh:\n"
            "    fh.write(json.dumps({'label': label, 'event': 'end', 't': end}) + '\\n')\n"
            + ("emit('VERDICT: advisory ok')\n" if codex else "emit('advisory ok')\n"),
            encoding="utf-8",
        )
        os.chmod(path, 0o755)

    claude = tmp_path / "fake_claude.py"
    codex = tmp_path / "fake_codex.py"
    _fake_cli(claude, codex=False)
    _fake_cli(codex, codex=True)
    tasks = (
        PowWowTaskSpec(
            task_name="senior_a",
            role="advisor",
            judgment=JudgmentRole(name="advisor", tier=Tier.SENIOR),
            dispatch_kind="advisory",
            description="advise",
        ),
        PowWowTaskSpec(
            task_name="senior_b",
            role="advisor",
            judgment=JudgmentRole(name="advisor", tier=Tier.SENIOR),
            dispatch_kind="advisory",
            description="advise",
        ),
        PowWowTaskSpec(
            task_name="staff_a",
            role="reviewer",
            judgment=JudgmentRole(name="reviewer", tier=Tier.STAFF),
            dispatch_kind="advisory",
            description="review",
        ),
        PowWowTaskSpec(
            task_name="staff_b",
            role="reviewer",
            judgment=JudgmentRole(name="reviewer", tier=Tier.STAFF),
            dispatch_kind="advisory",
            description="review",
        ),
    )
    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        # Capacity is the whole subject here, so this widens the repo's own
        # seating to two frontier seats rather than naming vendors: which vendor
        # holds which seat does not change how many run at once.
        **_seated(
            implementer=str(claude),
            reviewer=str(codex),
            bench={
                Tier.SENIOR: replace(repo_bench()[Tier.SENIOR], capacity=2),
                Tier.STAFF: replace(repo_bench()[Tier.STAFF], capacity=2),
                Tier.JUNIOR: BenchSlot(harness=Harness.PI, model="gemma4", capacity=4),
            },
        ),
    )
    executor._codex_auth_ok_cache = True

    result = executor.dispatch_pow_wow(
        "pow-parallel-advisory",
        target,
        tasks,
        replace(_context(target), dispatch_kind="advisory"),
    )

    timeline = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    active = 0
    max_active = 0
    for event in sorted(timeline, key=lambda item: (item["t"], item["event"] == "start")):
        active += 1 if event["event"] == "start" else -1
        max_active = max(max_active, active)
    assert result.status == "COMPLETED"
    assert sum(1 for task in result.tasks if task.status == "completed") == 4
    assert max_active >= 3
    assert not (tmp_path / "wt").exists()


def test_frontier_usage_limit_falls_back_to_other_frontier_provider(tmp_path: Path) -> None:
    import os

    from local_first_agent_os.pow_wow import CliPowWowExecutor
    from local_first_agent_os.staffing import JudgmentRole, Tier

    repo = tmp_path / "target"
    _init_git_repo(repo)
    target = _target(repo)
    claude = tmp_path / "fake_claude.py"
    claude.write_text(
        _FAKE_AGENT_PREAMBLE + "import json, sys\n"
        "prompt = sys.argv[-1]\n"
        "if 'cross-provider replacement' in prompt:\n"
        "    print(json.dumps({'result': 'claude fallback completed'}))\n"
        "else:\n"
        '    print("You\'ve hit your session limit", file=sys.stderr)\n'
        "    raise SystemExit(1)\n",
        encoding="utf-8",
    )
    os.chmod(claude, 0o755)
    codex = tmp_path / "fake_codex.py"
    codex.write_text(
        _FAKE_AGENT_PREAMBLE + "import sys\n"
        "prompt = sys.argv[-1]\n"
        "if 'cross-provider replacement' in prompt:\n"
        "    print('codex fallback completed')\n"
        "else:\n"
        "    print('usage limit hit for codex', file=sys.stderr)\n"
        "    raise SystemExit(1)\n",
        encoding="utf-8",
    )
    os.chmod(codex, 0o755)
    calls: list[dict] = []

    def fake_delegate(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "output": f"fallback:{kwargs['task_name']}", "metadata": {}}

    tasks = (
        PowWowTaskSpec(
            task_name="senior_limited",
            role="advisor",
            judgment=JudgmentRole(name="advisor", tier=Tier.SENIOR),
            dispatch_kind="advisory",
            description="do senior work",
        ),
        PowWowTaskSpec(
            task_name="staff_limited",
            role="reviewer",
            judgment=JudgmentRole(name="reviewer", tier=Tier.STAFF),
            dispatch_kind="advisory",
            description="do staff work",
        ),
    )
    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        **_seated(implementer=str(claude), reviewer=str(codex)),
        delegate_fn=fake_delegate,
    )
    executor._codex_auth_ok_cache = True

    result = executor.dispatch_pow_wow(
        "pow-fallback",
        target,
        tasks,
        replace(_context(target), dispatch_kind="advisory"),
    )

    assert result.status == "COMPLETED"
    assert calls == []
    assert all(task.status == "completed" for task in result.tasks)
    fallback_runs = [
        next(
            artifact.content
            for artifact in task.artifacts
            if artifact.artifact_type == "frontier_fallback_run"
        )
        for task in result.tasks
    ]
    assert {run["fallback_harness"] for run in fallback_runs} == {"claude", "codex"}
    assert all(run["schema_version"] == "frontier_fallback_run.v2" for run in fallback_runs)


def test_frontier_timeout_uses_other_provider_once(tmp_path: Path) -> None:
    import os

    from local_first_agent_os.pow_wow import CliPowWowExecutor
    from local_first_agent_os.staffing import JudgmentRole, Tier

    repo = tmp_path / "target"
    _init_git_repo(repo)
    target = _target(repo)
    claude = tmp_path / "fake_claude.py"
    claude.write_text(
        _FAKE_AGENT_PREAMBLE + "import json\n"
        "print(json.dumps({'result': 'claude replaced timed-out codex'}))\n",
        encoding="utf-8",
    )
    os.chmod(claude, 0o755)
    codex = tmp_path / "fake_codex.py"
    codex.write_text(
        _FAKE_AGENT_PREAMBLE + "import time\ntime.sleep(2)\n",
        encoding="utf-8",
    )
    os.chmod(codex, 0o755)
    calls: list[dict] = []

    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        **_seated(implementer=str(claude), reviewer=str(codex)),
        delegate_fn=lambda **kwargs: calls.append(kwargs) or {"ok": True, "output": "junior"},
        timeout_seconds=1,
    )
    executor._codex_auth_ok_cache = True
    task = PowWowTaskSpec(
        task_name="staff_timeout",
        role="reviewer",
        judgment=JudgmentRole(name="reviewer", tier=Tier.STAFF),
        dispatch_kind="advisory",
        description="review",
    )

    result = executor.dispatch_pow_wow(
        "pow-timeout",
        target,
        (task,),
        replace(_context(target), dispatch_kind="advisory"),
    )

    assert result.status == "COMPLETED"
    assert calls == []
    fallback = next(
        artifact.content
        for artifact in result.tasks[0].artifacts
        if artifact.artifact_type == "frontier_fallback_run"
    )
    assert fallback["reason"] == "timeout"
    # The staff seat timed out, so the one bounded replacement is the other
    # vendor - which is the senior seat's, whichever way the bench is seated.
    assert fallback["failed_harness"] == _staff_vendor()
    assert fallback["fallback_harness"] == _senior_vendor()


def test_four_junior_delegates_feed_codex_reviewer(tmp_path: Path) -> None:
    import os
    import time

    from local_first_agent_os.pow_wow import CliPowWowExecutor
    from local_first_agent_os.staffing import JudgmentRole, Tier

    repo = tmp_path / "target"
    _init_git_repo(repo)
    target = _target(repo)
    codex = tmp_path / "fake_codex.py"
    codex.write_text(
        _FAKE_AGENT_PREAMBLE + "import sys\n"
        "prompt = sys.argv[-1]\n"
        "missing = [name for name in ('gemma_0', 'gemma_1', 'gemma_2', 'gemma_3') "
        "if f'gemma-output:{name}' not in prompt]\n"
        "if missing:\n"
        "    print(f'MISSING DEPENDENCY OUTPUTS: {missing}')\n"
        "    raise SystemExit(2)\n"
        "emit('VERDICT: APPROVE - four junior outputs reviewed')\n",
        encoding="utf-8",
    )
    os.chmod(codex, 0o755)
    lock = threading.Lock()
    state = {"active": 0, "max": 0, "calls": 0}

    def fake_delegate(**kwargs):
        with lock:
            state["active"] += 1
            state["calls"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.2)
        with lock:
            state["active"] -= 1
        return {"ok": True, "output": f"gemma-output:{kwargs['task_name']}", "metadata": {}}

    junior_names = tuple(f"gemma_{idx}" for idx in range(4))
    tasks = tuple(
        PowWowTaskSpec(
            task_name=name,
            role="junior",
            judgment=JudgmentRole(name="junior", tier=Tier.JUNIOR),
            dispatch_kind="advisory",
            description="draft one slice",
        )
        for name in junior_names
    ) + (
        PowWowTaskSpec(
            task_name="codex_review",
            role="reviewer",
            judgment=JudgmentRole(name="reviewer", tier=Tier.STAFF),
            dispatch_kind="advisory",
            blocked_by=junior_names,
            description="review the four junior drafts",
        ),
    )
    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        **_seated(reviewer=str(codex)),
        delegate_fn=fake_delegate,
    )
    executor._codex_auth_ok_cache = True

    result = executor.dispatch_pow_wow(
        "pow-four-gemma-codex",
        target,
        tasks,
        replace(_context(target), dispatch_kind="advisory"),
    )

    review = next(task for task in result.tasks if task.task_name == "codex_review")
    review_run_capture = next(
        artifact.content
        for artifact in review.artifacts
        if artifact.artifact_type == "cli_agent_run"
    )
    assert result.status == "COMPLETED"
    assert state["calls"] == 4
    assert state["max"] == 4
    assert review.status == "completed"
    assert "APPROVE" in review_run_capture["verdict"]


def test_empty_junior_delegate_output_blocks_dependent_task(tmp_path: Path) -> None:
    from local_first_agent_os.pow_wow import CliPowWowExecutor
    from local_first_agent_os.staffing import JudgmentRole, Tier

    repo = tmp_path / "target"
    _init_git_repo(repo)
    target = _target(repo)

    def empty_delegate(**_kwargs):
        return {"ok": True, "output": "", "metadata": {}}

    tasks = (
        PowWowTaskSpec(
            task_name="gemma_empty",
            role="junior",
            judgment=JudgmentRole(name="junior", tier=Tier.JUNIOR),
            dispatch_kind="advisory",
            description="draft one slice",
        ),
        PowWowTaskSpec(
            task_name="codex_review",
            role="reviewer",
            judgment=JudgmentRole(name="reviewer", tier=Tier.STAFF),
            dispatch_kind="advisory",
            blocked_by=("gemma_empty",),
            description="review the junior draft",
        ),
    )
    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        delegate_fn=empty_delegate,
    )

    result = executor.dispatch_pow_wow(
        "pow-empty-delegate",
        target,
        tasks,
        replace(_context(target), dispatch_kind="advisory"),
    )

    junior = next(task for task in result.tasks if task.task_name == "gemma_empty")
    review = next(task for task in result.tasks if task.task_name == "codex_review")
    assert result.status == "FAILED"
    assert junior.status == "failed"
    assert "empty output" in junior.risks[0]
    assert review.status == "blocked"
    assert "dependencies did not complete" in review.risks[0]


def test_junior_tasks_fan_out_concurrently(tmp_path: Path) -> None:
    import time

    from local_first_agent_os.pow_wow import CliPowWowExecutor
    from local_first_agent_os.staffing import JudgmentRole, Tier

    repo = tmp_path / "target"
    _init_git_repo(repo)
    target = _target(repo)
    lock = threading.Lock()
    state = {"active": 0, "max": 0}

    def fake_delegate(**kwargs):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.2)
        with lock:
            state["active"] -= 1
        return {"ok": True, "output": "done", "metadata": {}}

    juniors = tuple(
        PowWowTaskSpec(
            task_name=f"junior_{i}",
            role="junior",
            judgment=JudgmentRole(name="junior", tier=Tier.JUNIOR),
            description="cheap async work",
        )
        for i in range(4)
    )
    result = CliPowWowExecutor(
        worktree_root=tmp_path / "wt", delegate_fn=fake_delegate
    ).dispatch_pow_wow("pow-fan", target, juniors, _context(target))

    assert result.status == "COMPLETED"
    assert sum(1 for t in result.tasks if t.status == "completed") == 4
    # junior bench capacity is 4, so they ran in parallel, not one-by-one
    assert state["max"] >= 2


def _review_loop_target(path: Path) -> LinkedProject:
    return LinkedProject(
        id="ai_business_portfolio",
        kind="business_factory",
        path=path,
        status="active_product_repo",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="portfolio repo",
        verification_commands=['python3 -c "print(1)"'],
    )


def _review_loop_fixture(
    tmp_path: Path,
    *,
    codex_verdicts: list[str],
) -> tuple[Path, str, str, tuple[PowWowTaskSpec, PowWowTaskSpec]]:
    """A repo, fake claude/codex bins, and an implement+review task pair.

    The fake codex prints the next verdict from `codex_verdicts` on each review
    invocation (repeating the last one when exhausted), so tests can script a
    block-then-approve negotiation.
    """
    import os

    from local_first_agent_os.staffing import JudgmentRole, Tier

    repo = tmp_path / "target"
    _init_git_repo(repo)
    claude = tmp_path / "fake_claude.py"
    claude.write_text(
        _FAKE_AGENT_PREAMBLE + "import sys, json\n"
        "from pathlib import Path\n"
        "prompt = sys.argv[-1]\n"
        "if 'requested changes' in prompt:\n"
        "    Path('NEXT_STEP.md').write_text('- add feature X\\n- guardrail\\n')\n"
        "else:\n"
        "    Path('NEXT_STEP.md').write_text('- add feature X\\n')\n"
        "emit('wrote NEXT_STEP.md')\n",
        encoding="utf-8",
    )
    os.chmod(claude, 0o755)
    counter = tmp_path / "codex_review_count"
    codex = tmp_path / "fake_codex.py"
    codex.write_text(
        _FAKE_AGENT_PREAMBLE + "from pathlib import Path\n"
        f"counter = Path({str(counter)!r})\n"
        "n = int(counter.read_text()) if counter.exists() else 0\n"
        "counter.write_text(str(n + 1))\n"
        f"verdicts = {codex_verdicts!r}\n"
        "emit(verdicts[min(n, len(verdicts) - 1)])\n",
        encoding="utf-8",
    )
    os.chmod(codex, 0o755)
    tasks = (
        PowWowTaskSpec(
            task_name="implement_next_step",
            role="implementer",
            judgment=JudgmentRole(name="implementer", tier=Tier.SENIOR),
            dispatch_kind="code",
            worktree_group="default",
            description="Create a file NEXT_STEP.md with one bullet.",
        ),
        PowWowTaskSpec(
            task_name="review_next_step",
            role="reviewer",
            judgment=JudgmentRole(name="reviewer", tier=Tier.STAFF, stance="evaluator"),
            dispatch_kind="code",
            blocked_by=("implement_next_step",),
            worktree_group="default",
            description="Review the change. Start with APPROVE or BLOCK.",
        ),
    )
    return repo, str(claude), str(codex), tasks


def test_review_block_triggers_revision_loop_and_converges(tmp_path: Path) -> None:
    from local_first_agent_os.pow_wow import CliPowWowExecutor

    repo, claude_bin, codex_bin, tasks = _review_loop_fixture(
        tmp_path,
        codex_verdicts=[
            "BLOCK - the change has no guardrail bullet",
            "APPROVE - guardrail added, findings addressed",
        ],
    )
    target = _review_loop_target(repo)
    progress: list[dict[str, object]] = []
    with progress_event_sink(progress.append):
        result = CliPowWowExecutor(
            worktree_root=tmp_path / "wt",
            **_seated(implementer=claude_bin, reviewer=codex_bin),
            max_review_rounds=2,
        ).dispatch_pow_wow("pow-review-loop", target, tasks, _context(target))

    names = [task_result.task_name for task_result in result.tasks]
    assert "implement_next_step_revision_r1" in names
    assert "review_next_step_r1" in names
    assert not any(name.endswith("_unresolved") for name in names)
    assert result.status == "COMPLETED"
    initial_review = next(tr for tr in result.tasks if tr.task_name == "review_next_step")
    bounded = next(
        artifact.content
        for artifact in initial_review.artifacts
        if artifact.artifact_type == "bounded_revision_context"
    )
    review = next(
        artifact.content
        for artifact in initial_review.artifacts
        if artifact.artifact_type == "review_result"
    )
    assert bounded["schema_version"] == "bounded_revision_context.v1"
    assert bounded["target"]["base_commit_sha"] != bounded["target"]["blocked_commit_sha"]
    assert bounded["reviewer"]["reviewer_tier"] == "STAFF"
    assert bounded["reviewer_output"]["state"] == "PERSISTENCE_PENDING"
    assert "findings" not in bounded
    assert review["review_text"] == "BLOCK - the change has no guardrail bullet"
    assert bounded["verification"]["commands"] == ['python3 -c "print(1)"']
    assert bounded["remaining_approval_boundaries"][0] == {
        "boundary": "CODE_MERGE",
        "status": "REQUIRED",
        "authority": "operator",
    }
    re_review = next(tr for tr in result.tasks if tr.task_name == "review_next_step_r1")
    verdict = next(a.content["verdict"] for a in re_review.artifacts if a.content.get("verdict"))
    assert verdict.startswith("APPROVE")
    # The full reviewed patch survives worktree cleanup as a ledger artifact.
    patch = next(a for a in result.artifacts if a.artifact_type == "code_patch")
    assert patch.content["truncated"] is False
    assert "NEXT_STEP.md" in patch.content["patch"]
    assert "+- guardrail" in patch.content["patch"]
    assert any(
        event.get("phase") == "task_started" and event.get("task_name") == "review_next_step"
        for event in progress
    )
    assert any(
        event.get("phase") == "review_revision_started" and event.get("round") == 1
        for event in progress
    )


def test_block_context_is_durable_before_revision_process_starts(tmp_path: Path) -> None:
    from local_first_agent_os.pow_wow import CliPowWowExecutor
    from local_first_agent_os.pow_wow.protocol import ReviewOrigin

    repo, claude_bin, codex_bin, tasks = _review_loop_fixture(
        tmp_path,
        codex_verdicts=[
            "BLOCK - preserve this complete novel reviewer explanation",
            "APPROVE - explanation addressed",
        ],
    )
    target = _review_loop_target(repo)
    calls: list[CoordinationCommand] = []
    submitted_ids: dict[str, str] = {}

    def fake_coordination_command(command: CoordinationCommand) -> CoordinationResult:
        calls.append(command)
        if isinstance(command, OpenExecutionLease):
            return parse_coordination_result(
                command,
                {
                    "ok": True,
                    "created": True,
                    "lease": {
                        "lease_id": f"lease-{len(calls)}",
                        "status": "ACTIVE",
                        "result": {},
                    },
                },
            )
        if isinstance(command, CompleteExecutionLease):
            return parse_coordination_result(
                command,
                {
                    "ok": True,
                    "lease": {
                        "lease_id": command.lease_id,
                        "status": command.status.value,
                        "result": dict(command.result or {}),
                    },
                },
            )
        if isinstance(command, ClaimTask):
            return parse_coordination_result(
                command,
                {"ok": True, "task_id": f"task-{len(calls)}"},
            )
        if isinstance(command, SubmitArtifact):
            artifact_id = f"artifact-{len(submitted_ids) + 1}"
            submitted_ids[command.artifact_type] = artifact_id
            return parse_coordination_result(
                command,
                {"ok": True, "artifact_id": artifact_id},
            )
        raise AssertionError(f"unexpected coordination command: {command}")

    recovery_context = replace(
        _context(target),
        review_origin=ReviewOrigin.RECOVERY_STAFF,
        recovery_retained_branch="agent/original-retained",
        recovery_original_task_contract="Implement only recovery milestone M3.",
        recovery_permission_envelope="read-only review; revisions only after BLOCK",
    )
    result = CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        **_seated(implementer=claude_bin, reviewer=codex_bin),
        max_review_rounds=2,
        coordination_command=fake_coordination_command,
    ).dispatch_pow_wow("pow-review-durable", target, tasks, recovery_context)

    assert result.status == "COMPLETED"
    initial_review = next(task for task in result.tasks if task.task_name == "review_next_step")
    review_artifact = next(
        artifact
        for artifact in initial_review.artifacts
        if artifact.artifact_type == "review_result"
    )
    bounded_artifact = next(
        artifact
        for artifact in initial_review.artifacts
        if artifact.artifact_type == "bounded_revision_context"
    )
    assert review_artifact.persisted_artifact_id == submitted_ids["review_result"]
    assert bounded_artifact.persisted_artifact_id == submitted_ids["bounded_revision_context"]
    assert bounded_artifact.content["reviewer_output"] == {
        **bounded_artifact.content["reviewer_output"],
        "state": "DURABLE_ARTIFACT",
        "artifact_id": review_artifact.persisted_artifact_id,
    }
    assert bounded_artifact.content["target"]["retained_branch"] == ("agent/original-retained")
    assert bounded_artifact.content["reviewer"]["review_origin"] == "RECOVERY_STAFF"
    assert bounded_artifact.content["revision_scope"] == {
        **bounded_artifact.content["revision_scope"],
        "original_task_contract": "Implement only recovery milestone M3.",
        "permission_envelope": "read-only review; revisions only after BLOCK",
    }
    bounded_submit_index = next(
        index
        for index, command in enumerate(calls)
        if isinstance(command, SubmitArtifact)
        and command.artifact_type == "bounded_revision_context"
    )
    revision_open_index = next(
        index
        for index, command in enumerate(calls)
        if isinstance(command, OpenExecutionLease) and "revision_r1" in command.worker_id
    )
    assert bounded_submit_index < revision_open_index


def test_revision_fails_closed_when_context_cannot_be_persisted(tmp_path: Path) -> None:
    from local_first_agent_os.pow_wow import CliPowWowExecutor

    repo, claude_bin, codex_bin, tasks = _review_loop_fixture(
        tmp_path,
        codex_verdicts=["BLOCK - persistence must succeed before revision"],
    )
    target = _review_loop_target(repo)
    calls: list[CoordinationCommand] = []

    def fake_coordination_command(command: CoordinationCommand) -> CoordinationResult:
        calls.append(command)
        if isinstance(command, OpenExecutionLease):
            return parse_coordination_result(
                command,
                {
                    "ok": True,
                    "created": True,
                    "lease": {
                        "lease_id": f"lease-{len(calls)}",
                        "status": "ACTIVE",
                        "result": {},
                    },
                },
            )
        if isinstance(command, CompleteExecutionLease):
            return parse_coordination_result(
                command,
                {
                    "ok": True,
                    "lease": {
                        "lease_id": command.lease_id,
                        "status": command.status.value,
                        "result": dict(command.result or {}),
                    },
                },
            )
        if isinstance(command, SubmitArtifact):
            if command.artifact_type == "bounded_revision_context":
                raise RuntimeError("ledger unavailable")
            return parse_coordination_result(
                command,
                {"ok": True, "artifact_id": "review-result-artifact"},
            )
        raise AssertionError(f"unexpected coordination command: {command}")

    result = CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        **_seated(implementer=claude_bin, reviewer=codex_bin),
        coordination_command=fake_coordination_command,
    ).dispatch_pow_wow("pow-review-persist-failure", target, tasks, _context(target))

    assert result.status == "FAILED"
    assert any(
        "bounded revision context could not be persisted" in task.summary
        for task in result.tasks
        if task.task_name.endswith("_unresolved")
    )
    assert not any(
        isinstance(command, OpenExecutionLease) and "revision_r1" in command.worker_id
        for command in calls
    )


def test_staff_review_fails_if_reviewer_mutates_worktree(tmp_path: Path) -> None:
    from local_first_agent_os.pow_wow import CliPowWowExecutor

    repo, claude_bin, codex_bin, tasks = _review_loop_fixture(
        tmp_path,
        codex_verdicts=["APPROVE - attempted to edit during review"],
    )
    Path(codex_bin).write_text(
        _FAKE_AGENT_PREAMBLE + "import sys\n"
        "from pathlib import Path\n"
        "if 'login' in sys.argv:\n"
        "    raise SystemExit(0)\n"
        "Path('REVIEWER_EDIT.md').write_text('not allowed\\n', encoding='utf-8')\n"
        "emit('APPROVE - attempted to edit during review')\n",
        encoding="utf-8",
    )
    target = _review_loop_target(repo)

    result = CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        **_seated(implementer=claude_bin, reviewer=codex_bin),
    ).dispatch_pow_wow("pow-review-read-only", target, tasks, _context(target))

    review = next(task for task in result.tasks if task.task_name == "review_next_step")
    typed_review = next(
        artifact.content
        for artifact in review.artifacts
        if artifact.artifact_type == "review_result"
    )
    assert review.status == "failed"
    assert review.changed_files == ("REVIEWER_EDIT.md",)
    assert typed_review["completion_status"] == "FAILED"
    assert typed_review["engineering_doctrine"] == (
        CURRENT_ENGINEERING_DOCTRINE.provenance_payload()
    )
    assert any("read-only boundary" in risk for risk in review.risks)
    assert result.status == "FAILED"


def test_review_loop_fails_closed_at_round_cap(tmp_path: Path) -> None:
    from local_first_agent_os.pow_wow import CliPowWowExecutor

    repo, claude_bin, codex_bin, tasks = _review_loop_fixture(
        tmp_path,
        codex_verdicts=[
            "BLOCK - missing guardrail",
            "BLOCK - guardrail exists but still no rollback note",
        ],
    )
    target = _review_loop_target(repo)
    result = CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        **_seated(implementer=claude_bin, reviewer=codex_bin),
        max_review_rounds=1,
    ).dispatch_pow_wow("pow-review-cap", target, tasks, _context(target))

    unresolved = next(tr for tr in result.tasks if tr.task_name.endswith("_unresolved"))
    assert unresolved.status == "failed"
    assert "round cap of 1" in unresolved.summary
    assert result.status == "FAILED"


def test_review_loop_stops_when_classifier_detects_circling(tmp_path: Path) -> None:
    from local_first_agent_os.pow_wow import CliPowWowExecutor

    repo, claude_bin, codex_bin, tasks = _review_loop_fixture(
        tmp_path,
        codex_verdicts=[
            "BLOCK - naming of the bullet is unclear",
            "BLOCK - bullet naming still reads awkwardly",
            "BLOCK - consider renaming the bullet again",
        ],
    )
    classifier_calls: list[str] = []

    def fake_delegate(**kwargs):
        classifier_calls.append(kwargs["role"])
        return {"ok": True, "output": "CIRCLING\nstyle-only iteration"}

    target = _review_loop_target(repo)
    result = CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        **_seated(implementer=claude_bin, reviewer=codex_bin),
        max_review_rounds=4,
        delegate_fn=fake_delegate,
    ).dispatch_pow_wow("pow-review-circle", target, tasks, _context(target))

    names = [task_result.task_name for task_result in result.tasks]
    # The classifier stopped the loop after round 1; rounds 2-4 never ran.
    assert "implement_next_step_revision_r1" in names
    assert "implement_next_step_revision_r2" not in names
    assert classifier_calls == ["review_convergence_classifier"]
    re_review = next(tr for tr in result.tasks if tr.task_name == "review_next_step_r1")
    convergence = next(a for a in re_review.artifacts if a.artifact_type == "review_convergence")
    assert convergence.content["classification"] == "circling"
    unresolved = next(tr for tr in result.tasks if tr.task_name.endswith("_unresolved"))
    assert "circling" in unresolved.summary
    assert result.status == "FAILED"


def test_cli_progress_assessor_uses_junior_delegate_and_parses_json(tmp_path: Path) -> None:
    from local_first_agent_os.pow_wow import CliPowWowExecutor

    calls: list[dict[str, object]] = []

    def fake_delegate(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return {
            "ok": True,
            "output": (
                "```json\n"
                '{"schema_version":"execution_progress_assessment.v1",'
                '"recommendation":"SPLIT","rationale":"Two independent blockers",'
                '"continuations":["repair persistence","retry review"]}'
                "\n```"
            ),
        }

    executor = CliPowWowExecutor(worktree_root=tmp_path / "worktrees", delegate_fn=fake_delegate)
    decision = executor._assess_stalled_progress(
        {
            "schema_version": "execution_progress_evidence.v1",
            "lease_id": "lease-123",
            "recent_events": ["1:lifecycle:lease.heartbeat"],
        }
    )

    assert decision["recommendation"] == "SPLIT"
    assert decision["continuations"] == ["repair persistence", "retry review"]
    assert calls[0]["tier"] == "junior"
    assert calls[0]["role"] == "progress_assessor"
    assert "Heartbeats prove only ownership/liveness" in str(calls[0]["prompt"])


def test_a_failed_harness_run_records_what_the_harness_said() -> None:
    """An exit code alone cannot name the cause an operator has to act on.

    This is the real capture from an expired `claude` credential: the CLI
    reports the reason on its JSON stream and exits 1. Recording only the 1
    sends an operator to the loop logs to learn that the fix is one command.
    """

    from local_first_agent_os.pow_wow.executor import _harness_failure_excerpt

    capture = CommandRunCapture(
        command="claude --print",
        cwd="/repo",
        stdout='{"type":"result","subtype":"error"}',
        stderr="",
        exit_code=1,
    )

    excerpt = _harness_failure_excerpt(capture, "Not logged in · Please run /login")

    assert excerpt == "Not logged in · Please run /login"


def test_a_successful_run_contributes_no_failure_excerpt() -> None:
    from local_first_agent_os.pow_wow.executor import _harness_failure_excerpt

    capture = CommandRunCapture(
        command="claude --print",
        cwd="/repo",
        stdout="all good",
        stderr="",
        exit_code=0,
    )

    assert _harness_failure_excerpt(capture, "all good") is None


def test_the_excerpt_prefers_stderr_when_the_harness_never_started() -> None:
    """A CLI that dies before producing its own stream still explains itself."""

    from local_first_agent_os.pow_wow.executor import _harness_failure_excerpt

    capture = CommandRunCapture(
        command="codex exec",
        cwd="/repo",
        stdout="",
        stderr="codex: command not found\n",
        exit_code=127,
    )

    assert _harness_failure_excerpt(capture) == "codex: command not found"


def test_the_excerpt_is_bounded_so_agent_output_cannot_bury_the_summary() -> None:
    from local_first_agent_os.pow_wow.executor import (
        _FAILURE_EXCERPT_LIMIT,
        _harness_failure_excerpt,
    )

    capture = CommandRunCapture(
        command="claude --print",
        cwd="/repo",
        stdout="x" * 5000,
        stderr="",
        exit_code=1,
    )

    excerpt = _harness_failure_excerpt(capture)

    assert excerpt is not None
    assert excerpt.endswith("...")
    assert len(excerpt) == _FAILURE_EXCERPT_LIMIT + 3


def test_a_hanging_verification_command_fails_the_gate_on_its_own_clock(
    tmp_path: Path,
) -> None:
    """A check that has not answered is not a check, and must not eat the budget.

    Every verification call site used to pass `timeout_seconds`, the agent's own
    process cap of two hours. So a project declaring a command that hangs - a
    bare `uv run pytest` against a suite with one sleeping test is the real case
    - held a live worktree for the whole cap and reported nothing, which reads
    exactly like a working run to anyone watching.

    The two clocks measure different things: one is how long a model may think,
    the other is how long a deterministic check may take. This asserts the
    second one fires, that the gate goes red rather than hanging, and that the
    capture says which command it was and how long it was given.
    """

    from local_first_agent_os.pow_wow import CliPowWowExecutor
    from local_first_agent_os.staffing import JudgmentRole

    repo = tmp_path / "target"
    _init_git_repo(repo)
    target = _target(repo)
    agent = tmp_path / "fake_agent.py"
    agent.write_text(
        _FAKE_AGENT_PREAMBLE + "from pathlib import Path\n"
        "Path('NEXT_STEP.md').write_text('- add feature X\\n')\n"
        "emit('wrote NEXT_STEP.md')\n",
        encoding="utf-8",
    )
    os.chmod(agent, 0o755)

    task = PowWowTaskSpec(
        task_name="implement_with_a_hanging_gate",
        role="implementer",
        judgment=JudgmentRole(name="implementer", tier=Tier.SENIOR),
        dispatch_kind="code",
        description="Create NEXT_STEP.md.",
    )

    result = CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        **_seated(implementer=str(agent)),
        verification_commands=("sleep 30",),
        verification_timeout_seconds=1,
        timeout_seconds=600,
    ).dispatch_pow_wow("pow-hanging-gate", target, (task,), _context(target))

    run = next(
        artifact.content
        for artifact in result.tasks[0].artifacts
        if artifact.artifact_type == "cli_agent_run"
    )
    verification = run["verification"]

    assert verification, "the gate must record the command it gave up on"
    assert verification[0]["exit_code"] == 124, "a timed-out command is a failing command"
    assert "sleep 30" in verification[0]["command"]
    assert result.tasks[0].status == "failed"
    # The agent's own budget is untouched: the gate answered in about a second
    # against a task permitted ten minutes, which is the whole point of the split.
    assert run["command"]["exit_code"] == 0, "the agent itself succeeded; the gate is what refused"


def test_the_verification_clock_is_not_the_agents(tmp_path: Path) -> None:
    """Stated as an assertion because the defect was that they were the same one."""

    from local_first_agent_os.pow_wow import CliPowWowExecutor

    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        timeout_seconds=7200,
    )

    assert executor.verification_timeout_seconds < executor.timeout_seconds
    assert executor.verification_timeout_seconds == DEFAULT_VERIFICATION_COMMAND_TIMEOUT_SECONDS


def test_the_verification_clock_reaps_the_whole_process_group(tmp_path: Path) -> None:
    """A timed-out command must not hold the gate hostage through its orphans.

    `subprocess.run` kills only its direct child on timeout - the shell, under
    `shell=True` - and then drains the pipes, which blocks until every process
    holding them exits. So a suite that spawns anything, or lingers itself, kept
    the gate alive for as long as it pleased after the clock fired: the recorded
    incident held a gate for 26 minutes past its summary. This pins the group
    kill: a parent that exits immediately but leaves a 60-second child on the
    pipe must come back at the clock, not at the child's.
    """

    import time

    from local_first_agent_os.pow_wow.process import run_captured_shell_command

    linger = (
        f"{shlex.quote(sys.executable)} -c "
        '"import subprocess, sys; '
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "print('suite summary printed')\""
    )
    started = time.monotonic()
    capture = run_captured_shell_command(linger, tmp_path, timeout_seconds=2)
    elapsed = time.monotonic() - started

    assert elapsed < 30, f"the gate waited {elapsed:.0f}s on an orphan the clock had condemned"
    assert capture.exit_code == 124, "held-open output past the clock is a timeout, not a pass"
    assert "suite summary printed" in capture.stdout, "partial evidence must survive the reap"
    assert "timed out after 2s" in capture.stderr
