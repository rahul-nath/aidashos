# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic dry-run pow-wow executor."""

from __future__ import annotations

from collections.abc import Sequence

from ..constants import AGENT_BRANCH_AUTO_MERGE
from ..project_center import LinkedProject
from .types import (
    PowWowArtifact,
    PowWowExecutionContext,
    PowWowRunResult,
    PowWowTaskResult,
    PowWowTaskSpec,
)


class DryRunPowWowExecutor:
    """Deterministic executor used to prove ledger and artifact flow."""

    mode = "dry_run"

    def dispatch_pow_wow(
        self,
        pow_wow_id: str,
        target_project: LinkedProject,
        tasks: Sequence[PowWowTaskSpec],
        context: PowWowExecutionContext,
    ) -> PowWowRunResult:
        task_results = tuple(
            self._build_dry_run_task_result(task, target_project=target_project, context=context)
            for task in tasks
        )
        run_artifact = PowWowArtifact(
            artifact_type="pow_wow_run_result",
            content={
                "schema_version": "pow_wow_run_result.v1",
                "mode": self.mode,
                "status": "DRY_RUN_COMPLETED",
                "pow_wow_id": pow_wow_id,
                "target_project": {
                    "id": target_project.id,
                    "path": str(target_project.expanded_path),
                    "read_only": target_project.read_only,
                },
                "external_agents_started": False,
                "auto_merge": AGENT_BRANCH_AUTO_MERGE,
                "task_count": len(task_results),
            },
        )
        risks = (
            "Dry run only: no target-repo files were changed.",
            "Real execution still needs a live executor such as the CLI executor.",
        )
        return PowWowRunResult(
            executor=type(self).__name__,
            mode=self.mode,
            pow_wow_id=pow_wow_id,
            target_project_id=target_project.id,
            target_project_path=str(target_project.expanded_path),
            status="DRY_RUN_COMPLETED",
            output_summary=(
                f"Dry-run pow-wow planned {len(task_results)} task(s) for "
                f"{target_project.id}; no external agents started and no files changed."
            ),
            tasks=task_results,
            changed_files=(),
            verification_commands=tuple(target_project.verification_commands),
            verification_output=(),
            risks=risks,
            artifacts=(run_artifact,),
        )

    def _build_dry_run_task_result(
        self,
        task: PowWowTaskSpec,
        *,
        target_project: LinkedProject,
        context: PowWowExecutionContext,
    ) -> PowWowTaskResult:
        artifact = PowWowArtifact(
            artifact_type="pow_wow_task_plan",
            task_name=task.task_name,
            content={
                "schema_version": "pow_wow_task_plan.v1",
                "mode": self.mode,
                "task": task.to_payload(),
                "target_project_id": target_project.id,
                "target_project_path": str(target_project.expanded_path),
                "saga_id": context.saga_id,
                "goal": context.goal,
                "external_agents_started": False,
            },
        )
        return PowWowTaskResult(
            task_name=task.task_name,
            role=task.role,
            status="completed",
            summary=(
                f"Dry-run planned {task.role} task '{task.task_name}' for "
                f"{target_project.id}; execution intentionally skipped."
            ),
            changed_files=(),
            verification_commands=tuple(target_project.verification_commands),
            verification_output=(),
            risks=("Dry run did not inspect or modify target files.",),
            artifacts=(artifact,),
        )
