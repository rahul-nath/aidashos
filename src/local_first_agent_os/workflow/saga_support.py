# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Active saga execution, document intake and milestone persistence support."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ..coordination import CreateSagaMilestone
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
from ..staffing import load_bench


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


def validate_approved_gawd_target_project(settings: Any, target_project_id: str) -> LinkedProject:
    center = load_project_center(settings)
    target_project = center.project_by_id(target_project_id)
    if target_project.read_only:
        raise ValueError(f"Approved GAWD target project is read-only: {target_project_id}")
    return target_project


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
