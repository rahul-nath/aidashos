# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared workflow result, report, and repository inspection operations."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

from ..contracts import (
    ArtifactRef,
    IngressEvent,
    Stage,
    WorkflowResult,
    WorkflowStatus,
    WorkflowType,
)
from ..ids import build_workflow_id, sha256_text

logger = logging.getLogger(__name__)


def build_event_workflow_id(workflow_type: WorkflowType, event: IngressEvent) -> str:
    digest = event.content_sha256 or sha256_text(event.model_dump_json())
    return build_workflow_id(workflow_type, event.workspace_id, event.source_type, digest)


def build_completed_workflow_result(
    workflow_id: str,
    workflow_type: WorkflowType,
    status: WorkflowStatus,
    stage: Stage,
    artifacts: list[ArtifactRef],
    egress_ids: list[str] | None = None,
    embedding_degraded: bool = False,
    manual_review_reason: str | None = None,
    help: dict[str, Any] | None = None,
) -> WorkflowResult:
    return WorkflowResult(
        workflow_id=workflow_id,
        workflow_type=workflow_type,
        status=status,
        current_stage=stage,
        artifacts=artifacts,
        egress_ids=egress_ids or [],
        embedding_degraded=embedding_degraded,
        manual_review_reason=manual_review_reason,
        help=help,
    )


def read_markdown_title(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            title = line.strip().lstrip("#").strip()
            if title:
                return title[:160]
    except OSError:
        return ""
    return ""


def build_markdown_inventory(
    root: Path,
    patterns: tuple[str, ...],
    *,
    limit: int = 24,
) -> list[dict[str, str]]:
    if not root.exists():
        return []
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for pattern in patterns:
        for match in sorted(root.glob(pattern)):
            if len(items) >= limit:
                return items
            if not match.is_file() or match.suffix.lower() != ".md":
                continue
            relative = str(match.relative_to(root))
            if relative in seen:
                continue
            seen.add(relative)
            items.append({"path": relative, "title": read_markdown_title(match)})
    return items


def list_git_status_entries(path: Path) -> list[str]:
    if not (path / ".git").exists():
        return []
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=path,
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    return [line for line in completed.stdout.splitlines() if line.strip()]


def render_operator_report(summary: dict[str, Any]) -> str:
    artifact_ids = summary.get("artifact_ids") or []
    task_ids = summary.get("task_ids") or []
    evidence_projects = summary.get("evidence_projects") or []
    risks = summary.get("executor_risks") or []
    mutation = summary.get("target_repos_mutated")
    lines = [
        "saga planned",
        f"saga_id: {summary.get('saga_id')}",
        f"pow_wow_id: {summary.get('pow_wow_id')}",
        f"target_project: {summary.get('target_project')}",
        f"evidence_projects: {', '.join(evidence_projects) if evidence_projects else 'none'}",
        f"executor_backend: {summary.get('executor_backend')}",
        f"executor_status: {summary.get('executor_status')}",
        f"executor_config_source: {summary.get('executor_config_source')}",
        f"executor_worktree_root: {summary.get('executor_worktree_root')}",
        f"task_ids: {', '.join(task_ids)}",
        f"artifact_count: {summary.get('artifact_count')}",
        f"artifact_ids: {', '.join(artifact_ids)}",
        f"ledger_path: {summary.get('ledger_path')}",
        f"target_repos_mutated: {mutation}",
        f"executor_risks: {' | '.join(risks) if risks else 'none'}",
        (
            f"merge_approval: {summary.get('merge_approval_id')} "
            f"({summary.get('merge_approval_status')})"
            if summary.get("merge_approval_id")
            else "merge_approval: none"
        ),
    ]
    return "\n".join(lines)
