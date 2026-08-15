# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from local_first_agent_os.contracts import (
    ArtifactRole,
    SourceType,
    WorkflowStatus,
    WorkspaceId,
)
from local_first_agent_os.coordination.store import tx
from local_first_agent_os.ingress import normalize_scheduled_event
from local_first_agent_os.pow_wow.ledger import describe_coordination_ledger
from local_first_agent_os.workflow import WorkflowEngine


def _write_linked_projects(
    config_dir: Path,
    *,
    control_path: Path,
    target_path: Path,
    evidence_path: Path,
    memory_path: Path,
    target_verification_commands: list[str] | None = None,
) -> None:
    target_verification_commands = target_verification_commands or ["pnpm check", "pnpm e2e"]
    (config_dir / "linked_projects.toml").write_text(
        f"""
[center]
id = "local_first_agent_os"
description = "test center"
control_plane_project = "local_first_agent_os"
default_saga_project = "ai_business_portfolio"
default_memory_project = "ai_stack_local"

[[projects]]
id = "local_first_agent_os"
kind = "control_plane"
path = "{control_path}"
status = "active_center"
read_only = false
description = "control plane"
primary_interfaces = ["pi"]
owns = ["commands"]
avoid = ["products"]
verification_commands = ["uv run pytest"]

[[projects]]
id = "ai_business_portfolio"
kind = "business_factory"
path = "{target_path}"
status = "active_product_repo"
read_only = false
description = "business factory"
primary_interfaces = ["pnpm check"]
owns = ["products"]
avoid = ["memory"]
verification_commands = {json.dumps(target_verification_commands)}

[[projects]]
id = "ai_business_portfolio_analysis"
kind = "business_evidence"
path = "{evidence_path}"
status = "read_only_evidence"
read_only = true
description = "analysis evidence"
primary_interfaces = ["uv run python analysis/make_reports.py"]
owns = ["evidence"]
avoid = ["implementation"]
verification_commands = ["uv run python analysis/make_reports.py"]

[[projects]]
id = "ai_stack_local"
kind = "personal_memory"
path = "{memory_path}"
status = "active_memory_repo"
read_only = false
description = "memory"
primary_interfaces = ["uv run embed.py"]
owns = ["memory"]
avoid = ["products"]
verification_commands = ["uv run python -m py_compile embed.py"]
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _run_git_command(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run_git_command(["git", "init"], path)
    _run_git_command(["git", "config", "user.email", "test@example.com"], path)
    _run_git_command(["git", "config", "user.name", "Test User"], path)
    (path / "README.md").write_text("# target\n", encoding="utf-8")
    _run_git_command(["git", "add", "README.md"], path)
    _run_git_command(["git", "commit", "-m", "initial"], path)


def _directive_event(directive: str):
    return normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": directive},
    )


def test_saga_directive_creates_durable_dry_run_pow_wow(runtime, tmp_path, monkeypatch) -> None:
    control_path = tmp_path / "control"
    target_path = tmp_path / "portfolio"
    evidence_path = tmp_path / "analysis"
    memory_path = tmp_path / "memory"
    for path in (control_path, target_path, evidence_path, memory_path):
        path.mkdir()
    _write_linked_projects(
        runtime.settings.config_dir,
        control_path=control_path,
        target_path=target_path,
        evidence_path=evidence_path,
        memory_path=memory_path,
    )
    coord_root = tmp_path / "coordination-root"
    monkeypatch.delenv("AGENT_COORDINATION_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_SAGA_EXECUTOR", raising=False)
    monkeypatch.delenv("LOCAL_AGENT_SAGA_EXECUTOR_BACKEND", raising=False)
    runtime.settings.coordination_root = coord_root
    runtime.settings.saga_executor_backend = "dry_run"

    result = WorkflowEngine(runtime).model_directive(
        _directive_event(
            '/saga "Use ai-business-portfolio reports and implement the next gated portfolio task"'
        )
    )

    assert result.status == WorkflowStatus.COMPLETED
    artifact = next(item for item in result.artifacts if item.role == ArtifactRole.DIRECTIVE_RESULT)
    payload = runtime.artifact_store.read_json(artifact.artifact_id)
    assert payload["status"] == "planned"
    assert payload["target_project"]["id"] == "ai_business_portfolio"
    assert payload["target_project"]["context_files"] == []
    assert payload["evidence_projects"][0]["id"] == "ai_business_portfolio_analysis"
    assert payload["evidence_projects"][0]["context_files"] == []
    assert payload["memory_project"]["retrieval_used"] is False
    assert payload["executor_result"]["mode"] == "dry_run"
    assert payload["executor_result"]["external_agents_started"] is False
    assert payload["auto_merge"] is False
    assert payload["operator_summary"]["saga_id"] == payload["saga"]["saga_id"]
    assert payload["operator_summary"]["pow_wow_id"] == payload["pow_wow"]["pow_wow_id"]
    assert payload["operator_summary"]["executor_backend"] == "DryRunPowWowExecutor:dry_run"
    assert payload["operator_summary"]["executor_config_source"] == "runtime_settings"
    assert payload["operator_summary"]["executor_worktree_root"] is None
    assert payload["operator_summary"]["target_project"] == "ai_business_portfolio"
    assert payload["operator_summary"]["evidence_projects"] == ["ai_business_portfolio_analysis"]
    assert payload["operator_summary"]["ledger_path"] == describe_coordination_ledger(
        runtime.settings
    )
    assert payload["operator_summary"]["artifact_count"] >= 4
    assert payload["operator_summary"]["target_repos_mutated"] is False
    assert payload["merge_approval_request"] is None
    assert payload["operator_summary"]["merge_approval_id"] is None
    assert "merge_approval: none" in payload["report"]
    assert "saga_id:" in payload["report"]
    assert "ledger_path:" in payload["report"]
    assert [task["task_name"] for task in payload["tasks"]] == [
        "implement_next_gated_portfolio_task",
        "review_and_verify_next_gated_portfolio_task",
    ]

    with tx() as conn:
        saga_count = conn.execute("SELECT COUNT(*) AS count FROM sagas").fetchone()["count"]
        pow_wow = conn.execute("SELECT status, output_summary FROM pow_wows").fetchone()
        tasks = [
            (row["task_name"], row["status"])
            for row in conn.execute(
                "SELECT task_name, status FROM saga_tasks ORDER BY created_at"
            ).fetchall()
        ]
        artifact_count = conn.execute("SELECT COUNT(*) AS count FROM task_artifacts").fetchone()[
            "count"
        ]

    assert saga_count == 1
    assert pow_wow["status"] == "COMPLETED"
    assert "Dry-run pow-wow planned 2 task(s)" in pow_wow["output_summary"]
    assert tasks == [
        ("implement_next_gated_portfolio_task", "COMPLETED"),
        ("review_and_verify_next_gated_portfolio_task", "COMPLETED"),
    ]
    assert artifact_count >= 4


def test_saga_directive_fake_process_mode_captures_external_run_without_repo_mutation(
    runtime,
    tmp_path,
    monkeypatch,
) -> None:
    control_path = tmp_path / "control"
    target_path = tmp_path / "portfolio"
    evidence_path = tmp_path / "analysis"
    memory_path = tmp_path / "memory"
    for path in (control_path, evidence_path, memory_path):
        path.mkdir()
    _init_git_repo(target_path)
    verification_command = (
        f'{sys.executable} -c "from pathlib import Path; '
        "print(Path('fake_agent_output.txt').read_text(encoding='utf-8'))\""
    )
    _write_linked_projects(
        runtime.settings.config_dir,
        control_path=control_path,
        target_path=target_path,
        evidence_path=evidence_path,
        memory_path=memory_path,
        target_verification_commands=[verification_command],
    )
    coord_root = tmp_path / "coordination-root"
    monkeypatch.delenv("AGENT_COORDINATION_ROOT", raising=False)
    runtime.settings.coordination_root = coord_root
    runtime.settings.saga_executor_backend = "dry_run"
    worktree_root = tmp_path / "worktrees"

    result = WorkflowEngine(runtime).model_directive(
        _directive_event(
            "/saga --executor fake_process "
            f"--worktree-root {worktree_root} "
            '"Use ai-business-portfolio reports and implement the next gated portfolio task"'
        )
    )

    assert result.status == WorkflowStatus.COMPLETED
    artifact = next(item for item in result.artifacts if item.role == ArtifactRole.DIRECTIVE_RESULT)
    payload = runtime.artifact_store.read_json(artifact.artifact_id)
    assert (
        payload["operator_summary"]["executor_backend"] == "FakeProcessPowWowExecutor:fake_process"
    )
    assert payload["operator_summary"]["executor_config_source"] == "directive"
    assert payload["operator_summary"]["executor_worktree_root"] == str(worktree_root)
    assert payload["operator_summary"]["executor_status"] == "COMPLETED"
    assert payload["operator_summary"]["target_repos_mutated"] is False
    assert "executor_status: COMPLETED" in payload["report"]
    assert payload["executor_result"]["external_agents_started"] is True
    assert payload["executor_result"]["changed_files"] == ["fake_agent_output.txt"]
    assert not (target_path / "fake_agent_output.txt").exists()
    with tx() as conn:
        rows = conn.execute(
            """
            SELECT artifact_type, task_id, content
            FROM task_artifacts
            WHERE pow_wow_id = ?
            ORDER BY created_at
            """,
            (payload["operator_summary"]["pow_wow_id"],),
        ).fetchall()
        approval_row = conn.execute(
            "SELECT payload_json FROM approval_requests WHERE saga_id = ?",
            (payload["saga"]["saga_id"],),
        ).fetchone()

    by_type = {row["artifact_type"]: row for row in rows}
    external_payload = json.loads(by_type["external_agent_run"]["content"])["content"]
    assert external_payload["command"]["cwd"]
    assert external_payload["command"]["stdout"]
    assert external_payload["command"]["stderr"] == ""
    assert external_payload["command"]["exit_code"] == 0
    assert external_payload["diff_summary"]["changed_files"] == ["fake_agent_output.txt"]
    assert external_payload["verification"][0]["exit_code"] == 0
    assert by_type["worktree_allocation"]["task_id"] in payload["operator_summary"]["task_ids"]
    checkpoint_payload = json.loads(by_type["worktree_commit_checkpoint"]["content"])["content"]
    assert checkpoint_payload["branch_name"].startswith("agent/")
    assert checkpoint_payload["commit_created"] is True
    assert payload["operator_summary"]["worktree_commits"] == [
        {
            "task_name": "implement_next_gated_portfolio_task",
            "branch_name": checkpoint_payload["branch_name"],
            "base_head_sha": checkpoint_payload["base_head_sha"],
            "commit_sha": checkpoint_payload["commit_sha"],
            "commit_created": True,
        }
    ]
    assert approval_row is not None
    approval_payload = json.loads(approval_row["payload_json"])
    assert approval_payload["worktree_commits"] == payload["operator_summary"]["worktree_commits"]
    assert "pow_wow_dispatch_summary" in by_type
