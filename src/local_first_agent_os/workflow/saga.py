# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""SagaWorkflow methods split from the workflow facade."""

from __future__ import annotations

import logging
from typing import Any

from ..constants import AGENT_BRANCH_AUTO_MERGE
from ..contracts import (
    ApprovalRequestType,
    ArtifactRef,
    ArtifactRole,
    IngressEvent,
    Stage,
    WorkflowResult,
    WorkflowStatus,
    WorkflowType,
)
from ..coordination import (
    ClaimTask,
    CompletePowWow,
    CreatePowWow,
    CreateSaga,
    SubmitApprovalRequest,
)
from ..merge_review import build_merge_review_packet
from ..pow_wow import (
    PowWowExecutionContext,
    build_default_saga_tasks,
)
from ..pow_wow.ledger import (
    coordination_root,
    describe_coordination_ledger,
    persist_pow_wow_run_result,
    resolve_coordination_events_path,
    run_coordination_command,
)
from ..project_center import load_project_center, project_status_row
from .base import WorkflowMixinBase
from .core import (
    build_completed_workflow_result,
    build_markdown_inventory,
    list_git_status_entries,
    render_operator_report,
)
from .saga_support import (
    build_saga_executor,
    map_pow_wow_run_status_to_ledger_status,
)

logger = logging.getLogger(__name__)


class SagaWorkflowMixin(WorkflowMixinBase):
    def _saga_directive(
        self,
        event: IngressEvent,
        spec: Any,
    ) -> WorkflowResult:
        """Handle /saga, /pow-wow, /ambiguity, and /stagnation directives."""
        from ..coordination.saga_coordinator import (
            check_ambiguity_heuristic,
            check_stagnation,
        )

        workflow_id = self._start(WorkflowType.MODEL_DIRECTIVE, event)
        artifacts: list[ArtifactRef] = []
        result: dict[str, Any]

        try:
            action = spec.action

            if action == "saga":
                goal = spec.query or ""
                budget_tokens = spec.budget_tokens or 1_000_000

                center = load_project_center(self.runtime.settings)
                target_project = center.default_saga_target()
                if target_project.read_only:
                    raise RuntimeError(f"Default saga target is read-only: {target_project.id}")
                ledger_root = coordination_root(self.runtime.settings)
                ledger_events = resolve_coordination_events_path(self.runtime.settings)
                memory_project = center.project_by_id(center.default_memory_project)
                evidence_projects = tuple(
                    project
                    for project in center.projects
                    if project.kind == "business_evidence" and project.read_only
                )
                watched_projects = (target_project, *evidence_projects)
                repo_status_before = {
                    project.id: list_git_status_entries(project.expanded_path)
                    for project in watched_projects
                }

                saga_data = run_coordination_command(
                    CreateSaga(goal=goal, budget_tokens=budget_tokens),
                    timeout=15,
                    settings=self.runtime.settings,
                )
                saga_id = saga_data["saga_id"]
                task_specs = build_default_saga_tasks(goal, target_project)
                required_outputs = [
                    "implementation task result",
                    "review/test task result",
                    "pow-wow run artifact",
                ]
                pow_wow_data = run_coordination_command(
                    CreatePowWow(
                        saga_id=saga_id,
                        stage="IMPLEMENTATION",
                        goal=goal,
                        exit_criteria=(
                            "Implementation and review tasks are planned and recorded; "
                            "no auto-merge."
                        ),
                        required_outputs=tuple(required_outputs),
                    ),
                    timeout=15,
                    settings=self.runtime.settings,
                )
                pow_wow_id = pow_wow_data["pow_wow_id"]

                task_records: list[dict[str, Any]] = []
                for task in task_specs:
                    task_data = run_coordination_command(
                        ClaimTask(
                            pow_wow_id=pow_wow_id,
                            task_name=task.task_name,
                            description=task.description,
                            blocked_by=task.blocked_by,
                        ),
                        timeout=15,
                        settings=self.runtime.settings,
                    )
                    task_records.append(
                        {
                            **task.to_payload(),
                            "task_id": task_data["task_id"],
                            "ledger": task_data,
                        }
                    )

                target_status = {
                    **project_status_row(target_project, include_git=True),
                    "context_files": build_markdown_inventory(
                        target_project.expanded_path,
                        (
                            "reports/*.md",
                            "analysis/imported_stage_1_outputs/reports/*.md",
                            "ops/**/*.md",
                            "README.md",
                        ),
                    ),
                }
                evidence_statuses = [
                    {
                        **project_status_row(project, include_git=True),
                        "context_files": build_markdown_inventory(
                            project.expanded_path,
                            ("reports/*.md", "README.md"),
                        ),
                    }
                    for project in evidence_projects
                ]
                context = PowWowExecutionContext(
                    saga_id=saga_id,
                    goal=goal,
                    directive=spec.raw,
                    target_project_id=target_project.id,
                    target_project_path=str(target_project.expanded_path),
                    target_project_kind=target_project.kind,
                    target_project_status=target_project.status,
                    target_project_read_only=target_project.read_only,
                    verification_commands=tuple(target_project.verification_commands),
                    evidence_project_ids=tuple(project.id for project in evidence_projects),
                    memory_project_id=memory_project.id,
                    personal_context_used=False,
                    no_auto_merge=not AGENT_BRANCH_AUTO_MERGE,
                )
                from ..dependency_context_compactor import build_dependency_context_compactor

                executor, executor_config_source, executor_worktree_root = build_saga_executor(
                    self.runtime.settings,
                    spec,
                    delegate_fn=self._saga_delegate_fn(workflow_id),
                    artifact_writer=self.runtime.artifact_store,
                    dependency_compactor=build_dependency_context_compactor(self.runtime),
                )
                run_result = executor.dispatch_pow_wow(
                    pow_wow_id,
                    target_project,
                    task_specs,
                    context,
                )

                task_ids = {record["task_name"]: record["task_id"] for record in task_records}
                coordination_events = persist_pow_wow_run_result(
                    pow_wow_id,
                    task_ids,
                    run_result,
                    timeout=15,
                    settings=self.runtime.settings,
                )
                worktree_commits_by_branch: dict[str, dict[str, Any]] = {}
                for task_result in run_result.tasks:
                    for run_artifact in task_result.artifacts:
                        if run_artifact.artifact_type != "worktree_commit_checkpoint":
                            continue
                        checkpoint = run_artifact.content
                        branch_name = checkpoint.get("branch_name")
                        commit_sha = checkpoint.get("commit_sha")
                        if not isinstance(branch_name, str) or not isinstance(commit_sha, str):
                            continue
                        worktree_commits_by_branch[branch_name] = {
                            "task_name": task_result.task_name,
                            "branch_name": branch_name,
                            "base_head_sha": checkpoint.get("base_head_sha"),
                            "commit_sha": commit_sha,
                            "commit_created": bool(checkpoint.get("commit_created")),
                        }
                worktree_commits = tuple(
                    worktree_commits_by_branch[branch_name]
                    for branch_name in sorted(worktree_commits_by_branch)
                )
                merge_approval: dict[str, Any] | None = None
                if (
                    run_result.status == "COMPLETED"
                    and run_result.external_agents_started
                    and run_result.changed_files
                ):
                    review_packet = build_merge_review_packet(
                        saga_id=saga_id,
                        approval_id=None,
                        requested_by="saga_directive",
                        intent_id=None,
                        pow_wow_id=pow_wow_id,
                        target_project_id=target_project.id,
                        run_result=run_result.to_payload(),
                        target_project_path=target_project.expanded_path,
                    )
                    merge_approval = run_coordination_command(
                        SubmitApprovalRequest(
                            saga_id=saga_id,
                            request_type=ApprovalRequestType.CODE_MERGE.value,
                            requested_by="saga_directive",
                            payload={
                                "pow_wow_id": pow_wow_id,
                                "executor_status": run_result.status,
                                "target_project_id": target_project.id,
                                "changed_files": list(run_result.changed_files),
                                "worktree_commits": list(worktree_commits),
                                "review_packet": review_packet,
                            },
                        ),
                        timeout=15,
                        settings=self.runtime.settings,
                    )
                completed_pow_wow = run_coordination_command(
                    CompletePowWow(
                        pow_wow_id=pow_wow_id,
                        output_summary=run_result.output_summary,
                        status=map_pow_wow_run_status_to_ledger_status(run_result.status),
                    ),
                    timeout=15,
                    settings=self.runtime.settings,
                )
                repo_status_after = {
                    project.id: list_git_status_entries(project.expanded_path)
                    for project in watched_projects
                }
                repo_mutation_checks = {
                    project_id: {
                        "before": before,
                        "after": repo_status_after.get(project_id, []),
                        "mutated": before != repo_status_after.get(project_id, []),
                    }
                    for project_id, before in repo_status_before.items()
                }
                artifact_ids = [
                    str(event["artifact_id"])
                    for event in coordination_events
                    if isinstance(event.get("artifact_id"), str)
                ]
                operator_summary = {
                    "saga_id": saga_id,
                    "pow_wow_id": pow_wow_id,
                    "task_ids": list(task_ids.values()),
                    "artifact_ids": artifact_ids,
                    "artifact_count": len(artifact_ids),
                    "executor_backend": f"{run_result.executor}:{run_result.mode}",
                    "executor_status": run_result.status,
                    "executor_config_source": executor_config_source,
                    "executor_worktree_root": (
                        str(executor_worktree_root) if executor_worktree_root else None
                    ),
                    "target_project": target_project.id,
                    "evidence_projects": [project.id for project in evidence_projects],
                    "ledger_root": str(ledger_root),
                    # The ledger is Postgres; printing a file path named a file
                    # that does not exist on any real deployment.
                    "ledger_path": describe_coordination_ledger(self.runtime.settings),
                    "ledger_events_path": str(ledger_events),
                    "target_repos_mutated": any(
                        check["mutated"] for check in repo_mutation_checks.values()
                    ),
                    "repo_mutation_checks": repo_mutation_checks,
                    "executor_risks": list(run_result.risks),
                    "worktree_commits": list(worktree_commits),
                    "merge_approval_id": (
                        merge_approval["approval_id"] if merge_approval else None
                    ),
                    "merge_approval_status": (merge_approval["status"] if merge_approval else None),
                    "auto_merge": AGENT_BRANCH_AUTO_MERGE,
                }

                result = {
                    "schema_version": "directive_result.v1",
                    "directive": spec.raw,
                    "action": "saga",
                    "status": "planned",
                    "goal": goal,
                    "budget_tokens": budget_tokens,
                    "target_project": target_status,
                    "evidence_projects": evidence_statuses,
                    "memory_project": {
                        "id": memory_project.id,
                        "path": str(memory_project.expanded_path),
                        "kind": memory_project.kind,
                        "retrieval_used": False,
                    },
                    "saga": saga_data,
                    "pow_wow": pow_wow_data,
                    "tasks": task_records,
                    "executor_result": run_result.to_payload(),
                    "coordination_events": coordination_events,
                    "merge_approval_request": merge_approval,
                    "completed_pow_wow": completed_pow_wow,
                    "auto_merge": AGENT_BRANCH_AUTO_MERGE,
                    "executor_config_source": executor_config_source,
                    "operator_summary": operator_summary,
                    "report": render_operator_report(operator_summary),
                    "next_steps": (
                        [
                            "Inspect the dry-run task plan and target project context.",
                            ("Select an explicit live executor before editing target files."),
                            "Keep ai-business-portfolio-analysis read-only evidence.",
                            "Require explicit approval before merge or deploy.",
                        ]
                        if run_result.mode == "dry_run"
                        else [
                            "Inspect captured executor artifacts, diffs, and verification output.",
                            "Keep ai-business-portfolio-analysis read-only evidence.",
                            (
                                "Resolve the pending CODE_MERGE approval request "
                                f"{merge_approval['approval_id']} before any merge."
                                if merge_approval
                                else "Require explicit approval before merge or deploy."
                            ),
                        ]
                    ),
                }

            elif action == "pow_wow":
                saga_id = spec.saga_id or ""
                stage = spec.pow_wow_stage or "IMPLEMENTATION"
                goal = spec.query or ""

                pw_data = run_coordination_command(
                    CreatePowWow(
                        saga_id=saga_id,
                        stage=stage,
                        goal=goal,
                        exit_criteria=f"Produce all required outputs for {stage}",
                    ),
                    timeout=15,
                    settings=self.runtime.settings,
                )

                result = {
                    "schema_version": "directive_result.v1",
                    "directive": spec.raw,
                    "action": "pow_wow",
                    "saga_id": saga_id,
                    "stage": stage,
                    "goal": goal,
                    "pow_wow": pw_data,
                    "policy_reminder": (
                        "Roles do NOT imply permissions. "
                        "Agents must request_tool_permission for sensitive tools. "
                        "What an agent may edit is decided by its spawn posture, "
                        "not by its role."
                    ),
                }

            elif action == "ambiguity_check":
                gawd_doc_id = spec.query or ""
                score = check_ambiguity_heuristic(gawd_doc_id)
                result = {
                    "schema_version": "directive_result.v1",
                    "directive": spec.raw,
                    "action": "ambiguity_check",
                    "gawd_doc_id": gawd_doc_id,
                    "ready_to_execute": score.ready_to_execute,
                    "scores": score.scores,
                    "passes": score.passes,
                    "message": (
                        "GAWD doc passes ambiguity gate — safe to proceed."
                        if score.ready_to_execute
                        else "Ambiguity gate FAILED. Resolve failing checks before advancing."
                    ),
                    "thresholds": {
                        "goal_clarity": 0.85,
                        "constraints_clarity": 0.80,
                        "success_criteria_clarity": 0.80,
                        "max_unresolved_critical": 0,
                    },
                }

            else:  # stagnation_check
                saga_id = spec.query or ""
                report = check_stagnation(saga_id)
                result = {
                    "schema_version": "directive_result.v1",
                    "directive": spec.raw,
                    "action": "stagnation_check",
                    "saga_id": saga_id,
                    "stagnated": report.stagnated,
                    "delta_ratio": report.delta_ratio,
                    "threshold": report.threshold,
                    "reason": report.reason,
                    "recommendation": report.recommendation,
                    "pow_wows_checked": report.pow_wows_checked,
                }

        except Exception as exc:
            result = {
                "schema_version": "directive_result.v1",
                "directive": spec.raw,
                "action": spec.action,
                "status": "failed",
                "error": str(exc),
            }
            self.runtime.repository.update_workflow(
                workflow_id,
                status=WorkflowStatus.FAILED_PERMANENT,
                stage=Stage.COMPLETED,
                error=str(exc),
            )
            artifact = self.runtime.artifact_store.write_json(
                role=ArtifactRole.DIRECTIVE_RESULT.value,
                payload=result,
                workflow_id=workflow_id,
                schema_version="directive_result.v1",
            )
            return build_completed_workflow_result(
                workflow_id,
                WorkflowType.MODEL_DIRECTIVE,
                WorkflowStatus.FAILED_PERMANENT,
                Stage.COMPLETED,
                [artifact],
            )

        artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.DIRECTIVE_RESULT.value,
            payload=result,
            workflow_id=workflow_id,
            schema_version="directive_result.v1",
        )
        artifacts.append(artifact)
        self.runtime.repository.update_workflow(
            workflow_id, status=WorkflowStatus.COMPLETED, stage=Stage.COMPLETED
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.MODEL_DIRECTIVE,
            WorkflowStatus.COMPLETED,
            Stage.COMPLETED,
            artifacts,
        )
