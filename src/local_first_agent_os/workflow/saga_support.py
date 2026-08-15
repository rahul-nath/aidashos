# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Approved-GAWD and saga dispatch support operations."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..contracts import (
    DirectiveSpec,
)
from ..coordination import (
    ApprovalDecision,
    CreateSagaMilestone,
    ListApprovalRequests,
    ListDispatchIntents,
    ListSagaMilestones,
    ResolveApprovalRequest,
    SubmitApprovalRequest,
)
from ..coordination.milestones import SAGA_MILESTONE_SOURCE_MARKER
from ..pow_wow import (
    CliPowWowExecutor,
    DryRunPowWowExecutor,
    FakeProcessPowWowExecutor,
    PowWowExecutor,
)
from ..pow_wow.ledger import (
    run_coordination_command,
    run_typed_coordination_command,
)
from ..project_access import AccessMode, ProjectAccessPolicy
from ..project_center import LinkedProject, load_project_center
from ..project_scaffold import TargetProjectScaffold
from ..staffing import load_bench

logger = logging.getLogger(__name__)


def map_pow_wow_run_status_to_ledger_status(run_status: str) -> str:
    if run_status == "DRY_RUN_COMPLETED":
        return "COMPLETED"
    if run_status in {"COMPLETED", "VERIFICATION_FAILED", "FAILED", "BLOCKED"}:
        return run_status
    return "FAILED"


def run_coroutine_blocking(coro: Any) -> Any:
    """Run a coroutine to completion from sync code, safe whether or not an
    event loop is already running in this thread."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


def build_saga_executor(
    settings: Any,
    spec: Any,
    *,
    delegate_fn: Callable[..., Mapping[str, Any]] | None = None,
    artifact_writer: Any | None = None,
    dependency_compactor: Any | None = None,
) -> tuple[PowWowExecutor, str, Path | None]:
    """Build the executor a /saga directive runs on.

    ``dependency_compactor`` is optional for the same reason it is everywhere:
    unset means the dependency block truncates on overflow, which is safe. This
    builder takes ``settings`` rather than a runtime, so it cannot construct
    the runtime-backed compactor itself; a caller holding a runtime passes one
    in, and the fake-process backend ignores it because there is no model in a
    fake process.
    """

    executor_backend = spec.saga_executor_backend or settings.saga_executor_backend
    config_source = (
        "directive" if spec.saga_executor_backend or spec.saga_worktree_root else "runtime_settings"
    )
    if executor_backend == "fake_process":
        worktree_root = (spec.saga_worktree_root or settings.saga_worktree_root).expanduser()
        return (
            FakeProcessPowWowExecutor(worktree_root=worktree_root),
            config_source,
            worktree_root,
        )
    if executor_backend == "cli":
        worktree_root = (spec.saga_worktree_root or settings.saga_worktree_root).expanduser()
        return (
            CliPowWowExecutor(
                worktree_root=worktree_root,
                timeout_seconds=settings.saga_task_timeout_seconds,
                max_review_rounds=settings.saga_max_review_rounds,
                bench=load_bench(settings.config_dir / "staffing.toml"),
                delegate_fn=delegate_fn,
                dependency_compactor=dependency_compactor,
                coordination_command=lambda command: run_typed_coordination_command(
                    command,
                    settings=settings,
                ),
                artifact_writer=artifact_writer,
                coordination_timeout_seconds=settings.coordination_command_timeout_seconds,
                git_timeout_seconds=settings.git_operation_timeout_seconds,
                progress_assessment_timeout_seconds=(settings.progress_assessment_timeout_seconds),
                artifact_write_timeout_seconds=settings.artifact_write_timeout_seconds,
                stream_drain_timeout_seconds=settings.stream_drain_timeout_seconds,
            ),
            config_source,
            worktree_root,
        )
    return DryRunPowWowExecutor(), config_source, None


def resolve_project_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def load_control_plane_target_project(settings: Any) -> LinkedProject:
    try:
        center = load_project_center(settings)
        return center.project_by_id(center.control_plane_project)
    except Exception:
        repo_root = resolve_project_repo_root()
        return LinkedProject(
            id="local_first_agent_os",
            kind="control_plane",
            path=repo_root,
            status="active_center",
            access=ProjectAccessPolicy(
                mode=AccessMode.READ_WRITE,
                owns=("coordination", "gawd_intake"),
                avoid=("unapproved_execution",),
            ),
            description="local agent control plane",
            primary_interfaces=["pi"],
            verification_commands=["UV_CACHE_DIR=/tmp/uv-cache uv run pytest"],
        )


def _extend_repeated_arg(args: list[str], flag: str, values: tuple[str, ...]) -> None:
    cleaned = tuple(value for value in values if value.strip())
    if cleaned:
        args.append(flag)
        args.extend(cleaned)


APPROVED_GAWD_DISPATCH_SOURCE_PREFIX = "approved_gawd:"
APPROVED_GAWD_DUPLICATE_GUARD_STATUSES = {
    "PENDING",
    "CLAIMED",
    "CHECKPOINT_REVIEW",
    "PAUSED",
    "SUPERSEDED",
    "DONE",
}


def build_approved_gawd_dispatch_source(gawd_doc_id: str) -> str:
    return f"{APPROVED_GAWD_DISPATCH_SOURCE_PREFIX}{gawd_doc_id}"


def build_approved_gawd_milestone_dispatch_source(gawd_doc_id: str, milestone_id: str) -> str:
    """The one place a milestone-linked dispatch source is assembled.

    The marker comes from the module that parses it back, because the format is
    genuinely one decision written and read together. An empty id would produce a
    source the parser has to report as malformed, so it is refused here instead:
    the caller has the id in hand and a blank one is a programmer error.
    """

    if not milestone_id.strip():
        raise ValueError("a milestone dispatch source needs a milestone id")
    prefix = build_approved_gawd_dispatch_source(gawd_doc_id)
    return f"{prefix}{SAGA_MILESTONE_SOURCE_MARKER}{milestone_id}"


def find_existing_dispatch_intent_for_source(
    settings: Any,
    source: str,
) -> dict[str, Any] | None:
    intents = run_coordination_command(
        ListDispatchIntents(),
        timeout=15,
        settings=settings,
    ).get("intents", [])
    for intent in intents:
        if (
            intent.get("source") == source
            and intent.get("status") in APPROVED_GAWD_DUPLICATE_GUARD_STATUSES
        ):
            return intent
    return None


def extract_target_project_id_from_gawd_doc(gawd_doc: Mapping[str, Any]) -> str | None:
    task_graph = gawd_doc.get("task_graph")
    if not isinstance(task_graph, Mapping):
        return None
    for key in ("target_project_id", "target_project", "project_id"):
        value = task_graph.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def build_target_project_scaffold_from_gawd_doc(
    gawd_doc: Mapping[str, Any],
) -> TargetProjectScaffold | None:
    task_graph = gawd_doc.get("task_graph")
    if not isinstance(task_graph, Mapping):
        return None
    raw = task_graph.get("target_project_scaffold")
    if raw is None:
        return None
    return TargetProjectScaffold.from_payload(dict(raw) if isinstance(raw, Mapping) else raw)


def resolve_approved_gawd_target_project_id(
    spec: DirectiveSpec,
    gawd_doc: Mapping[str, Any],
    *,
    inferred_target_project_id: str | None = None,
) -> str:
    scaffold = build_target_project_scaffold_from_gawd_doc(gawd_doc)
    embedded_target = extract_target_project_id_from_gawd_doc(gawd_doc)
    if scaffold is not None and embedded_target is not None:
        raise ValueError("Approved GAWD target cannot be both linked and scaffolded.")
    if spec.target_project_id and scaffold and spec.target_project_id != scaffold.project_id:
        raise ValueError(
            "Explicit target project does not match the approved scaffold contract: "
            f"{spec.target_project_id!r} != {scaffold.project_id!r}."
        )
    if spec.create_target_id and embedded_target:
        raise ValueError(
            "Approved GAWD already names a linked target; --create-target cannot replace it."
        )
    if spec.create_target_id and scaffold and spec.create_target_id != scaffold.project_id:
        raise ValueError(
            "Explicit scaffold target does not match the approved scaffold contract: "
            f"{spec.create_target_id!r} != {scaffold.project_id!r}."
        )
    target_project_id = (
        spec.target_project_id
        or spec.create_target_id
        or embedded_target
        or (scaffold.project_id if scaffold else None)
        or inferred_target_project_id
    )
    if not target_project_id:
        raise ValueError(
            "Approved GAWD execution requires an explicit target project. "
            "Use /start /approved-gawd <final_gawd_doc_id> --target-project <project_id> "
            "or --create-target <project_id>."
        )
    return target_project_id


def resolve_target_project_from_gawd_dispatch_history(
    settings: Any,
    gawd_doc_id: str,
) -> dict[str, Any] | None:
    """Infer a legacy GAWD target only when its durable history is unanimous."""

    intents = run_coordination_command(
        ListDispatchIntents(),
        timeout=15,
        settings=settings,
    ).get("intents", [])
    source_prefix = f"approved_gawd:{gawd_doc_id}"
    matches = [
        dict(intent)
        for intent in intents
        if isinstance(intent, Mapping)
        and (
            intent.get("source") == source_prefix
            or str(intent.get("source") or "").startswith(f"{source_prefix}:")
        )
        and str(intent.get("target_project_id") or "").strip()
    ]
    targets = sorted({str(intent["target_project_id"]).strip() for intent in matches})
    if not targets:
        return None
    if len(targets) > 1:
        raise ValueError(
            "Approved GAWD dispatch history names multiple target projects; "
            "the shortcut will not guess. Use the explicit /start /approved-gawd "
            f"form. gawd_doc_id={gawd_doc_id!r}, targets={targets!r}."
        )
    return {
        "target_project_id": targets[0],
        "source": "prior_gawd_dispatch_intents",
        "intent_ids": sorted(
            str(intent.get("intent_id")) for intent in matches if intent.get("intent_id")
        ),
    }


def validate_approved_gawd_target_project(settings: Any, target_project_id: str) -> LinkedProject:
    center = load_project_center(settings)
    target_project = center.project_by_id(target_project_id)
    if target_project.read_only:
        raise ValueError(f"Approved GAWD target project is read-only: {target_project_id}")
    return target_project


def build_approved_gawd_dispatch_prompt(
    gawd_doc: Mapping[str, Any],
    milestone: Mapping[str, Any] | None = None,
) -> str:
    lines = [
        (
            "Implement the next approved saga milestone."
            if milestone is not None
            else "Implement the approved GAWD contract."
        ),
        "",
        f"gawd_doc_id: {gawd_doc.get('gawd_doc_id')}",
        f"saga_id: {gawd_doc.get('saga_id')}",
        f"goal: {gawd_doc.get('goal')}",
        "",
        "Constraints:",
        *_markdown_items(_string_items(gawd_doc.get("constraints"))),
        "",
        "Success criteria:",
        *_markdown_items(_string_items(gawd_doc.get("success_criteria"))),
        "",
        "Acceptance criteria:",
        *_markdown_items(_string_items(gawd_doc.get("acceptance_criteria"))),
        "",
    ]
    if milestone is not None:
        lines.extend(
            [
                "Milestone:",
                f"- milestone_id: {milestone.get('milestone_id')}",
                f"- sequence: {milestone.get('sequence')}",
                f"- name: {milestone.get('name')}",
                f"- description: {milestone.get('description')}",
                "",
                "Milestone entry criteria:",
                *_markdown_items(_string_items(milestone.get("entry_criteria"))),
                "",
                "Milestone exit criteria:",
                *_markdown_items(_string_items(milestone.get("exit_criteria"))),
                "",
                "Milestone required artifacts/evidence:",
                *_markdown_items(_string_items(milestone.get("required_artifacts"))),
                "",
            ]
        )
    lines.extend(
        [
            "Unresolved questions:",
            *_markdown_items(_string_items(gawd_doc.get("unresolved_questions")) or ("None.",)),
            "",
            "Durable plan reference:",
            "- The complete task graph and finalized GAWD remain attached to the ledger under "
            f"gawd_doc_id {gawd_doc.get('gawd_doc_id')}. This dispatch brief deliberately "
            "contains only the approved milestone contract; do not widen scope to downstream "
            "milestones.",
            "",
            "Execution rules:",
            "- Stay inside the approved goal, constraints, and non-goals.",
            "- Preserve the permission envelope; do not merge, deploy, spend, send external "
            "messages, access secrets, or perform destructive operations without a separate "
            "approval.",
            "- Record verification evidence in the ledger before marking work complete.",
        ]
    )
    return "\n".join(lines)


def _string_items(value: Any) -> tuple[str, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if value is None:
        return ()
    text = str(value).strip()
    return (text,) if text else ()


def _markdown_items(items: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"- {item}" for item in items) or ("- None.",)


def persist_durable_workflow_milestones(
    settings: Any,
    *,
    saga_id: str,
    gawd_doc_id: str,
    durable_workflow_plan: Any,
) -> list[dict[str, Any]]:
    """Promote GAWD workflow-plan milestones into durable ledger rows."""

    steps_by_milestone = {
        step.milestone_id: step for step in getattr(durable_workflow_plan, "steps", ())
    }
    created: list[dict[str, Any]] = []
    previous_milestone_id: str | None = None
    for sequence, milestone in enumerate(
        getattr(durable_workflow_plan, "milestones", ()),
        start=1,
    ):
        step = steps_by_milestone.get(milestone.milestone_id)
        ledger_milestone_id = f"{saga_id}:{milestone.milestone_id}"
        description_parts = [milestone.happy_path_step]
        if step is not None:
            description_parts.append(step.durable_boundary_reason)
        depends_on = (previous_milestone_id,) if previous_milestone_id else ()
        entry_criteria: tuple[str, ...] = ()
        exit_criteria: tuple[str, ...]
        required_artifacts: tuple[str, ...] = ()
        approval_required = False
        if step is not None:
            entry_criteria = tuple(step.inputs)
            exit_criteria = tuple(step.outputs)
            required_artifacts = tuple(step.evidence_to_record)
            approval_required = step.approval_required
        else:
            exit_criteria = (milestone.happy_path_step,)
        created.append(
            run_coordination_command(
                CreateSagaMilestone(
                    saga_id=saga_id,
                    name_text=milestone.name,
                    sequence=sequence,
                    milestone_id=ledger_milestone_id,
                    gawd_doc_id=gawd_doc_id,
                    description="\n\n".join(part for part in description_parts if part),
                    depends_on=depends_on,
                    entry_criteria=entry_criteria,
                    exit_criteria=exit_criteria,
                    required_artifacts=required_artifacts,
                    approval_required=approval_required,
                ),
                timeout=15,
                settings=settings,
            )["milestone"]
        )
        previous_milestone_id = ledger_milestone_id
    return created


def ensure_approved_gawd_milestones(
    settings: Any,
    *,
    saga_id: str,
    gawd_doc: Mapping[str, Any],
) -> list[dict[str, Any]]:
    existing = run_coordination_command(
        ListSagaMilestones(saga_id),
        timeout=15,
        settings=settings,
    )["milestones"]
    if existing:
        return existing
    gawd_doc_id = str(gawd_doc.get("gawd_doc_id") or "")
    created = run_coordination_command(
        CreateSagaMilestone(
            saga_id=saga_id,
            name_text="Implement approved GAWD contract",
            sequence=1,
            milestone_id=f"{saga_id}:m01_implement_approved_gawd_contract",
            gawd_doc_id=gawd_doc_id,
            description=(
                "Legacy approved GAWD without a persisted workflow plan. Execute as one "
                "explicit milestone rather than a whole-saga dispatch."
            ),
            entry_criteria=("GAWD doc is approved and attached to this saga.",),
            exit_criteria=("Approved GAWD success and acceptance criteria are satisfied.",),
            required_artifacts=("test_log",),
        ),
        timeout=15,
        settings=settings,
    )["milestone"]
    return [created]


def approve_next_dependency_ready_milestone(
    settings: Any,
    *,
    saga_id: str,
    gawd_doc_id: str,
    target_project_id: str,
    blocked: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Apply this explicit operator command to one dependency-ready gate.

    `/approved-gawd` is intentionally repeatable: the first invocation approves
    milestone 1, and a later invocation after completion approves milestone 2.
    It never grants approval to downstream milestones in advance.
    """
    candidate = next(
        (
            item
            for item in blocked
            if item.get("dependency_ready") and not item.get("approval_ready")
        ),
        None,
    )
    if candidate is None:
        return None
    milestone_id = str(candidate.get("milestone_id") or "").strip()
    if not milestone_id:
        return None
    pending = run_coordination_command(
        ListApprovalRequests(saga_id=saga_id, status="PENDING"),
        timeout=15,
        settings=settings,
    ).get("requests", [])
    request = next(
        (
            item
            for item in pending
            if item.get("request_type") == "GENERAL"
            and isinstance(item.get("payload"), Mapping)
            and item["payload"].get("milestone_id") == milestone_id
        ),
        None,
    )
    if request is None:
        request = run_coordination_command(
            SubmitApprovalRequest(
                saga_id=saga_id,
                request_type="GENERAL",
                requested_by="pi:/start /approved-gawd",
                payload={
                    "schema_version": "milestone_execution_approval.v1",
                    "milestone_id": milestone_id,
                    "gawd_doc_id": gawd_doc_id,
                    "target_project_id": target_project_id,
                },
            ),
            timeout=15,
            settings=settings,
        )
    resolution = run_coordination_command(
        ResolveApprovalRequest(
            approval_id=str(request["approval_id"]),
            decision=ApprovalDecision.APPROVE,
            resolved_by="operator:/start /approved-gawd",
        ),
        timeout=15,
        settings=settings,
    )
    return {
        "milestone_id": milestone_id,
        "request": request,
        "resolution": resolution,
    }
