# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import contextlib
import json
import logging
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..constants import AGENT_BRANCH_AUTO_MERGE, DEFAULT_DISPATCHER_NAME
from ..contracts import (
    ApprovalRequestType,
    ArtifactRef,
    ArtifactRole,
    DirectiveSpec,
    IngressEvent,
    ModelRole,
    Stage,
    WorkflowResult,
    WorkflowStatus,
    WorkflowType,
)
from ..coordination import (
    ApprovalDecision,
    ApproveGawdDoc,
    AttachGawdDocToSaga,
    ClaimTask,
    CompletePowWow,
    CreateGawdDoc,
    CreatePowWow,
    CreateSaga,
    DispatchKind,
    DispatchTier,
    GetGawdDoc,
    ListApprovalRequests,
    ListSagaMilestones,
    ListSagas,
    NextReadySagaMilestone,
    ResolveApprovalRequest,
    RetrySagaMilestone,
    SubmitArtifact,
    SubmitDispatchIntent,
)
from ..coordination.outcomes import (
    DispatchPromotionState,
    next_dispatch_promotion_states,
    require_dispatch_promotion_transition,
)
from ..coordination.projects import saga_content_digest
from ..directives import DirectiveParser
from ..directives_help import help_payload
from ..gawd_walkthru import (
    GawdWalkthruStore,
)
from ..gawd_walkthru_runtime import GawdWalkthruSummarizer
from ..ids import sha256_text
from ..lifecycle_failure_harness import (
    LifecycleTransitionPoint,
    reach_lifecycle_transition,
)
from ..merge_review import (
    pending_code_merge_approval,
    render_merge_review_packet,
    require_staff_review_provenance,
    review_packet_for_approval,
)
from ..new_project_intake import (
    SCHEMA_VERSION_PERMISSION_ENVELOPE,
    SparseGawdDraft,
    append_required_artifacts_section,
    build_durable_workflow_plan,
    build_gawd_review_tasks,
    build_reviewable_gawd_draft,
    create_sparse_gawd_draft_file,
    merge_pow_wow_result_into_gawd_review_markdown,
    parse_sparse_gawd_draft,
    refine_durable_workflow_plan_from_run_result,
    render_execution_milestones_markdown,
    render_required_artifacts_markdown,
    replace_execution_milestones_section,
    task_graph_payload,
    write_gawd_review_files,
)
from ..observability import (
    WORKFLOW_ACTIVE,
    WORKFLOW_LATENCY_SECONDS,
    WORKFLOW_RUNS_TOTAL,
    observability_context,
    profiled_step,
)
from ..pow_wow import (
    PowWowExecutionContext,
)
from ..pow_wow.ledger import (
    persist_pow_wow_run_result,
    run_coordination_command,
    serialize_coordination_content_to_json,
)
from ..project_center import load_project_center, project_status_row
from ..project_scaffold import (
    scaffold_spec,
    scaffold_target_project,
)
from ..runtime import AppRuntime, get_runtime
from .browser import BrowserWorkflowMixin
from .core import (
    build_completed_workflow_result,
    build_event_workflow_id,
)
from .graph import GraphWorkflowMixin
from .knowledge import KnowledgeWorkflowMixin
from .models import ModelWorkflowMixin
from .saga import SagaWorkflowMixin
from .saga_support import (
    approve_next_dependency_ready_milestone,
    build_approved_gawd_dispatch_prompt,
    build_approved_gawd_milestone_dispatch_source,
    build_saga_executor,
    build_target_project_scaffold_from_gawd_doc,
    ensure_approved_gawd_milestones,
    extract_target_project_id_from_gawd_doc,
    find_existing_dispatch_intent_for_source,
    load_control_plane_target_project,
    map_pow_wow_run_status_to_ledger_status,
    persist_durable_workflow_milestones,
    resolve_approved_gawd_target_project_id,
    resolve_project_repo_root,
    resolve_target_project_from_gawd_dispatch_history,
    validate_approved_gawd_target_project,
)
from .whiteboard import WhiteboardIntentWorkflowMixin
from .workspace import WorkspaceWorkflowMixin

logger = logging.getLogger(__name__)


def _git_contains_commit(repo: Path, commit_sha: str) -> bool:
    try:
        completed = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit_sha, "main"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _build_no_ready_milestone_guidance(
    *,
    gawd_doc_id: str,
    target_project_id: str,
    target_project_path: Path,
    coordination_root: Path,
    saga_milestones: Sequence[object],
    blocked_milestones: Sequence[object],
    approved_requests: Sequence[object],
) -> dict[str, Any]:
    """Explain the exact durable transition blocking the next milestone."""

    milestones: dict[str, Mapping[str, Any]] = {}
    for item in saga_milestones:
        if not isinstance(item, Mapping):
            continue
        milestone_id = str(item.get("milestone_id") or "").strip()
        if milestone_id:
            milestones[milestone_id] = item
    blocked = [item for item in blocked_milestones if isinstance(item, Mapping)]
    approved_merges = [
        item
        for item in approved_requests
        if isinstance(item, Mapping)
        and item.get("request_type") == ApprovalRequestType.CODE_MERGE.value
    ]
    blocker_details: list[dict[str, Any]] = []
    next_actions: list[dict[str, Any]] = []
    lines = ["no_ready_milestone: no milestone is currently runnable."]
    next_step = "Resolve the listed milestone dependency or approval."

    for candidate in blocked:
        candidate_id = str(candidate.get("milestone_id") or "").strip()
        unresolved_dependencies: list[dict[str, Any]] = []
        for raw_dependency_id in candidate.get("depends_on") or ():
            dependency_id = str(raw_dependency_id)
            dependency = milestones.get(dependency_id, {})
            dependency_status = str(dependency.get("status") or "MISSING")
            if dependency_status != "COMPLETED":
                unresolved_dependencies.append(
                    {
                        "milestone_id": dependency_id,
                        "status": dependency_status,
                    }
                )
        detail = {
            "milestone_id": candidate_id,
            "dependency_ready": bool(candidate.get("dependency_ready")),
            "approval_ready": bool(candidate.get("approval_ready")),
            "unresolved_dependencies": unresolved_dependencies,
        }
        blocker_details.append(detail)
        if unresolved_dependencies:
            rendered = ", ".join(
                f"{item['milestone_id']} ({item['status']})" for item in unresolved_dependencies
            )
            lines.append(f"Candidate {candidate_id} is blocked by: {rendered}.")

    unresolved = next(
        (
            dependency
            for detail in blocker_details
            for dependency in detail["unresolved_dependencies"]
        ),
        None,
    )
    rerun_command = shlex.join(
        [
            "pi",
            "/start",
            "/approved-gawd",
            gawd_doc_id,
            "--target-project",
            target_project_id,
        ]
    )
    if unresolved is not None:
        dependency_id = str(unresolved["milestone_id"])
        approval = next(
            (
                request
                for request in approved_merges
                if isinstance(request.get("payload"), Mapping)
                and request["payload"].get("milestone_id") == dependency_id
            ),
            None,
        )
        raw_payload = approval.get("payload") if isinstance(approval, Mapping) else None
        payload: Mapping[str, Any] = raw_payload if isinstance(raw_payload, Mapping) else {}
        commit_sha = str(payload.get("commit_sha") or "").strip()
        approval_id = str(approval.get("approval_id") or "").strip() if approval else ""
        if commit_sha and _git_contains_commit(target_project_path, commit_sha):
            outcome = (
                "MANUAL_RECOVERY_COMPLETION"
                if payload.get("manual_recovery")
                else "AUTOMATED_COMPLETION"
            )
            evidence = json.dumps(
                {
                    "schema_version": "approved_merge_milestone_completion.v1",
                    "approval_id": approval_id,
                    "target_project_id": target_project_id,
                    "approved_commit_sha": commit_sha,
                    "promotion_state": "MERGED",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            completion_command = "UV_CACHE_DIR=/tmp/uv-cache " + shlex.join(
                [
                    "uv",
                    "run",
                    "python",
                    str(resolve_project_repo_root() / "agent_coordination_mcp.py"),
                    "--root",
                    str(coordination_root),
                    "complete_saga_milestone",
                    dependency_id,
                    "--evidence-type",
                    "summary",
                    "--evidence-content",
                    evidence,
                    "--outcome",
                    outcome,
                ]
            )
            lines.extend(
                [
                    (
                        f"Approved commit {commit_sha} from CODE_MERGE {approval_id} is "
                        f"already contained in {target_project_id}/main."
                    ),
                    "The missing transition is ledger milestone completion:",
                    completion_command,
                    "Then rerun:",
                    rerun_command,
                ]
            )
            next_step = completion_command
            next_actions.extend(
                [
                    {
                        "action": "complete_merged_milestone",
                        "milestone_id": dependency_id,
                        "approval_id": approval_id,
                        "commit_sha": commit_sha,
                        "command": completion_command,
                    },
                    {
                        "action": "dispatch_next_ready_milestone",
                        "command": rerun_command,
                    },
                ]
            )
        elif commit_sha:
            merge_command = shlex.join(
                [
                    "git",
                    "-C",
                    str(target_project_path),
                    "merge",
                    "--ff-only",
                    commit_sha,
                ]
            )
            lines.extend(
                [
                    f"CODE_MERGE {approval_id} is approved, but its commit is not in main.",
                    "Merge the exact approved commit first:",
                    merge_command,
                ]
            )
            next_step = merge_command
            next_actions.append(
                {
                    "action": "merge_exact_approved_commit",
                    "milestone_id": dependency_id,
                    "approval_id": approval_id,
                    "commit_sha": commit_sha,
                    "command": merge_command,
                }
            )
        else:
            lines.append(
                f"Complete dependency milestone {dependency_id} before retrying this command."
            )
            next_step = f"Complete dependency milestone {dependency_id}."
    elif blocked and not bool(blocked[0].get("approval_ready")):
        candidate_id = str(blocked[0].get("milestone_id") or "").strip()
        lines.append(f"Milestone {candidate_id} still requires its execution approval.")
        next_step = f"Resolve the execution approval for milestone {candidate_id}."
    else:
        active = [
            item for item in milestones.values() if item.get("status") in {"IN_PROGRESS", "BLOCKED"}
        ]
        if active:
            lines.append(
                "Active milestone(s): "
                + ", ".join(f"{item.get('milestone_id')} ({item.get('status')})" for item in active)
                + "."
            )
        else:
            lines.append("There are no pending dependency-ready milestones to dispatch.")

    return {
        "blocker_details": blocker_details,
        "next_actions": next_actions,
        "next_step": next_step,
        "note": "The GAWD doc is approved, but a durable milestone boundary is unresolved.",
        "report": "\n".join(lines),
    }


def _saga_milestone_snapshot(settings: Any) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    """Read the operator-facing saga/milestone projection from the durable ledger."""

    sagas = run_coordination_command(
        ListSagas(),
        timeout=15,
        settings=settings,
    ).get("sagas", [])
    snapshot: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for raw_saga in sagas:
        if not isinstance(raw_saga, Mapping):
            continue
        saga = dict(raw_saga)
        saga_id = str(saga.get("saga_id") or "").strip()
        if not saga_id:
            continue
        raw_milestones = run_coordination_command(
            ListSagaMilestones(saga_id),
            timeout=15,
            settings=settings,
        ).get("milestones", [])
        milestones = [dict(item) for item in raw_milestones if isinstance(item, Mapping)]
        snapshot.append((saga, milestones))
    return snapshot


def _find_latest_retryable_milestone(settings: Any) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for saga, milestones in _saga_milestone_snapshot(settings):
        for milestone in milestones:
            if milestone.get("status") not in {"FAILED", "BLOCKED", "CANCELED"}:
                continue
            candidates.append(
                {
                    "saga_id": saga.get("saga_id"),
                    "gawd_doc_id": milestone.get("gawd_doc_id") or saga.get("gawd_doc_id"),
                    "milestone_id": milestone.get("milestone_id"),
                    "milestone_name": milestone.get("name"),
                    "previous_status": milestone.get("status"),
                    "previous_outcome": milestone.get("outcome"),
                    "updated_at": milestone.get("updated_at") or "",
                }
            )
    if not candidates:
        raise ValueError("/try-milestone found no FAILED, BLOCKED, or CANCELED milestone to retry.")
    return max(
        candidates,
        key=lambda item: (
            str(item["updated_at"]),
            str(item["milestone_id"]),
        ),
    )


def _find_latest_dependency_ready_gawd(settings: Any) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for saga, milestones in _saga_milestone_snapshot(settings):
        statuses = {
            str(item.get("milestone_id")): item.get("status")
            for item in milestones
            if item.get("milestone_id")
        }
        ready = [
            item
            for item in milestones
            if item.get("status") == "PENDING"
            and all(statuses.get(str(dep)) == "COMPLETED" for dep in item.get("depends_on", []))
        ]
        if not ready:
            continue
        selected = min(
            ready,
            key=lambda item: (
                int(item.get("sequence") or 0),
                str(item.get("milestone_id") or ""),
            ),
        )
        gawd_doc_id = str(selected.get("gawd_doc_id") or saga.get("gawd_doc_id") or "").strip()
        if not gawd_doc_id:
            continue
        latest_activity = max(
            (str(item.get("updated_at") or "") for item in milestones),
            default=str(saga.get("updated_at") or ""),
        )
        candidates.append(
            {
                "saga_id": saga.get("saga_id"),
                "gawd_doc_id": gawd_doc_id,
                "milestone_id": selected.get("milestone_id"),
                "milestone_name": selected.get("name"),
                "milestone_sequence": selected.get("sequence"),
                "activity_at": latest_activity,
            }
        )
    if not candidates:
        raise ValueError("/approve-most-recent found no dependency-ready PENDING milestone.")
    return max(
        candidates,
        key=lambda item: (
            str(item["activity_at"]),
            str(item["saga_id"]),
            str(item["milestone_id"]),
        ),
    )


class WorkflowEngine(
    GraphWorkflowMixin,
    KnowledgeWorkflowMixin,
    BrowserWorkflowMixin,
    ModelWorkflowMixin,
    WorkspaceWorkflowMixin,
    WhiteboardIntentWorkflowMixin,
    SagaWorkflowMixin,
):
    def __init__(self, runtime: AppRuntime):
        self.runtime = runtime

    def _saga_delegate_fn(self, workflow_id: str) -> Callable[..., Mapping[str, Any]]:
        """Sync callback the executor uses to run junior-tier tasks on the local
        model via delegate_task (bounded prompt -> ledger artifact). The junior
        tier coordinates with the frontier tiers through the durable ledger, not
        process IPC.

        The delegate's model I/O artifacts are scoped to the saga's registered
        ``workflow_id`` so the artifact store's workflow FK is satisfied.

        The body lives in ``local_delegate`` because the resident dispatcher
        needs the same callback and cannot reach a method on this class. Being a
        method here is the reason it went without one.
        """
        from ..local_delegate import build_directive_local_delegate

        return build_directive_local_delegate(self.runtime, workflow_id=workflow_id)

    def _start(self, workflow_type: WorkflowType, event: IngressEvent) -> str:
        workflow_id = build_event_workflow_id(workflow_type, event)
        self.runtime.repository.register_ingress_event(event)
        self.runtime.repository.start_workflow_run(
            workflow_id=workflow_id,
            workflow_type=workflow_type.value,
            workspace_id=event.workspace_id,
            input_event_id=event.event_id,
        )
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.PROCESSING,
            stage=Stage.VALIDATED,
        )
        return workflow_id

    def model_directive(self, event: IngressEvent) -> WorkflowResult:
        parser = DirectiveParser(self.runtime.settings)
        raw_directive = str(event.payload.get("directive", ""))
        try:
            spec = parser.parse(raw_directive)
        except Exception as exc:
            workflow_id = self._start(WorkflowType.MODEL_DIRECTIVE, event)
            help_block = help_payload(parser, raw_directive, str(exc))
            artifact = self.runtime.artifact_store.write_json(
                role=ArtifactRole.DIRECTIVE_RESULT.value,
                payload={
                    "schema_version": "directive_result.v1",
                    "directive": raw_directive,
                    "status": "failed",
                    "error": str(exc),
                    "help": help_block,
                },
                workflow_id=workflow_id,
                schema_version="directive_result.v1",
            )
            self.runtime.repository.update_workflow(
                workflow_id,
                status=WorkflowStatus.FAILED_PERMANENT,
                stage=Stage.COMPLETED,
                error=str(exc),
            )
            return build_completed_workflow_result(
                workflow_id,
                WorkflowType.MODEL_DIRECTIVE,
                WorkflowStatus.FAILED_PERMANENT,
                Stage.COMPLETED,
                [artifact],
                manual_review_reason=help_block.get("summary"),
                help=help_block,
            )
        if spec.action == "agent_query":
            return self.agent_query(event)
        if spec.action == "ocr_capture":
            return self.ocr_capture(event)
        if spec.action in {"store", "screenshot"}:
            return self.directory_embedding(event)
        if spec.action == "send_to_wf":
            return self.send_to_workflowy(event)
        if spec.action == "done":
            return self.done_recall(event)
        if spec.action == "chrome":
            return self.chrome_control(event)
        if spec.action in {"saga", "pow_wow", "ambiguity_check", "stagnation_check"}:
            return self._saga_directive(event, spec)
        if spec.action == "new_project":
            return self._new_project_directive(event, spec)
        if spec.action in {"approved_gawd", "approve_most_recent"}:
            return self._approved_gawd_directive(event, spec)
        workflow_id = self._start(WorkflowType.MODEL_DIRECTIVE, event)
        artifacts: list[ArtifactRef] = []
        status = WorkflowStatus.COMPLETED
        stage = Stage.COMPLETED
        result: dict[str, Any]

        try:
            if spec.action == "start":
                role = spec.model_role or ModelRole.GENERAL
                # ASR routes through AudioTranscriber (whisper.cpp), not the
                # llama-cpp model registry. `/start /asr` (or `/audio`) both
                # loads the large model and immediately opens a foreground
                # streaming mic session.
                if role == ModelRole.ASR and spec.alias in {"/asr", "/audio"}:
                    self.runtime.audio_transcriber.load_active_model()
                    result = {
                        "schema_version": "directive_result.v1",
                        "directive": spec.raw,
                        "action": "start",
                        "role": role.value,
                        "status": "loaded",
                    }
                    if not self.runtime.settings.mock_models:
                        exit_code = self.runtime.audio_transcriber.stream()
                        result["asr_session"] = {
                            "exit_code": exit_code,
                            "outcome": "timeout" if exit_code == 1 else "ended",
                        }
                else:
                    try:
                        self.runtime.model_manager.ensure_loaded(role, allow_autoload=True)
                        if role == ModelRole.GENERAL:
                            self.runtime.model_manager.clear_default_fallback()
                        self.runtime.model_manager.set_active_general_role(
                            role,
                            reason=f"started by {spec.raw}",
                        )
                        result = {
                            "schema_version": "directive_result.v1",
                            "directive": spec.raw,
                            "action": "start",
                            "role": role.value,
                            "status": "loaded",
                        }
                    except Exception as exc:
                        if role != ModelRole.GENERAL:
                            raise
                        fallback = self._start_default_fallback()
                        result = {
                            "schema_version": "directive_result.v1",
                            "directive": spec.raw,
                            "action": "start",
                            "role": role.value,
                            "status": "degraded_default_fallback",
                            "reason": str(exc),
                            **fallback,
                        }
            elif spec.action == "stop":
                # ASR owns a separate whisper-server process. Stop its
                # supervisor too, otherwise launchd KeepAlive recreates it.
                if spec.model_role == ModelRole.ASR:
                    unload_result = self.runtime.audio_transcriber.stop_server()
                else:
                    unload_result = self.runtime.model_manager.unload(spec.model_role)
                    if spec.model_role is None:
                        with contextlib.suppress(Exception):
                            self.runtime.audio_transcriber.stop_server()
                self.runtime.model_manager.clear_active_general_role_if(spec.model_role)
                result = {
                    "schema_version": "directive_result.v1",
                    "directive": spec.raw,
                    "action": "stop",
                    "result": unload_result,
                }
            elif spec.action == "observability":
                result = self._observability_directive(spec)
            elif spec.action in {"dispatcher", "dispatch_once"}:
                result = self._dispatcher_directive(spec, workflow_id)
            elif spec.action == "try_milestone":
                result = self._try_milestone_directive(spec)
            elif spec.action == "review_merge":
                result = self._review_merge_directive(spec)
            elif spec.action == "approve_merge":
                result = self._approve_merge_directive(spec)
            elif spec.action == "ledger":
                result = self._ledger_directive(spec)
            elif spec.action == "status":
                result = self._status_directive()
            elif spec.action == "project_status":
                from ..project_action import build_project_action_snapshot

                snapshot = build_project_action_snapshot(
                    spec.target_project_id or "",
                    settings=self.runtime.settings,
                )
                result = {
                    "schema_version": "directive_result.v1",
                    "directive": spec.raw,
                    "action": "project_status",
                    "status": "completed",
                    "snapshot": snapshot.model_dump(mode="json"),
                    "report": "\n".join(
                        line
                        for line in (
                            f"{spec.target_project_id}: {snapshot.action.value}",
                            snapshot.summary,
                            (
                                "Milestone: "
                                + str(snapshot.milestone.name or snapshot.milestone.milestone_id)
                                if snapshot.milestone
                                else None
                            ),
                            f"Next: {snapshot.next_command}" if snapshot.next_command else None,
                        )
                        if line
                    ),
                }
            elif spec.action == "graph":
                result = self._graph_directive(event, spec)
            elif spec.action in {"get", "fetch"}:
                query = spec.query or str(event.payload.get("query") or "")
                if spec.action == "fetch" and spec.retrieval_source == "workflowy":
                    hits = self.runtime.retrieval.fetch_workflowy(query, top_k=8)
                    report = (
                        "\n\n---\n\n".join(hit.text for hit in hits)
                        if hits
                        else "No indexed Workflowy evidence matched that request."
                    )
                else:
                    hits = self.runtime.retrieval.search(
                        query,
                        workspace_id=None,
                        top_k=8,
                    )
                    report = None
                result = {
                    "schema_version": "directive_result.v1",
                    "directive": spec.raw,
                    "action": spec.action,
                    "query": query,
                    "retrieval_source": spec.retrieval_source,
                    "ranked_ids": [hit.chunk_id for hit in hits],
                    "hits": [
                        {
                            "chunk_id": hit.chunk_id,
                            "workspace_id": hit.workspace_id,
                            "score": hit.score,
                            "text_preview": hit.text[:600],
                            "metadata": hit.metadata,
                        }
                        for hit in hits
                    ],
                }
                if report is not None:
                    result["report"] = report
            else:
                result = self._compact_context(
                    workflow_id=workflow_id,
                    directive=spec.raw,
                    context=str(event.payload.get("context") or spec.query or ""),
                    max_window_tokens=int(
                        event.payload.get("max_window_tokens") or parser.default_max_window_tokens
                    ),
                    threshold_ratio=float(
                        event.payload.get("threshold_ratio") or parser.compaction_threshold_ratio
                    ),
                    target_ratio=float(
                        event.payload.get("target_ratio") or parser.compaction_target_ratio
                    ),
                )
        except Exception as exc:
            status = WorkflowStatus.FAILED_PERMANENT
            stage = Stage.COMPLETED
            result = {
                "schema_version": "directive_result.v1",
                "directive": spec.raw,
                "status": "failed",
                "error": str(exc),
                "help": help_payload(parser, spec.raw, str(exc)),
            }

        artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.DIRECTIVE_RESULT.value,
            payload=result,
            workflow_id=workflow_id,
            schema_version="directive_result.v1",
        )
        artifacts.append(artifact)
        self.runtime.repository.update_workflow(
            workflow_id,
            status=status,
            stage=stage,
            error=result.get("error"),
        )
        help_block = result.get("help") if status != WorkflowStatus.COMPLETED else None
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.MODEL_DIRECTIVE,
            status,
            stage,
            artifacts,
            manual_review_reason=(
                result.get("warning") or (help_block.get("summary") if help_block else None)
            ),
            help=help_block,
        )

    def _dispatcher_directive(self, spec: DirectiveSpec, workflow_id: str) -> dict[str, Any]:
        from ..dispatcher import LedgerDispatcher
        from ..dispatcher_runner import build_dispatcher_runner
        from ..staffing import dispatch_seat_counts, load_bench

        runner = build_dispatcher_runner(
            self.runtime,
            delegate_fn=self._saga_delegate_fn(workflow_id),
        )
        # Same seats as the resident loop, from the same staffing file: the
        # directive door and the resident door must not disagree about how many
        # pipelines a tier may run at once. A one-poll `/dispatch` therefore
        # drains one sweep - up to the free seats - rather than one intent.
        dispatcher = LedgerDispatcher(
            runner,
            name=spec.dispatcher_name or DEFAULT_DISPATCHER_NAME,
            tier=spec.dispatcher_tier,
            settings=self.runtime.settings,
            seats=dispatch_seat_counts(
                load_bench(self.runtime.settings.config_dir / "staffing.toml")
            ),
        )
        dispatched = dispatcher.dispatch_pending_intents(
            interval_seconds=spec.dispatcher_interval_seconds or 2.0,
            max_polls=spec.dispatcher_max_polls,
        )
        dispatch_outcomes = list(getattr(dispatcher, "last_outcomes", []))
        outcome_lines = [
            (
                f"intent {outcome.intent_id}: {outcome.status}; tier={outcome.tier}; "
                f"target={outcome.target_project_id or 'unspecified'}; "
                f"milestone={outcome.milestone_id or 'unspecified'}; "
                f"source={outcome.source or 'unspecified'}"
            )
            for outcome in dispatch_outcomes
        ]
        report_lines = [
            f"dispatcher completed: dispatched {dispatched} intent(s) "
            f"in {spec.dispatcher_max_polls or 'unbounded'} poll(s)"
        ]
        report_lines.extend(
            outcome_lines
            or [
                (
                    "No PENDING dispatch intent was available."
                    if dispatched == 0
                    else "Per-intent metadata was unavailable from the dispatcher."
                )
            ]
        )
        return {
            "schema_version": "directive_result.v1",
            "directive": spec.raw,
            "action": spec.action,
            "resolved_command": (
                "/start /dispatcher --max-polls 1" if spec.action == "dispatch_once" else spec.raw
            ),
            "status": "completed",
            "dispatcher_name": spec.dispatcher_name or DEFAULT_DISPATCHER_NAME,
            "tier": spec.dispatcher_tier,
            "interval_seconds": spec.dispatcher_interval_seconds or 2.0,
            "max_polls": spec.dispatcher_max_polls,
            "dispatched_count": dispatched,
            "dispatch_outcomes": [
                {
                    "intent_id": outcome.intent_id,
                    "status": outcome.status,
                    "tier": outcome.tier,
                    "target_project_id": outcome.target_project_id,
                    "milestone_id": outcome.milestone_id,
                    "source": outcome.source,
                }
                for outcome in dispatch_outcomes
            ],
            "report": "\n".join(report_lines),
        }

    def _try_milestone_directive(self, spec: DirectiveSpec) -> dict[str, Any]:
        resolution = _find_latest_retryable_milestone(self.runtime.settings)
        milestone_id = str(resolution["milestone_id"])
        reason = (spec.query or "").strip() or "Retry requested via pi /try-milestone"
        retry = run_coordination_command(
            RetrySagaMilestone(milestone_id, reason),
            timeout=15,
            settings=self.runtime.settings,
        )
        if retry.get("status") == "checkpoint_recovery_required":
            checkpoint = retry.get("checkpoint")
            checkpoint_id = (
                str(checkpoint.get("checkpoint_id") or "")
                if isinstance(checkpoint, Mapping)
                else ""
            )
            return {
                "schema_version": "directive_result.v1",
                "directive": spec.raw,
                "action": "try_milestone",
                "status": "checkpoint_recovery_required",
                "resolution": resolution,
                "reason": reason,
                "milestone": retry.get("milestone"),
                "checkpoint": checkpoint,
                "execution_enqueued": False,
                "next_step": retry.get("next_step") or "pi /ledger",
                "note": (
                    "The milestone was not reopened. Its durable checkpoint "
                    f"{checkpoint_id or 'must be inspected'} owns recovery, so a "
                    "fresh approved-GAWD dispatch was not permitted."
                ),
            }
        if not retry.get("ok"):
            raise RuntimeError(
                str(retry.get("message") or retry.get("error") or "milestone retry failed")
            )
        return {
            "schema_version": "directive_result.v1",
            "directive": spec.raw,
            "action": "try_milestone",
            "status": "retried",
            "resolution": resolution,
            "reason": reason,
            "milestone": retry.get("milestone"),
            "next_step": "pi /approve-most-recent",
            "note": (
                "The terminal milestone was reopened as PENDING. No implementation "
                "was enqueued or started by this command."
            ),
        }

    def _review_merge_directive(self, spec: DirectiveSpec) -> dict[str, Any]:
        approval_id = (spec.query or "").strip() or None
        approval = pending_code_merge_approval(
            settings=self.runtime.settings,
            approval_id=approval_id,
        )
        packet = review_packet_for_approval(
            approval,
            settings=self.runtime.settings,
        )
        return {
            "schema_version": "directive_result.v1",
            "directive": spec.raw,
            "action": "review_merge",
            "status": "review_ready",
            "approval_id": approval.get("approval_id"),
            "approval_status": approval.get("status"),
            "review_packet": packet,
            "report": render_merge_review_packet(packet),
            "mutated_approval": False,
            "next_step": f"pi /approve-merge {approval.get('approval_id')}",
        }

    def _approve_merge_directive(self, spec: DirectiveSpec) -> dict[str, Any]:
        approval_id = (spec.query or "").strip()
        approval = pending_code_merge_approval(
            settings=self.runtime.settings,
            approval_id=approval_id,
        )
        require_staff_review_provenance(
            approval,
            settings=self.runtime.settings,
        )
        resolution = run_coordination_command(
            ResolveApprovalRequest(
                approval_id=approval_id,
                decision=ApprovalDecision.APPROVE,
                resolved_by="pi:/approve-merge",
            ),
            timeout=15,
            settings=self.runtime.settings,
        )
        reach_lifecycle_transition(
            LifecycleTransitionPoint.AFTER_MERGE_APPROVAL_RESOLVED,
            approval_id=approval_id,
            decision="APPROVED",
            saga_id=str(approval.get("saga_id") or "") or None,
        )
        require_dispatch_promotion_transition(
            DispatchPromotionState.MERGE_PENDING,
            DispatchPromotionState.MERGE_APPROVED,
        )
        next_required_state = next(
            iter(next_dispatch_promotion_states(DispatchPromotionState.MERGE_APPROVED))
        )
        payload = approval.get("payload")
        merge_payload = payload if isinstance(payload, Mapping) else {}
        target_project_id = str(merge_payload.get("target_project_id") or "").strip()
        branch = str(merge_payload.get("branch") or "").strip()
        base_sha = str(merge_payload.get("base_sha") or "").strip()
        commit_sha = str(merge_payload.get("commit_sha") or "").strip()
        milestone_id = str(merge_payload.get("milestone_id") or "").strip()
        target_path = ""
        if target_project_id:
            try:
                target_path = str(
                    load_project_center(self.runtime.settings)
                    .project_by_id(target_project_id)
                    .expanded_path
                )
            except (FileNotFoundError, KeyError, ValueError):
                target_path = ""
        saga_id = str(approval.get("saga_id") or "").strip()
        saga_listing = run_coordination_command(
            ListSagas(), timeout=15, settings=self.runtime.settings
        )
        saga = next(
            (
                row
                for row in saga_listing.get("sagas", [])
                if isinstance(row, Mapping) and row.get("saga_id") == saga_id
            ),
            {},
        )
        gawd_doc_id = str(saga.get("gawd_doc_id") or "").strip()
        next_actions: list[dict[str, Any]] = [
            {
                "state": DispatchPromotionState.MERGED.value,
                "action": "merge_exact_approved_commit",
                "target_project_id": target_project_id or None,
                "target_project_path": target_path or None,
                "branch": branch or None,
                "base_sha": base_sha or None,
                "commit_sha": commit_sha or None,
                "instruction": (
                    "Confirm target main is clean and still contains the approved base, "
                    "then merge the exact approved commit."
                ),
            }
        ]
        if milestone_id:
            next_actions.extend(
                [
                    {
                        "state": DispatchPromotionState.MILESTONE_COMPLETED.value,
                        "action": "complete_milestone_after_merge",
                        "milestone_id": milestone_id,
                        "instruction": (
                            "Record milestone completion with the resulting merge commit "
                            "as evidence."
                        ),
                    },
                    {
                        "action": "dispatch_next_ready_milestone",
                        "gawd_doc_id": gawd_doc_id or None,
                        "target_project_id": target_project_id or None,
                        "instruction": (
                            "Run the approved-GAWD path again only after the merge and "
                            "milestone completion are durable."
                        ),
                    },
                ]
            )
        integration_request_id = str(resolution.get("integration_request_id") or "").strip()
        report_lines = [
            f"approved CODE_MERGE request {approval_id}; no code was merged by this command.",
            f"Next required transition: {DispatchPromotionState.MERGE_APPROVED.value} -> "
            f"{next_required_state.value}.",
        ]
        if integration_request_id:
            # The queue row exists from this moment; the loop that drains it does
            # not exist yet, which is milestone 3 of the refinery design. Saying
            # so is the difference between an operator who knows the manual merge
            # below is still the only thing that lands anything and one who waits
            # for a runner that is not there.
            report_lines.append(
                f"Queued for integration as {integration_request_id}. No refinery run drains "
                "the queue yet, so the manual steps below are still how it lands."
            )
        if target_project_id and commit_sha:
            location = f" in {target_path}" if target_path else ""
            report_lines.append(
                f"1. Confirm {target_project_id} main is clean and still contains base "
                f"{base_sha or 'recorded in the approval'}, then merge exact commit "
                f"{commit_sha} from {branch or 'the approved branch'}{location}."
            )
        else:
            report_lines.append(
                "1. Inspect the approval payload and merge its exact approved commit into "
                "the target main branch."
            )
        if milestone_id:
            report_lines.append(
                f"2. After the merge, complete milestone {milestone_id} with the merge "
                "commit recorded as evidence."
            )
        else:
            report_lines.append(
                "2. This approval has no milestone_id; do not invent a milestone "
                "transition after integration."
            )
        if milestone_id and gawd_doc_id and target_project_id:
            report_lines.append(
                "3. Dispatch the next dependency-ready milestone with: "
                f"pi /start /approved-gawd {gawd_doc_id} --target-project "
                f"{target_project_id}"
            )
        elif milestone_id:
            report_lines.append(
                "3. Re-run the approved-GAWD path to select the next dependency-ready milestone."
            )
        return {
            "schema_version": "directive_result.v1",
            "directive": spec.raw,
            "action": "approve_merge",
            "status": "approved",
            "approval_id": approval_id,
            "saga_id": saga_id or None,
            "resolution": resolution,
            "promotion_state": DispatchPromotionState.MERGE_APPROVED.value,
            "next_required_state": next_required_state.value,
            "next_actions": next_actions,
            "report": "\n".join(report_lines),
            "code_merged": False,
            "integration_request_id": integration_request_id or None,
        }

    def _ledger_directive(self, spec: DirectiveSpec) -> dict[str, Any]:
        repo_root = resolve_project_repo_root()
        script = repo_root / "scripts" / "inspect-agent-ledger.py"
        if not script.exists():
            raise FileNotFoundError(f"ledger inspection script not found: {script}")
        extra_args = shlex.split(spec.query_tail or "")
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "--root",
                str(self.runtime.settings.coordination_root),
                "--json",
                *extra_args,
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            output = (completed.stdout + completed.stderr).strip()
            raise RuntimeError(f"ledger inspection exited {completed.returncode}: {output}")
        inspection = json.loads(completed.stdout)
        return {
            "schema_version": "directive_result.v1",
            "directive": spec.raw,
            "action": "ledger",
            "status": inspection.get("status", "ok"),
            "inspection": inspection,
            "report": inspection.get("report", ""),
        }

    def _new_project_directive(
        self,
        event: IngressEvent,
        spec: DirectiveSpec,
    ) -> WorkflowResult:
        workflow_id = self._start(WorkflowType.MODEL_DIRECTIVE, event)
        artifacts: list[ArtifactRef] = []
        status = WorkflowStatus.COMPLETED
        stage = Stage.COMPLETED

        if spec.walkthru_action is not None:
            return self._gawd_walkthru_directive(spec, workflow_id)

        try:
            target_scaffold = (
                scaffold_spec(self.runtime.settings, spec.create_target_id)
                if spec.create_target_id
                else None
            )
            implementation_target = (
                validate_approved_gawd_target_project(
                    self.runtime.settings,
                    spec.target_project_id,
                )
                if spec.target_project_id
                else None
            )
            if spec.path is None:
                draft_file = create_sparse_gawd_draft_file(resolve_project_repo_root())
                draft_payload = draft_file.to_payload()
                if implementation_target is not None:
                    draft_payload["next_command"] = (
                        f"{draft_file.next_command} --target-project-id {implementation_target.id}"
                    )
                elif target_scaffold is not None:
                    draft_payload["next_command"] = (
                        f"{draft_file.next_command} --create-target {target_scaffold.project_id}"
                    )
                result = {
                    "schema_version": "directive_result.v1",
                    "directive": spec.raw,
                    "action": "new_project",
                    "status": "draft_created",
                    "draft": draft_payload,
                    "target_project_id": (
                        implementation_target.id if implementation_target else None
                    ),
                    "target_project": (
                        project_status_row(implementation_target, include_git=True)
                        if implementation_target
                        else None
                    ),
                    "target_project_scaffold": (
                        target_scaffold.to_payload() if target_scaffold else None
                    ),
                    "execution_started": False,
                    "next_step": draft_payload["next_command"],
                }
            else:
                draft = parse_sparse_gawd_draft(spec.path)
                finalized = build_reviewable_gawd_draft(draft)
                finalized_path, permissions_path = write_gawd_review_files(finalized)
                workflow_plan_scaffold = build_durable_workflow_plan(finalized)
                tasks = build_gawd_review_tasks(
                    draft,
                    workflow_plan_scaffold,
                    draft_markdown=finalized.final_markdown,
                )
                target_project = load_control_plane_target_project(self.runtime.settings)

                saga_data = run_coordination_command(
                    CreateSaga(
                        goal=f"New project intake: {draft.goal}",
                        # Key on the draft's content, not its goal or path:
                        # five sagas once shared one goal prefix.
                        content_digest=saga_content_digest(draft.raw_text),
                    ),
                    timeout=15,
                    settings=self.runtime.settings,
                )
                saga_id = saga_data["saga_id"]
                if saga_data.get("replayed"):
                    return self._already_ingested_result(
                        workflow_id=workflow_id,
                        spec=spec,
                        draft=draft,
                        saga_data=saga_data,
                        finalized_path=finalized_path,
                        permissions_path=permissions_path,
                    )

                initial_task_graph: dict[str, Any] = {
                    "schema_version": "sparse_gawd_task_graph.v1",
                    "source_draft_path": draft.source_path,
                    "draft_id": draft.draft_id,
                }
                if implementation_target is not None:
                    initial_task_graph["target_project_id"] = implementation_target.id
                elif target_scaffold is not None:
                    initial_task_graph["target_project_scaffold"] = target_scaffold.to_payload()
                initial_doc = run_coordination_command(
                    CreateGawdDoc(
                        goal=draft.goal,
                        saga_id=saga_id,
                        task_graph=initial_task_graph,
                        constraints=draft.constraints(),
                        success_criteria=draft.success_criteria(),
                        unresolved=draft.unresolved_questions,
                        acceptance_criteria=draft.acceptance_criteria(),
                    ),
                    timeout=15,
                    settings=self.runtime.settings,
                )
                run_coordination_command(
                    AttachGawdDocToSaga(saga_id, initial_doc["gawd_doc_id"]),
                    timeout=15,
                    settings=self.runtime.settings,
                )

                pow_wow_data = run_coordination_command(
                    CreatePowWow(
                        saga_id=saga_id,
                        stage="GAWD_DOC",
                        goal=f"Finalize new project GAWD doc: {draft.goal}",
                        exit_criteria=(
                            "Finalized GAWD draft and permission envelope are written; "
                            "execution remains blocked."
                        ),
                        required_outputs=(
                            "finalized_gawd_draft.v1",
                            # Named by the constant, because this is the version
                            # the artifact is actually submitted under a few
                            # lines below. Written out as a literal it stayed at
                            # v1 while the envelope moved to v2, and the pow-wow
                            # would have asked for an output nothing produced.
                            SCHEMA_VERSION_PERMISSION_ENVELOPE,
                            "durable_workflow_plan.v1",
                            "staff_final_verdict",
                        ),
                    ),
                    timeout=15,
                    settings=self.runtime.settings,
                )
                pow_wow_id = pow_wow_data["pow_wow_id"]

                task_records: dict[str, str] = {}
                for task in tasks:
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
                    task_records[task.task_name] = task_data["task_id"]

                context = PowWowExecutionContext(
                    saga_id=saga_id,
                    goal=draft.goal,
                    directive=spec.raw,
                    target_project_id=target_project.id,
                    target_project_path=str(target_project.expanded_path),
                    target_project_kind=target_project.kind,
                    target_project_status=json.dumps(
                        project_status_row(target_project, include_git=True),
                        sort_keys=True,
                    ),
                    target_project_read_only=target_project.read_only,
                    verification_commands=tuple(target_project.verification_commands),
                    personal_context_used=False,
                    no_auto_merge=not AGENT_BRANCH_AUTO_MERGE,
                    dispatch_kind="advisory",
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
                    tasks,
                    context,
                )
                coordination_events = persist_pow_wow_run_result(
                    pow_wow_id,
                    task_records,
                    run_result,
                    timeout=15,
                    settings=self.runtime.settings,
                )
                durable_workflow_plan, workflow_plan_refinement = (
                    refine_durable_workflow_plan_from_run_result(
                        workflow_plan_scaffold,
                        run_result,
                    )
                )
                # The TOML sidecar is gone. Nothing in this repository ever read it
                # back - it was written and never loaded - while the document it sat
                # beside could not be compiled. The milestones it carried now render
                # into that document instead, where `compile_design_doc` looks.
                # Rewrite the operator-facing sidecar from what actually ran: a
                # failed pow-wow must not read as ready for approval, and a
                # successful one must show the real model output, not template
                # placeholders.
                final_markdown, finalized_merge = merge_pow_wow_result_into_gawd_review_markdown(
                    finalized.final_markdown,
                    run_result,
                )
                final_markdown = replace_execution_milestones_section(
                    final_markdown,
                    render_execution_milestones_markdown(durable_workflow_plan),
                )
                # The milestones above declare IMPLEMENT work, and the compiler
                # rejects a plan that changes the system without naming terminal
                # evidence. Written here rather than in the template so it stays
                # consistent with the milestones just rendered beside it.
                final_markdown = append_required_artifacts_section(
                    final_markdown,
                    render_required_artifacts_markdown(durable_workflow_plan),
                )
                finalized_path.write_text(final_markdown, encoding="utf-8")
                finalization_succeeded = finalized_merge["status"] != "finalization_failed"

                final_task_graph = task_graph_payload(
                    finalized,
                    tasks=tasks,
                    durable_workflow_plan=durable_workflow_plan,
                    finalized_path=finalized_path,
                    permissions_path=permissions_path,
                    target_project_id=(implementation_target.id if implementation_target else None),
                    target_project_scaffold=(
                        target_scaffold.to_payload() if target_scaffold else None
                    ),
                )
                final_doc_command = CreateGawdDoc(
                    goal=draft.goal,
                    saga_id=saga_id,
                    task_graph=final_task_graph,
                    constraints=(
                        *draft.constraints(),
                        *(
                            f"Denied without approval: {permission}"
                            for permission in finalized.permission_envelope.denied_without_approval
                        ),
                    ),
                    success_criteria=draft.success_criteria(),
                    unresolved=draft.unresolved_questions,
                    acceptance_criteria=draft.acceptance_criteria(),
                )
                # Fail closed: a failed finalization creates no approvable final
                # doc and persists no milestones, so /approved-gawd has nothing
                # to act on until intake is re-run cleanly.
                final_doc = None
                active_gawd_doc = None
                saga_milestones: list[dict[str, Any]] = []
                if finalization_succeeded:
                    final_doc = run_coordination_command(
                        final_doc_command,
                        timeout=15,
                        settings=self.runtime.settings,
                    )
                    active_gawd_doc = run_coordination_command(
                        AttachGawdDocToSaga(saga_id, final_doc["gawd_doc_id"]),
                        timeout=15,
                        settings=self.runtime.settings,
                    )
                    saga_milestones = persist_durable_workflow_milestones(
                        self.runtime.settings,
                        saga_id=saga_id,
                        gawd_doc_id=final_doc["gawd_doc_id"],
                        durable_workflow_plan=durable_workflow_plan,
                    )

                extra_artifacts = [
                    run_coordination_command(
                        SubmitArtifact(
                            pow_wow_id,
                            "sparse_gawd_draft",
                            serialize_coordination_content_to_json(draft.to_payload()),
                            draft.schema_version,
                        ),
                        timeout=15,
                        settings=self.runtime.settings,
                    ),
                    run_coordination_command(
                        SubmitArtifact(
                            pow_wow_id,
                            "durable_workflow_plan_model_refinement",
                            serialize_coordination_content_to_json(workflow_plan_refinement),
                            workflow_plan_refinement["schema_version"],
                        ),
                        timeout=15,
                        settings=self.runtime.settings,
                    ),
                    run_coordination_command(
                        SubmitArtifact(
                            pow_wow_id,
                            "finalized_gawd_model_merge",
                            serialize_coordination_content_to_json(finalized_merge),
                            finalized_merge["schema_version"],
                        ),
                        timeout=15,
                        settings=self.runtime.settings,
                    ),
                    run_coordination_command(
                        SubmitArtifact(
                            pow_wow_id,
                            "finalized_gawd_draft",
                            serialize_coordination_content_to_json(finalized.to_payload()),
                            finalized.schema_version,
                        ),
                        timeout=15,
                        settings=self.runtime.settings,
                    ),
                    run_coordination_command(
                        SubmitArtifact(
                            pow_wow_id,
                            "permission_envelope",
                            serialize_coordination_content_to_json(
                                finalized.permission_envelope.to_payload()
                            ),
                            finalized.permission_envelope.schema_version,
                        ),
                        timeout=15,
                        settings=self.runtime.settings,
                    ),
                    run_coordination_command(
                        SubmitArtifact(
                            pow_wow_id,
                            "durable_workflow_plan",
                            serialize_coordination_content_to_json(
                                durable_workflow_plan.to_payload()
                            ),
                            durable_workflow_plan.schema_version,
                        ),
                        timeout=15,
                        settings=self.runtime.settings,
                    ),
                ]

                completed_pow_wow = run_coordination_command(
                    CompletePowWow(
                        pow_wow_id,
                        run_result.output_summary,
                        map_pow_wow_run_status_to_ledger_status(run_result.status),
                    ),
                    timeout=15,
                    settings=self.runtime.settings,
                )

                artifact_ids = [
                    str(event["artifact_id"])
                    for event in (*coordination_events, *extra_artifacts)
                    if isinstance(event.get("artifact_id"), str)
                ]
                final_gawd_doc_id = final_doc["gawd_doc_id"] if final_doc else None
                try:
                    project_center = load_project_center(self.runtime.settings)
                    available_targets = ", ".join(
                        project.id for project in project_center.projects if not project.read_only
                    )
                except Exception:
                    available_targets = "(could not load configs/linked_projects.toml)"
                if finalization_succeeded:
                    status_line = "finalized_pending_operator_approval"
                    if implementation_target is not None:
                        next_command = (
                            f"pi /start /approved-gawd {final_gawd_doc_id} "
                            f"--target-project {implementation_target.id}"
                        )
                    elif target_scaffold is not None:
                        next_command = f"pi /start /approved-gawd {final_gawd_doc_id}"
                    else:
                        next_command = (
                            f"pi /start /approved-gawd {final_gawd_doc_id} "
                            "--target-project <project_id>"
                        )
                else:
                    status_line = "finalization_failed"
                    next_command = (
                        "fix the failed task(s) above, then re-run: "
                        f"pi /start /new-project {draft.source_path}"
                    )
                    if implementation_target is not None:
                        next_command += f" --target-project-id {implementation_target.id}"
                    elif target_scaffold is not None:
                        next_command += f" --create-target {target_scaffold.project_id}"
                result = {
                    "schema_version": "directive_result.v1",
                    "directive": spec.raw,
                    "action": "new_project",
                    "status": status_line,
                    "execution_started": False,
                    "draft": draft.to_payload(),
                    "finalized_path": str(finalized_path),
                    "permissions_path": str(permissions_path),
                    "permission_envelope": finalized.permission_envelope.to_payload(),
                    "durable_workflow_plan": durable_workflow_plan.to_payload(),
                    "workflow_plan_refinement": workflow_plan_refinement,
                    "finalized_model_merge": finalized_merge,
                    "saga": saga_data,
                    "saga_id": saga_id,
                    "initial_gawd_doc": initial_doc,
                    "initial_gawd_doc_id": initial_doc["gawd_doc_id"],
                    "final_gawd_doc": final_doc,
                    "final_gawd_doc_id": final_gawd_doc_id,
                    "active_gawd_doc": active_gawd_doc,
                    "saga_milestones": saga_milestones,
                    "target_project_id": (
                        implementation_target.id if implementation_target else None
                    ),
                    "target_project": (
                        project_status_row(implementation_target, include_git=True)
                        if implementation_target
                        else None
                    ),
                    "target_project_scaffold": (
                        target_scaffold.to_payload() if target_scaffold else None
                    ),
                    "finalization_project_id": target_project.id,
                    "available_target_projects": available_targets,
                    "pow_wow": pow_wow_data,
                    "completed_pow_wow": completed_pow_wow,
                    "pow_wow_id": pow_wow_id,
                    "tasks": [
                        {
                            **task.to_payload(),
                            "task_id": task_records[task.task_name],
                        }
                        for task in tasks
                    ],
                    "executor_result": run_result.to_payload(),
                    "executor_config_source": executor_config_source,
                    "executor_worktree_root": (
                        str(executor_worktree_root) if executor_worktree_root else None
                    ),
                    "artifact_ids": artifact_ids,
                    "approval_required": finalization_succeeded,
                    "approval_subject": "final_gawd_doc",
                    "approval_note": (
                        "Review the finalized GAWD draft, permission envelope, and "
                        "durable workflow plan before starting implementation."
                        if finalization_succeeded
                        else "Finalization failed; no approvable final doc was created."
                    ),
                    "report": "\n".join(
                        (
                            f"finalization_status: {status_line}",
                            f"saga_id: {saga_id}",
                            f"initial_gawd_doc_id: {initial_doc['gawd_doc_id']}",
                            f"final_gawd_doc_id: {final_gawd_doc_id}",
                            f"pow_wow_id: {pow_wow_id}",
                            f"target_project (finalization ran here): {target_project.id}",
                            "implementation_target_project: "
                            + (
                                implementation_target.id
                                if implementation_target
                                else (
                                    f"scaffold {target_scaffold.project_id} at approval"
                                    if target_scaffold
                                    else "select at approval"
                                )
                            ),
                            f"available --target-project ids: {available_targets}",
                            f"finalized_path: {finalized_path}",
                            f"permissions_path: {permissions_path}",
                            f"finalized_document: {finalized_path}",
                            "execution_started: false",
                            f"next_command: {next_command}",
                        )
                        if finalization_succeeded
                        # A failed finalization produced nothing reviewable, so
                        # the report must not advertise doc ids or sidecar paths
                        # as if it had. It names what failed, why, where the
                        # diagnosis lives, and the forensic handles.
                        else (
                            f"finalization_status: {status_line}",
                            *(
                                f"failed_task: {item}"
                                for item in finalized_merge.get("failed_tasks", [])
                            ),
                            "cause: "
                            + " ".join(
                                (
                                    run_result.risks[0]
                                    if run_result.risks
                                    else run_result.output_summary
                                ).split()
                            ),
                            f"failure_report: {finalized_path}",
                            f"saga_id (forensics): {saga_id}",
                            f"pow_wow_id (forensics): {pow_wow_id}",
                            "execution_started: false",
                            f"next_command: {next_command}",
                        )
                    ),
                }
        except Exception as exc:
            status = WorkflowStatus.FAILED_PERMANENT
            result = {
                "schema_version": "directive_result.v1",
                "directive": spec.raw,
                "action": "new_project",
                "status": "failed",
                "error": str(exc),
                "help": help_payload(DirectiveParser(self.runtime.settings), spec.raw, str(exc)),
            }

        artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.DIRECTIVE_RESULT.value,
            payload=result,
            workflow_id=workflow_id,
            schema_version="directive_result.v1",
        )
        artifacts.append(artifact)
        self.runtime.repository.update_workflow(
            workflow_id,
            status=status,
            stage=stage,
            error=result.get("error"),
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.MODEL_DIRECTIVE,
            status,
            stage,
            artifacts,
            manual_review_reason=(
                result.get("approval_note") if result.get("approval_required") else None
            ),
            help=result.get("help"),
        )

    def _already_ingested_result(
        self,
        *,
        workflow_id: str,
        spec: DirectiveSpec,
        draft: SparseGawdDraft,
        saga_data: Mapping[str, Any],
        finalized_path: Path,
        permissions_path: Path,
    ) -> WorkflowResult:
        """Report a replayed intake instead of performing it a second time.

        ``create_saga`` recognized this draft's content digest, so the saga and
        everything hanging off it already exist. Continuing would re-run
        finalization, add a second GAWD doc pair and pow-wow, and only then die
        on the milestone primary key, which is deterministic precisely so a
        second write cannot land. Refusing here costs one completed command and
        skips all of it.

        The milestones are read back rather than omitted because "already
        ingested as saga X, and here is where X stands" is the answer an
        operator re-running a command actually wants, and by this point it is
        one query.

        This completes rather than fails: nothing went wrong. A failure status
        would be a lie about a command whose work was simply already done.
        """

        saga_id = str(saga_data["saga_id"])
        raw_milestones = run_coordination_command(
            ListSagaMilestones(saga_id),
            timeout=15,
            settings=self.runtime.settings,
        ).get("milestones", [])
        milestones = [dict(item) for item in raw_milestones if isinstance(item, Mapping)]
        pending = [item for item in milestones if str(item.get("status")) == "PENDING"]
        next_command = (
            f"pi /start /approved-gawd {saga_data.get('gawd_doc_id')}"
            if saga_data.get("gawd_doc_id")
            else f"pi /saga status {saga_id}"
        )
        result = {
            "schema_version": "directive_result.v1",
            "directive": spec.raw,
            "action": "new_project",
            "status": "already_ingested",
            "execution_started": False,
            "replayed": True,
            "draft": draft.to_payload(),
            "saga": dict(saga_data),
            "saga_id": saga_id,
            "saga_milestones": milestones,
            # The sidecars are rewritten before intake knows this is a replay,
            # so naming them keeps the report honest about what touched disk.
            "finalized_path": str(finalized_path),
            "permissions_path": str(permissions_path),
            "next_step": next_command,
            "report": "\n".join(
                (
                    "finalization_status: already_ingested",
                    f"saga_id: {saga_id}",
                    f"saga_status: {saga_data.get('status') or 'unknown'}",
                    f"milestones: {len(milestones)} total, {len(pending)} pending",
                    f"source_draft: {draft.source_path}",
                    "execution_started: false",
                    f"next_command: {next_command}",
                )
            ),
        }
        artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.DIRECTIVE_RESULT.value,
            payload=result,
            workflow_id=workflow_id,
            schema_version="directive_result.v1",
        )
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.COMPLETED,
            stage=Stage.COMPLETED,
            error=None,
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.MODEL_DIRECTIVE,
            WorkflowStatus.COMPLETED,
            Stage.COMPLETED,
            [artifact],
        )

    def _gawd_walkthru_directive(
        self,
        spec: DirectiveSpec,
        workflow_id: str,
    ) -> WorkflowResult:
        artifacts: list[ArtifactRef] = []
        status = WorkflowStatus.COMPLETED
        try:
            store = GawdWalkthruStore(resolve_project_repo_root())
            operation_id = sha256_text(spec.raw)
            action = spec.walkthru_action
            if action == "start":
                if spec.target_project_id:
                    validate_approved_gawd_target_project(
                        self.runtime.settings,
                        spec.target_project_id,
                    )
                walkthru = store.start(
                    target_project_id=spec.target_project_id,
                    create_target_id=spec.create_target_id,
                    operation_id=operation_id,
                )
            else:
                walkthru_id = spec.walkthru_id or ""

                summarize = GawdWalkthruSummarizer(self.runtime, workflow_id)

                if action == "answer":
                    walkthru = store.answer(
                        walkthru_id,
                        spec.walkthru_text or "",
                        operation_id=operation_id,
                        summarize=summarize,
                    )
                    artifacts.extend(summarize.artifacts)
                elif action == "accept":
                    walkthru = store.accept_proposed_summary(
                        walkthru_id,
                        operation_id=operation_id,
                    )
                elif action == "revise":
                    walkthru = store.revise_proposed_summary(
                        walkthru_id,
                        spec.walkthru_text or "",
                        operation_id=operation_id,
                    )
                elif action == "skip":
                    walkthru = store.skip_section(
                        walkthru_id,
                        operation_id=operation_id,
                    )
                elif action == "edit":
                    walkthru = store.edit_accepted_summary(
                        walkthru_id,
                        spec.walkthru_section_id or "",
                        spec.walkthru_text or "",
                        operation_id=operation_id,
                    )
                elif action == "status":
                    walkthru = store.read_status(walkthru_id)
                elif action == "finish":
                    walkthru = store.write_completed_sparse_gawd_draft(
                        walkthru_id,
                        operation_id=operation_id,
                    )
                else:
                    raise ValueError(f"Unsupported walkthru action: {action}")
            result = {
                "schema_version": "directive_result.v1",
                "directive": spec.raw,
                "action": "new_project_walkthru",
                **walkthru,
            }
        except Exception as exc:
            status = WorkflowStatus.FAILED_PERMANENT
            result = {
                "schema_version": "directive_result.v1",
                "directive": spec.raw,
                "action": "new_project_walkthru",
                "status": "failed",
                "execution_started": False,
                "error": str(exc),
                "help": help_payload(
                    DirectiveParser(self.runtime.settings),
                    spec.raw,
                    str(exc),
                ),
            }
        artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.DIRECTIVE_RESULT.value,
            payload=result,
            workflow_id=workflow_id,
            schema_version="directive_result.v1",
        )
        artifacts.append(artifact)
        self.runtime.repository.update_workflow(
            workflow_id,
            status=status,
            stage=Stage.COMPLETED,
            error=result.get("error"),
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.MODEL_DIRECTIVE,
            status,
            Stage.COMPLETED,
            artifacts,
            help=result.get("help"),
        )

    def _approved_gawd_directive(
        self,
        event: IngressEvent,
        spec: DirectiveSpec,
    ) -> WorkflowResult:
        workflow_id = self._start(WorkflowType.MODEL_DIRECTIVE, event)
        artifacts: list[ArtifactRef] = []
        status = WorkflowStatus.COMPLETED
        stage = Stage.COMPLETED
        shortcut_resolution: dict[str, Any] | None = None

        try:
            gawd_doc_id = (spec.query or "").strip()
            if spec.action == "approve_most_recent":
                shortcut_resolution = _find_latest_dependency_ready_gawd(self.runtime.settings)
                gawd_doc_id = str(shortcut_resolution["gawd_doc_id"])
            if not gawd_doc_id:
                raise ValueError("/start /approved-gawd requires a final_gawd_doc_id.")
            doc_data = run_coordination_command(
                GetGawdDoc(gawd_doc_id),
                timeout=15,
                settings=self.runtime.settings,
            )
            gawd_doc = doc_data["gawd_doc"]
            doc_status = str(gawd_doc.get("status") or "")
            if doc_status not in {"DRAFT", "APPROVED"}:
                raise ValueError(
                    "Only DRAFT or APPROVED GAWD docs can start implementation; "
                    f"{gawd_doc_id} is {doc_status or 'UNKNOWN'}."
                )
            saga_id = str(gawd_doc.get("saga_id") or "").strip()
            if not saga_id:
                raise ValueError("Approved GAWD doc must be attached to a saga before execution.")
            target_scaffold = build_target_project_scaffold_from_gawd_doc(gawd_doc)
            target_history: dict[str, Any] | None = None
            if (
                spec.action == "approve_most_recent"
                and not spec.target_project_id
                and not spec.create_target_id
                and target_scaffold is None
                and extract_target_project_id_from_gawd_doc(gawd_doc) is None
            ):
                target_history = resolve_target_project_from_gawd_dispatch_history(
                    self.runtime.settings,
                    gawd_doc_id,
                )
            target_project_id = resolve_approved_gawd_target_project_id(
                spec,
                gawd_doc,
                inferred_target_project_id=(
                    str(target_history["target_project_id"]) if target_history is not None else None
                ),
            )
            if shortcut_resolution is not None:
                shortcut_resolution["target_project_id"] = target_project_id
                shortcut_resolution["target_resolution"] = target_history or {
                    "target_project_id": target_project_id,
                    "source": "approved_gawd_contract",
                }
            if target_scaffold is None and spec.create_target_id:
                target_scaffold = scaffold_spec(
                    self.runtime.settings,
                    spec.create_target_id,
                )
            target_project = (
                None
                if target_scaffold is not None
                else validate_approved_gawd_target_project(
                    self.runtime.settings,
                    target_project_id,
                )
            )
            if doc_status == "DRAFT":
                approval = run_coordination_command(
                    ApproveGawdDoc(gawd_doc_id),
                    timeout=15,
                    settings=self.runtime.settings,
                )
                doc_data = run_coordination_command(
                    GetGawdDoc(gawd_doc_id),
                    timeout=15,
                    settings=self.runtime.settings,
                )
                gawd_doc = doc_data["gawd_doc"]
            else:
                approval = {
                    "ok": True,
                    "gawd_doc_id": gawd_doc_id,
                    "status": "APPROVED",
                    "already_approved": True,
                }
            scaffold_result = None
            if target_scaffold is not None:
                task_graph = gawd_doc.get("task_graph")
                finalized_path = (
                    task_graph.get("finalized_path") if isinstance(task_graph, Mapping) else None
                )
                if not isinstance(finalized_path, str) or not finalized_path.strip():
                    raise ValueError(
                        "Approved scaffold contract is missing its finalized GAWD path."
                    )
                scaffold_result = scaffold_target_project(
                    target_scaffold,
                    settings=self.runtime.settings,
                    finalized_gawd_path=Path(finalized_path).expanduser().resolve(),
                )
                target_project = validate_approved_gawd_target_project(
                    self.runtime.settings,
                    target_project_id,
                )
            if target_project is None:
                raise RuntimeError("Approved GAWD target project was not prepared.")
            active_gawd_doc = run_coordination_command(
                AttachGawdDocToSaga(saga_id, gawd_doc_id),
                timeout=15,
                settings=self.runtime.settings,
            )
            saga_milestones = ensure_approved_gawd_milestones(
                self.runtime.settings,
                saga_id=saga_id,
                gawd_doc=gawd_doc,
            )
            ready_milestone = run_coordination_command(
                NextReadySagaMilestone(saga_id),
                timeout=15,
                settings=self.runtime.settings,
            )
            milestone_approval = None
            if ready_milestone.get("milestone") is None:
                milestone_approval = approve_next_dependency_ready_milestone(
                    self.runtime.settings,
                    saga_id=saga_id,
                    gawd_doc_id=gawd_doc_id,
                    target_project_id=target_project_id,
                    blocked=ready_milestone.get("blocked", []),
                )
                if milestone_approval is not None:
                    ready_milestone = run_coordination_command(
                        NextReadySagaMilestone(saga_id),
                        timeout=15,
                        settings=self.runtime.settings,
                    )
            milestone = ready_milestone.get("milestone")
            if milestone is None:
                approved_merge_requests = run_coordination_command(
                    ListApprovalRequests(saga_id=saga_id, status="APPROVED"),
                    timeout=15,
                    settings=self.runtime.settings,
                ).get("requests", [])
                guidance = _build_no_ready_milestone_guidance(
                    gawd_doc_id=gawd_doc_id,
                    target_project_id=target_project_id,
                    target_project_path=target_project.expanded_path,
                    coordination_root=self.runtime.settings.coordination_root.expanduser(),
                    saga_milestones=saga_milestones,
                    blocked_milestones=ready_milestone.get("blocked", []),
                    approved_requests=approved_merge_requests,
                )
                result = {
                    "schema_version": "directive_result.v1",
                    "directive": spec.raw,
                    "action": "approved_gawd",
                    "requested_action": spec.action,
                    "resolution": shortcut_resolution,
                    "status": "no_ready_milestone",
                    "gawd_doc_id": gawd_doc_id,
                    "saga_id": saga_id,
                    "approval": approval,
                    "active_gawd_doc": active_gawd_doc,
                    "target_project_id": target_project_id,
                    "target_project": project_status_row(target_project, include_git=True),
                    "target_project_scaffold_result": scaffold_result,
                    "saga_milestones": saga_milestones,
                    "milestone_approval": milestone_approval,
                    "blocked_milestones": ready_milestone.get("blocked", []),
                    "blocker_details": guidance["blocker_details"],
                    "next_actions": guidance["next_actions"],
                    "execution_enqueued": False,
                    "execution_started": False,
                    "next_step": guidance["next_step"],
                    "note": guidance["note"],
                    "report": guidance["report"],
                }
                artifact = self.runtime.artifact_store.write_json(
                    role=ArtifactRole.DIRECTIVE_RESULT.value,
                    payload=result,
                    workflow_id=workflow_id,
                    schema_version="directive_result.v1",
                )
                artifacts.append(artifact)
                self.runtime.repository.update_workflow(
                    workflow_id,
                    status=status,
                    stage=stage,
                    error=None,
                )
                return build_completed_workflow_result(
                    workflow_id,
                    WorkflowType.MODEL_DIRECTIVE,
                    status,
                    stage,
                    artifacts,
                )

            reach_lifecycle_transition(
                LifecycleTransitionPoint.AFTER_MILESTONE_SELECTED,
                saga_id=saga_id,
                milestone_id=str(milestone["milestone_id"]),
                gawd_doc_id=gawd_doc_id,
                target_project_id=target_project_id,
            )
            source = build_approved_gawd_milestone_dispatch_source(
                gawd_doc_id,
                str(milestone["milestone_id"]),
            )
            existing_intent = find_existing_dispatch_intent_for_source(
                self.runtime.settings,
                source,
            )
            if (
                existing_intent is not None
                and existing_intent.get("target_project_id") != target_project_id
            ):
                raise ValueError(
                    "An active dispatch intent already exists for this approved GAWD "
                    "milestone with "
                    f"target_project_id={existing_intent.get('target_project_id')!r}. "
                    "Cancel or supersede that intent before enqueueing a different target."
                )
            if existing_intent is None:
                dispatch_intent = run_coordination_command(
                    SubmitDispatchIntent(
                        DispatchTier.SENIOR,
                        build_approved_gawd_dispatch_prompt(gawd_doc, milestone),
                        kind=DispatchKind.CODE,
                        target_project_id=target_project_id,
                        source=source,
                    ),
                    timeout=15,
                    settings=self.runtime.settings,
                )
                reach_lifecycle_transition(
                    LifecycleTransitionPoint.AFTER_INTENT_CREATED,
                    intent_id=str(dispatch_intent["intent_id"]),
                    milestone_id=str(milestone["milestone_id"]),
                    saga_id=saga_id,
                    target_project_id=target_project_id,
                )
                result_status = "approved_and_enqueued"
                execution_enqueued = True
            else:
                dispatch_intent = existing_intent
                result_status = "already_enqueued"
                execution_enqueued = False

            intent_status = str(dispatch_intent.get("status") or "PENDING")
            if intent_status == "PENDING":
                next_step = "pi /dispatch"
                execution_note = "The intent is durable but unclaimed. Run the dispatcher once now."
            else:
                next_step = "pi /ledger"
                execution_note = (
                    f"The intent is already {intent_status}; inspect the ledger before "
                    "starting another dispatcher."
                )
            ready_name = str(milestone.get("name") or milestone["milestone_id"])
            report = "\n".join(
                [
                    f"{result_status}: {ready_name}",
                    f"Milestone: {milestone['milestone_id']}",
                    f"Dispatch intent: {dispatch_intent.get('intent_id')}",
                    f"Intent status: {intent_status}",
                    "Execution started: no",
                    execution_note,
                    f"Next: {next_step}",
                ]
            )

            result = {
                "schema_version": "directive_result.v1",
                "directive": spec.raw,
                "action": "approved_gawd",
                "requested_action": spec.action,
                "resolution": shortcut_resolution,
                "status": result_status,
                "gawd_doc_id": gawd_doc_id,
                "saga_id": saga_id,
                "approval": approval,
                "active_gawd_doc": active_gawd_doc,
                "target_project_id": target_project_id,
                "target_project": project_status_row(target_project, include_git=True),
                "target_project_scaffold_result": scaffold_result,
                "dispatch_source": source,
                "dispatch_intent": dispatch_intent,
                "dispatch_intent_id": dispatch_intent.get("intent_id"),
                "saga_milestones": saga_milestones,
                "milestone_approval": milestone_approval,
                "ready_milestone": milestone,
                "execution_enqueued": execution_enqueued,
                "execution_started": False,
                "next_step": next_step,
                "note": execution_note,
                "report": report,
            }
        except Exception as exc:
            status = WorkflowStatus.FAILED_PERMANENT
            result = {
                "schema_version": "directive_result.v1",
                "directive": spec.raw,
                "action": "approved_gawd",
                "requested_action": spec.action,
                "resolution": shortcut_resolution,
                "status": "failed",
                "error": str(exc),
                "help": help_payload(DirectiveParser(self.runtime.settings), spec.raw, str(exc)),
            }

        artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.DIRECTIVE_RESULT.value,
            payload=result,
            workflow_id=workflow_id,
            schema_version="directive_result.v1",
        )
        artifacts.append(artifact)
        self.runtime.repository.update_workflow(
            workflow_id,
            status=status,
            stage=stage,
            error=result.get("error"),
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.MODEL_DIRECTIVE,
            status,
            stage,
            artifacts,
            help=result.get("help"),
        )

    def _observability_directive(self, spec: DirectiveSpec) -> dict[str, Any]:
        """Bring the observability stack up or down via scripts/start-local-observability.sh.

        `/start /logging` and `/stop /logging` route here instead of the model
        registry — the telemetry containers (alloy/tempo/pyroscope/loki + their
        MinIO backend) are Docker services, not llama models.
        """
        verb = spec.query if spec.query in {"up", "down"} else "up"
        repo_root = Path(__file__).resolve().parents[3]
        script = repo_root / "scripts" / "start-local-observability.sh"
        if not script.exists():
            raise FileNotFoundError(f"observability script not found: {script}")
        completed = subprocess.run(
            ["bash", str(script), verb],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        output = (completed.stdout + completed.stderr).strip()
        if completed.returncode != 0:
            raise RuntimeError(
                "scripts/start-local-observability.sh "
                f"{verb} exited {completed.returncode}: {output}"
            )
        return {
            "schema_version": "directive_result.v1",
            "directive": spec.raw,
            "action": "observability",
            "verb": verb,
            "status": "started" if verb == "up" else "stopped",
            "output": output,
        }

    def _status_directive(self) -> dict[str, Any]:
        """Report which models are loaded across the llama-server router and
        the whisper ASR backend. A pure read — never mutates model state."""
        settings = self.runtime.settings
        llama = self.runtime.model_manager.model_status()
        whisper_up = self.runtime.audio_transcriber.is_available()

        lines: list[str] = []
        header = f"llama-server  {settings.llama_base_url}"
        lines.append(header if llama["reachable"] else f"{header}  (unreachable)")
        role_width = max((len(row["role"]) for row in llama["roles"]), default=4)
        for row in llama["roles"]:
            marker = "  <- active general" if row["active_general"] else ""
            lines.append(
                f"  {row['role']:<{role_width}}  {row['status']:<10}  {row['model']}{marker}"
            )
        lines.append("")
        whisper_base_url = self.runtime.audio_transcriber.base_url
        lines.append(f"whisper-server  {whisper_base_url}  ({'running' if whisper_up else 'down'})")
        return {
            "schema_version": "directive_result.v1",
            "action": "status",
            "status": "ok",
            "llama": llama,
            "whisper": {"reachable": whisper_up, "base_url": whisper_base_url},
            "report": "\n".join(lines),
        }

    # ------------------------------------------------------------------
    # Saga / pow-wow directive handler
    # ------------------------------------------------------------------


def engine() -> WorkflowEngine:
    return WorkflowEngine(get_runtime())


def _drain_general_questions(event: IngressEvent) -> WorkflowResult:
    return engine().general_questions(event)


def run_workflow(workflow_type: WorkflowType, event: IngressEvent) -> WorkflowResult:
    workflow_id = build_event_workflow_id(workflow_type, event)
    router = {
        WorkflowType.GENERAL_QUESTIONS: _drain_general_questions,
        WorkflowType.MODEL_DIRECTIVE: engine().model_directive,
        WorkflowType.AGENT_QUERY: engine().agent_query,
        WorkflowType.OCR_CAPTURE: engine().ocr_capture,
        WorkflowType.DIRECTORY_EMBEDDING: engine().directory_embedding,
        WorkflowType.CONTEXT_COMPACTION: engine().context_compaction,
        WorkflowType.WHITEBOARD_OCR: engine().whiteboard_ocr,
        WorkflowType.PAPER_NOTES_OCR: engine().paper_notes_ocr,
        WorkflowType.APPLE_NOTES_SYNC: engine().apple_notes_sync,
        WorkflowType.WORKFLOWY_SYNC: engine().workflowy_sync,
        WorkflowType.WORKFLOWY_WRITE: engine().workflowy_write,
        WorkflowType.AUDIO_TRANSCRIPTION: engine().audio_transcription,
        WorkflowType.MEDICAL_IMAGE_ANALYZER: engine().medical_image_analyzer,
        WorkflowType.TRAINING_EXPORT_STUB: engine().training_export_stub,
        WorkflowType.SEND_TO_WORKFLOWY: engine().send_to_workflowy,
        WorkflowType.DONE_RECALL: engine().done_recall,
        WorkflowType.CHROME_CONTROL: engine().chrome_control,
        WorkflowType.WHITEBOARD_INTENT: engine().whiteboard_intent,
        WorkflowType.CREATE_TOMORROW: engine().create_tomorrow,
        WorkflowType.GRAPH_EXTRACTION: engine().graph_extraction,
        WorkflowType.GRAPH_ANALYTICS: engine().graph_analytics,
    }
    started = time.perf_counter()
    status = "failed"
    with observability_context(
        workflow_type=workflow_type.value,
        workflow_id=workflow_id,
        step="workflow",
    ):
        WORKFLOW_ACTIVE.labels(workflow_type=workflow_type.value).inc()
        logger.info("workflow_started")
        try:
            with profiled_step(
                "workflow",
                workflow_type=workflow_type.value,
                workflow_id=workflow_id,
            ):
                result = router[workflow_type](event)
            status = result.status.value
            logger.info("workflow_completed")
            return result
        except Exception:
            logger.exception("workflow_failed")
            raise
        finally:
            elapsed = time.perf_counter() - started
            WORKFLOW_ACTIVE.labels(workflow_type=workflow_type.value).dec()
            WORKFLOW_RUNS_TOTAL.labels(workflow_type=workflow_type.value, status=status).inc()
            WORKFLOW_LATENCY_SECONDS.labels(
                workflow_type=workflow_type.value,
                status=status,
            ).observe(elapsed)


def parse_workflow_from_payload(workflow_type: str, payload: dict[str, Any]) -> WorkflowResult:
    event = IngressEvent.model_validate(payload)
    return run_workflow(WorkflowType(workflow_type), event)
