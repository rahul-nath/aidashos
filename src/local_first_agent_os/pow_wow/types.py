# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Core data model shared by pow-wow schedulers and executor backends."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol

from ..constants import AGENT_BRANCH_AUTO_MERGE
from ..coordination.contracts import CoordinationCommand, CoordinationResult
from ..project_center import LinkedProject
from ..staffing import IMPLEMENTER, REVIEWER, JudgmentRole
from .protocol import (
    PlanningPhase,
    ReferencePack,
    ReviewOrigin,
    TaskPurpose,
    infer_legacy_task_purpose,
)

type DelegateFn = Callable[..., Mapping[str, Any]]
type CoordinationCommandFn = Callable[[CoordinationCommand], CoordinationResult]
type DispatchKind = Literal["advisory", "code", "cast"]
type ExecutionLeaseStatus = Literal[
    "COMPLETED",
    "FAILED",
    "TIMED_OUT",
    "CANCELED",
    "COMPENSATED",
]
type PowWowTaskStatus = Literal["planned", "completed", "blocked", "failed"]
type PowWowRunStatus = Literal[
    "DRY_RUN_COMPLETED",
    "COMPLETED",
    "VERIFICATION_FAILED",
    "BLOCKED",
    "FAILED",
]


@dataclass(frozen=True)
class CommandRunCapture:
    command: str
    cwd: str
    stdout: str
    stderr: str
    exit_code: int

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionAttemptLease:
    idempotency_key: str
    worker_id: str
    task_id: str | None = None
    lease_id: str | None = None
    created: bool = False
    open_status: str | None = None
    open_error: str | None = None
    complete_status: str | None = None
    complete_error: str | None = None
    reused_terminal: bool = False
    blocked_existing_active: bool = False
    result: Mapping[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key,
            "worker_id": self.worker_id,
            "task_id": self.task_id,
            "lease_id": self.lease_id,
            "created": self.created,
            "open_status": self.open_status,
            "open_error": self.open_error,
            "complete_status": self.complete_status,
            "complete_error": self.complete_error,
            "reused_terminal": self.reused_terminal,
            "blocked_existing_active": self.blocked_existing_active,
        }


@dataclass(frozen=True)
class PowWowTaskSpec:
    task_name: str
    role: str
    description: str
    success_criteria: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    """What this task's spawned process may do, as `Capability` values.

    Replaces a dead `allowed_tools: tuple[str, ...]` that no constructor in the
    repository ever filled. Kept as strings for the same reason `ToolPolicy`
    keeps them as strings: this is serialized into artifacts and compared by
    bytes, so the wire form is the stored form. `SpawnAuthority.from_names`
    parses them, and refuses a name no capability answers to.

    Empty means nothing declared anything, which is read as the narrowest
    authority rather than the widest.
    """

    purpose: TaskPurpose | None = None
    judgment: JudgmentRole | None = None
    dispatch_kind: DispatchKind | None = None
    blocked_by: tuple[str, ...] = ()
    worktree_group: str | None = None
    planning_phase: PlanningPhase | None = None
    reference_packs: tuple[ReferencePack, ...] = ()

    def __post_init__(self) -> None:
        purpose = self.purpose
        if isinstance(purpose, str):
            purpose = TaskPurpose(purpose)
        if purpose is None:
            purpose = infer_legacy_task_purpose(
                task_name=self.task_name,
                role=self.role,
                judgment_name=self.judgment.name if self.judgment else None,
                dispatch_kind=self.dispatch_kind,
            )
        object.__setattr__(self, "purpose", purpose)
        planning_phase = self.planning_phase
        if isinstance(planning_phase, str):
            object.__setattr__(self, "planning_phase", PlanningPhase(planning_phase))
        object.__setattr__(
            self,
            "reference_packs",
            tuple(ReferencePack(value) for value in self.reference_packs),
        )

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["purpose"] = self.purpose.value if self.purpose else None
        payload["planning_phase"] = self.planning_phase.value if self.planning_phase else None
        payload["reference_packs"] = [pack.value for pack in self.reference_packs]
        payload["judgment"] = self.judgment.to_payload() if self.judgment else None
        return payload


@dataclass(frozen=True)
class PowWowExecutionContext:
    saga_id: str
    goal: str
    directive: str
    target_project_id: str
    target_project_path: str
    target_project_kind: str
    target_project_status: str
    target_project_read_only: bool
    dispatch_intent_id: str | None = None
    verification_commands: tuple[str, ...] = ()
    evidence_project_ids: tuple[str, ...] = ()
    memory_project_id: str | None = None
    personal_context_used: bool = False
    no_auto_merge: bool = not AGENT_BRANCH_AUTO_MERGE
    dispatch_kind: DispatchKind = "code"
    execution_checkpoint_id: str | None = None
    checkpoint_worktree_path: str | None = None
    checkpoint_base_head_sha: str | None = None
    checkpoint_patch_artifact_id: str | None = None
    reuse_checkpoint_worktree: bool = False
    review_origin: ReviewOrigin = ReviewOrigin.AUTOMATED_STAFF
    reviewed_commit_sha: str | None = None
    review_base_sha: str | None = None
    recovery_retained_branch: str | None = None
    recovery_original_task_contract: str | None = None
    recovery_permission_envelope: str | None = None
    task_ids_by_name: Mapping[str, str] | None = None

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PowWowArtifact:
    artifact_type: str
    content: dict[str, Any]
    task_name: str | None = None
    schema_version: str = "pow_wow_artifact.v1"
    persisted_artifact_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.persisted_artifact_id is None:
            payload.pop("persisted_artifact_id")
        return payload


@dataclass(frozen=True)
class PowWowTaskResult:
    task_name: str
    role: str
    status: PowWowTaskStatus
    summary: str
    changed_files: tuple[str, ...] = ()
    verification_commands: tuple[str, ...] = ()
    verification_output: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    artifacts: tuple[PowWowArtifact, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["artifacts"] = [artifact.to_payload() for artifact in self.artifacts]
        return payload


@dataclass(frozen=True)
class PowWowRunResult:
    executor: str
    mode: str
    pow_wow_id: str
    target_project_id: str
    target_project_path: str
    status: PowWowRunStatus
    output_summary: str
    tasks: tuple[PowWowTaskResult, ...] = ()
    changed_files: tuple[str, ...] = ()
    verification_commands: tuple[str, ...] = ()
    verification_output: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    artifacts: tuple[PowWowArtifact, ...] = ()
    external_agents_started: bool = False
    auto_merge: bool = AGENT_BRANCH_AUTO_MERGE

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["tasks"] = [task.to_payload() for task in self.tasks]
        payload["artifacts"] = [artifact.to_payload() for artifact in self.artifacts]
        return payload


class PowWowExecutor(Protocol):
    def dispatch_pow_wow(
        self,
        pow_wow_id: str,
        target_project: LinkedProject,
        tasks: Sequence[PowWowTaskSpec],
        context: PowWowExecutionContext,
    ) -> PowWowRunResult: ...


def build_default_saga_tasks(
    goal: str,
    target_project: LinkedProject,
) -> tuple[PowWowTaskSpec, ...]:
    target = target_project.id
    return (
        PowWowTaskSpec(
            task_name="implement_next_gated_portfolio_task",
            role=IMPLEMENTER.name,
            purpose=TaskPurpose.IMPLEMENTATION,
            judgment=IMPLEMENTER,
            dispatch_kind="code",
            worktree_group="default",
            description=(
                f"Use {target} reports and runbooks to identify the next gated portfolio "
                f"implementation task for this goal, then prepare the implementation work: "
                f"{goal}"
            ),
            success_criteria=(
                "Target project and gate context are explicit.",
                "Expected file changes are identified before edit execution.",
                "No merge or deploy action is taken automatically.",
            ),
        ),
        PowWowTaskSpec(
            task_name="review_and_verify_next_gated_portfolio_task",
            role=REVIEWER.name,
            purpose=TaskPurpose.REVIEW,
            judgment=REVIEWER,
            dispatch_kind="code",
            blocked_by=("implement_next_gated_portfolio_task",),
            worktree_group="default",
            description=(
                f"Review the implementation task for {target}, identify verification "
                "commands, and record risks or approval needs before any merge."
            ),
            success_criteria=(
                "Verification commands are recorded.",
                "Risks and approval edges are captured.",
                "The review remains independent of the implementation agent.",
            ),
        ),
    )


__all__ = [
    "CommandRunCapture",
    "CoordinationCommandFn",
    "DelegateFn",
    "DispatchKind",
    "ExecutionAttemptLease",
    "ExecutionLeaseStatus",
    "PowWowArtifact",
    "PowWowExecutionContext",
    "PowWowExecutor",
    "PowWowRunResult",
    "PowWowRunStatus",
    "PowWowTaskResult",
    "PowWowTaskSpec",
    "PowWowTaskStatus",
    "build_default_saga_tasks",
]
