# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar, Final, assert_never, cast

from ..agent_execution_supervisor import (
    ArtifactWriter,
    StreamingCommandSupervisor,
    SupervisedCommandResult,
)
from ..agent_ledger_mcp import claude_mcp_args, codex_mcp_args
from ..browser_acceptance import (
    BrowserAcceptanceArtifactWriter,
    BrowserAcceptanceRequest,
    BrowserAcceptanceRunner,
    BrowserAcceptanceStatus,
    LocalPreviewSession,
    PreviewStartError,
)
from ..capabilities import Capability, gated_capabilities
from ..capability_gate import AgentCaller, CapabilityDenied, check_capability
from ..constants import (
    AGENT_BRANCH_AUTO_MERGE,
    AGENT_WORKTREE_BRANCH_PREFIX,
    CLI_AGENT_RUN_ARTIFACT_TYPE,
    DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
    DEFAULT_ARTIFACT_WRITE_TIMEOUT_SECONDS,
    DEFAULT_COORDINATION_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS,
    DEFAULT_PROGRESS_ASSESSMENT_TIMEOUT_SECONDS,
    DEFAULT_STREAM_DRAIN_TIMEOUT_SECONDS,
    DEFAULT_VERIFICATION_COMMAND_TIMEOUT_SECONDS,
    DELEGATED_TASK_RUN_ARTIFACT_TYPE,
    DISPATCH_RETRY_POLICY,
    FRONTIER_FALLBACK_RUN_ARTIFACT_TYPE,
)
from ..contracts import ApprovalRequestType
from ..coordination.contracts import (
    AcknowledgementResult,
    AppendExecutionEvent,
    ClaimTask,
    CollectionResult,
    CompleteExecutionLease,
    CreateExecutionCheckpoint,
    EntityResult,
    ExecutionLeaseTerminalStatus,
    FindAgentContinuation,
    LatestRepoAudit,
    OpenExecutionLease,
    SubmitApprovalRequest,
    SubmitArtifact,
)
from ..coordination.outcomes import (
    AgentStatus,
    ExecutionTransition,
    InfrastructureFailure,
    PersistenceStatus,
    SupervisorStatus,
    classify_failure,
    failure_category,
)
from ..engineering_doctrine import CURRENT_ENGINEERING_DOCTRINE
from ..lifecycle_failure_harness import (
    LifecycleTransitionPoint,
    reach_lifecycle_transition,
)
from ..marketing_site_doctrine import CURRENT_MARKETING_SITE_DOCTRINE
from ..observability import profiled_step
from ..progress_events import emit_progress
from ..project_center import LinkedProject
from ..spawn_authority import (
    ReadOnlyInspection,
    SpawnAuthority,
    SpawnPosture,
    UnattendedImplementation,
    authority_for_purpose,
    describe_posture,
)
from ..staffing import (
    DEFAULT_BENCH,
    Bench,
    BenchSlot,
    FrontierHarness,
    Harness,
    HarnessKind,
    JudgmentWorkload,
    LocalHarness,
    Tier,
    classify_harness,
    resolve_bench,
    resolve_bench_for_workload,
)
from ..toolchains import project_environment
from .dry_run import DryRunPowWowExecutor
from .git_ops import (
    WorktreeAllocation,
    WorktreeCleanupPolicy,
    WorktreeCommitCheckpoint,
    build_worktree_code_patch,
    commit_worktree_checkpoint,
    list_changed_worktree_files,
    run_git_command_for_output,
    summarize_worktree_diff,
)
from .planning import (
    PlanningContractError,
    audit_consumer_tier,
    persist_planning_evidence,
    persist_repo_audit,
    validate_planning_visibility_contract,
)
from .process import (
    FrontierFallbackReason,
    build_command_capture_from_lease_result,
    build_command_capture_lease_payload,
    build_execution_attempt_idempotency_key,
    classify_execution_lease_status,
    describe_execution_lease_error,
    extract_agent_cli_output,
    infer_frontier_fallback_reason,
    run_captured_command,
    run_captured_shell_command,
    warrants_provider_swap,
)
from .prompts import (
    build_agent_task_prompt,
    build_assigned_worktree_context,
    build_assigned_worktree_environment,
    build_resumed_senior_implementation_prompt,
)
from .protocol import (
    PlanningPhase,
    ReferencePack,
    ReviewCompletionStatus,
    ReviewDisposition,
    ReviewerTier,
    ReviewOrigin,
    ReviewVerdict,
    TaskPurpose,
    classify_finding_severity,
)
from .repo_audit import RepoAudit, RepoAuditError, render_audit_context_block
from .results import derive_pow_wow_run_status
from .review import (
    extract_review_verdict_text,
    is_agent_task,
    is_implementation_task,
    is_review_task,
    review_verdict_disposition,
)
from .revision import build_bounded_revision_context_from_review
from .types import (
    CommandRunCapture,
    CoordinationCommandFn,
    DelegateFn,
    DispatchKind,
    ExecutionAttemptLease,
    ExecutionLeaseStatus,
    PowWowArtifact,
    PowWowExecutionContext,
    PowWowExecutor,
    PowWowRunResult,
    PowWowRunStatus,
    PowWowTaskResult,
    PowWowTaskSpec,
    PowWowTaskStatus,
    build_default_saga_tasks,
)
from .verification import (
    VerificationNotDeclared,
    VerificationOutcome,
    checkpoint_permitted,
    classify_verification,
    uncertifiable_reason,
)
from .views import ViewCompactor

__all__ = [
    "CONTROL_PLANE_ENV_NAMES",
    "CONTROL_PLANE_ENV_PREFIXES",
    "CliPowWowExecutor",
    "CommandRunCapture",
    "CoordinationCommandFn",
    "DelegateFn",
    "DispatchKind",
    "DryRunPowWowExecutor",
    "ExecutionAttemptLease",
    "ExecutionLeaseStatus",
    "FakeProcessPowWowExecutor",
    "PowWowArtifact",
    "PowWowExecutionContext",
    "PowWowExecutor",
    "PowWowRunResult",
    "PowWowRunStatus",
    "PowWowTaskResult",
    "PowWowTaskSpec",
    "PowWowTaskStatus",
    "WorktreeAllocation",
    "WorktreeCleanupPolicy",
    "WorktreeCommitCheckpoint",
    "build_agent_task_prompt",
    "list_changed_worktree_files",
    "build_worktree_code_patch",
    "build_default_saga_tasks",
    "verification_gate_environment",
]


_LEASE_ACTIVE_STATUSES = {"ACTIVE", "CANCEL_REQUESTED"}
LEASE_TERMINAL_STATUSES = {
    "COMPLETED",
    "FAILED",
    "TIMED_OUT",
    "CANCELED",
    "COMPENSATED",
}

# Every variable this control plane sets to configure itself lives under one of
# these prefixes; tests/test_verification_gate_environment.py is what keeps that
# true as settings are added.
CONTROL_PLANE_ENV_PREFIXES: Final = ("LOCAL_AGENT_", "AGENT_COORDINATION_", "DBOS_")
# `VIRTUAL_ENV` leaks from whatever venv the dispatcher runs in. `uv` ignores it
# with a warning, but a target project invoking bare `pytest` or `python -m`
# would silently execute against the control plane's interpreter.
# `AGENT_SESSION_ID` is the coordination CLI's per-child handshake, set at spawn
# by agent_adapters: un-prefixed because external harness configs name it, but
# control-plane state all the same, so the gate strips it too.
CONTROL_PLANE_ENV_NAMES: Final = frozenset({"VIRTUAL_ENV", "AGENT_SESSION_ID"})

# A frontier harness is a child process, not an operator terminal.  Codex and
# Claude may start login shells while using their command tools; those shells
# source ``pi_terminal_hook.zsh`` in this repository.  Without this inherited
# sentinel, each shell registers itself as an interactive Pi session and its
# exit trap can stop the resident runtime underneath the dispatcher that owns
# it.  Keep the assumption at the process boundary rather than teaching the
# terminal hook about every possible headless parent.
_HEADLESS_AGENT_ENV: Final = {"LOCAL_AGENT_TERMINAL_SESSION_STARTED": "1"}


def _headless_agent_environment(
    environment: Mapping[str, str] | None,
) -> dict[str, str]:
    return {**(environment or {}), **_HEADLESS_AGENT_ENV}


def verification_gate_environment(cwd: Path) -> tuple[dict[str, str], tuple[str, ...]]:
    """The environment a declared verification command runs in, and what was cut.

    A verification command is a statement about the target project, so it runs in
    the target project's environment and not the control plane's. The gate gets
    what a developer's shell would give it - `HOME`, `PATH`, toolchain variables,
    registry credentials - and nothing that exists only because a dispatcher is
    running. A denylist rather than an allowlist, deliberately: verification
    commands are somebody else's toolchain, and a gate that scrubbed everything
    unlisted would fail honest suites for opaque reasons.

    The supervised dispatcher exports `LOCAL_AGENT_USE_DBOS=true` because it is
    the process that hands work to DBOS. Inherited by a gate's `pytest`, that one
    variable made this repository's own suite fail 72 tests against a clean diff,
    and the evidence blamed the diff. See
    docs/completed/verification_gate_environment_design.md.

    Returns the environment beside the sorted names it stripped, because a
    stripped variable is invisible unless the run record says so. Names only,
    never values: the values include database URLs.
    """

    environment = project_environment(cwd)
    stripped = tuple(
        sorted(
            name
            for name in environment
            if name.startswith(CONTROL_PLANE_ENV_PREFIXES) or name in CONTROL_PLANE_ENV_NAMES
        )
    )
    for name in stripped:
        del environment[name]
    return environment, stripped


def _commit_worktree_checkpoint_at_lifecycle_boundary(
    worktree: WorktreeAllocation,
    *,
    task_name: str,
) -> WorktreeCommitCheckpoint:
    checkpoint = commit_worktree_checkpoint(worktree, task_name=task_name)
    if checkpoint.commit_sha and not checkpoint.error:
        reach_lifecycle_transition(
            LifecycleTransitionPoint.AFTER_CHECKPOINT_GIT_COMMIT,
            task_name=task_name,
            source_repo_path=worktree.source_repo_path,
            worktree_path=worktree.worktree_path,
            branch_name=worktree.branch_name,
            base_head_sha=checkpoint.base_head_sha,
            commit_sha=checkpoint.commit_sha,
        )
    return checkpoint


def _build_engineering_doctrine_provenance(task: PowWowTaskSpec) -> dict[str, str] | None:
    if task.judgment is None or task.judgment.tier not in {Tier.SENIOR, Tier.STAFF}:
        return None
    return CURRENT_ENGINEERING_DOCTRINE.provenance_payload()


def _build_marketing_site_doctrine_provenance(
    task: PowWowTaskSpec,
) -> dict[str, str] | None:
    if (
        task.judgment is None
        or task.judgment.tier not in {Tier.SENIOR, Tier.STAFF}
        or ReferencePack.MARKETING_SITE not in task.reference_packs
    ):
        return None
    return CURRENT_MARKETING_SITE_DOCTRINE.provenance_payload()


@dataclass(frozen=True)
class _CodeWorktreeLease:
    group: str
    allocation: WorktreeAllocation


@dataclass(frozen=True)
class ResumeExisting:
    thread_id: str
    source_task_name: str
    source_task_id: str
    authority_transition: ReadOnlyToImplementation
    model_transition: ReaderToImplementationModelTransition


@dataclass(frozen=True)
class ReadOnlyToImplementation:
    """The sole permitted authority widening for an existing Codex thread."""

    source_permission_envelope_sha256: str
    target_permission_envelope_sha256: str


@dataclass(frozen=True)
class ReaderToImplementationModelTransition:
    """The staffing-declared model change across the resumed planning boundary."""

    source_model: str | None
    target_model: str | None


@dataclass(frozen=True)
class StartFreshBounded:
    reason: str


@dataclass(frozen=True)
class StartFreshIndependent:
    reason: str


type FrontierLaunchDecision = ResumeExisting | StartFreshBounded | StartFreshIndependent


def _launch_decision_payload(decision: FrontierLaunchDecision) -> dict[str, object]:
    match decision:
        case ResumeExisting(
            thread_id=thread_id,
            source_task_name=source_task_name,
            source_task_id=source_task_id,
            authority_transition=authority_transition,
            model_transition=model_transition,
        ):
            return {
                "kind": "resume_existing",
                "thread_id": thread_id,
                "source_task_name": source_task_name,
                "source_task_id": source_task_id,
                "authority_transition": {
                    "kind": "read_only_to_implementation",
                    "source_permission_envelope_sha256": (
                        authority_transition.source_permission_envelope_sha256
                    ),
                    "target_permission_envelope_sha256": (
                        authority_transition.target_permission_envelope_sha256
                    ),
                },
                "model_transition": {
                    "kind": "reader_to_implementation",
                    "source_model": model_transition.source_model,
                    "target_model": model_transition.target_model,
                },
            }
        case StartFreshBounded(reason=reason):
            return {"kind": "start_fresh_bounded", "reason": reason}
        case StartFreshIndependent(reason=reason):
            return {"kind": "start_fresh_independent", "reason": reason}
        case _ as unreachable:
            assert_never(unreachable)


def _slugify_path_component(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-")
    return slug[:80] or "task"


_FAILURE_EXCERPT_LIMIT: Final = 400


def _harness_failure_excerpt(
    capture: CommandRunCapture,
    extracted_output: str | None = None,
) -> str | None:
    """What a nonzero harness run actually said, in one bounded line.

    An exit code cannot tell an unauthenticated CLI apart from a refused task,
    and both readings were already in hand wherever the code recorded only the
    integer: the capture carries the streams that explain it. Recording the
    number alone hands an operator a bare `exit=1` for what is often a
    one-command fix, and sends them to the loop logs to find out which.

    `extracted_output` wins when a caller has already parsed the harness's own
    stream format, because raw stdout from a JSON-streaming CLI is transport
    noise around the sentence that matters. `stderr` comes next, since a harness
    that fails before it starts writes there, and raw stdout is the last resort.

    Bounded, because this lands in a durable ledger row that an operator reads
    rather than greps, and unbounded agent output would bury the summary it is
    attached to.
    """

    if capture.exit_code == 0:
        return None
    for stream in (extracted_output, capture.stderr, capture.stdout):
        text = " ".join((stream or "").split())
        if not text:
            continue
        if len(text) > _FAILURE_EXCERPT_LIMIT:
            text = f"{text[:_FAILURE_EXCERPT_LIMIT]}..."
        return text
    return None


# The harness flags each posture earns, one table per CLI so the three rows can
# be read against each other. Keyed by `describe_posture`, which is the same
# string the run artifact records, so what a reader sees in the ledger and what
# the process was given cannot drift.
#
# Both middle values are real flags on the installed CLIs, checked rather than
# assumed: `codex exec -s` accepts read-only|workspace-write|danger-full-access,
# and `claude --disallowedTools` takes a comma-separated list.
# The ceiling an executor gets when nothing supplied one.
#
# Deliberately everything, and deliberately named. An executor built directly -
# by a saga directive, by a test - has no dispatch intent behind it and therefore
# no compiled declaration to read, and narrowing those to nothing would refuse
# work that used to run. The narrowing that matters happens per task, from its
# purpose; this only decides what the *intersection* cannot exceed when nobody
# has stated a bound. A dispatcher-built executor always states one.
_UNBOUNDED_CEILING: Final = SpawnAuthority.of(Capability)


_CODEX_SANDBOX_ARGS: Mapping[str, list[str]] = {
    "read_only_inspection": ["-s", "read-only"],
    "supervised_commands": ["-s", "workspace-write"],
    "unattended_implementation": ["--dangerously-bypass-approvals-and-sandbox"],
}

# The tools each posture withholds, named by what they are rather than spelled
# per row. The two lists below used to repeat "Edit,Write,NotebookEdit", so
# "which tools mutate a file" was one fact written twice: a Claude release
# adding a fourth editor tool needed both rows edited and nothing failed if only
# one was.
_CLAUDE_EDITOR_TOOLS: Final = ("Edit", "Write", "NotebookEdit")
_CLAUDE_SHELL_TOOLS: Final = ("Bash",)

# `Task` is deliberately absent from every row, which is a decision rather than
# an omission: a spawned agent may fan out into its own in-process subagents.
# They inherit this process's `--disallowedTools`, so a read-only agent's
# subagents are read-only too, and the whole fan-out reaches the world through
# one leased worktree whose diff faces the same verification and review gates a
# solo agent's would. Policing it here would also be harness-specific theatre:
# `codex exec` has no equivalent facility, and this repository keeps its
# boundaries where every harness meets them, which is git output.
# `engineering_doctrine.v2` states the same boundary to the agent in prose.


def _claude_disallowed(*tools: tuple[str, ...]) -> list[str]:
    return ["--disallowedTools", ",".join(name for group in tools for name in group)]


_CLAUDE_PERMISSION_ARGS: Mapping[str, list[str]] = {
    # No bypass, and the mutating tools named as forbidden. Previously this was
    # "no flag at all", which relies on claude declining to write in --print mode
    # rather than on having been told not to.
    "read_only_inspection": _claude_disallowed(_CLAUDE_EDITOR_TOOLS, _CLAUDE_SHELL_TOOLS),
    # A shell, but not an editor.
    "supervised_commands": _claude_disallowed(_CLAUDE_EDITOR_TOOLS),
    "unattended_implementation": ["--dangerously-skip-permissions"],
}


class _WorktreePowWowExecutorBase:
    mode: ClassVar[str]
    """How this executor runs tasks, named by each concrete subclass.

    Declared here rather than only on the subclasses because the shared result
    builders on this class record it. Annotated without a value so a subclass
    that forgets to name itself is a type error rather than an `AttributeError`
    raised while building the artifact for a failure.
    """

    def __init__(
        self,
        *,
        worktree_root: Path,
        cleanup_policy: WorktreeCleanupPolicy = "remove",
        timeout_seconds: int = 30,
        verification_timeout_seconds: int = DEFAULT_VERIFICATION_COMMAND_TIMEOUT_SECONDS,
        verification_commands: Sequence[str] | None = None,
        bench: Bench | None = None,
        delegate_fn: DelegateFn | None = None,
        dependency_compactor: ViewCompactor | None = None,
        spawn_ceiling: SpawnAuthority | None = None,
        agent_ledger_root: Path | None = None,
    ) -> None:
        self.worktree_root = worktree_root
        self.cleanup_policy: WorktreeCleanupPolicy = cleanup_policy
        self.timeout_seconds = timeout_seconds
        # Its own clock, not the agent's. `timeout_seconds` is how long a model
        # may think; this is how long a deterministic check may take, and a
        # check sharing the agent's budget can consume all of it without ever
        # reporting red.
        self.verification_timeout_seconds = verification_timeout_seconds
        self.verification_commands = tuple(verification_commands or ())
        self.bench = bench if bench is not None else DEFAULT_BENCH
        self.delegate_fn = delegate_fn
        # Unset means the dependency block truncates on overflow, which is what
        # every executor did before this existed. An executor built without a
        # runtime - by a test, by a dry run - has no model to reach, and prompt
        # construction is not the place to discover that.
        self.dependency_compactor = dependency_compactor
        self.spawn_ceiling = spawn_ceiling if spawn_ceiling is not None else _UNBOUNDED_CEILING
        # Which ledger a dispatched agent may read, or None to offer it nothing.
        # A root rather than a boolean because the offer is meaningless without
        # one: a server told to find its own would answer from whichever database
        # the ambient environment named, which is a wrong answer no reader could
        # distinguish from a right one. An executor built without a runtime has no
        # root to give and therefore makes no offer, which is what every executor
        # did before this existed.
        self.agent_ledger_root = agent_ledger_root
        self._worktree_lock = threading.Lock()

    def _task_bench_slot(self, task: PowWowTaskSpec) -> BenchSlot | None:
        """The bench slot a task's judgment tier resolves to, if it has one.

        Missing rather than raising on an unstaffed tier, deliberately: this is
        asked by scheduling predicates about every task, and an unstaffed tier is
        a question for the path that actually needs the slot. ``resolve_bench``
        still raises there, with its own message.

        The staffed check happens before the resolve, not around it, because
        ``resolve_bench_for_workload`` raises on an unstaffed tier where the
        ``self.bench.get`` this replaced returned ``None``. Catching the
        ``KeyError`` instead would also swallow one raised deeper in the profile
        lookup, which is a real defect rather than an unstaffed bench.
        """

        if task.judgment is None:
            return None
        if task.judgment.tier not in self.bench:
            return None
        return resolve_bench_for_workload(
            task.judgment.tier,
            self._judgment_workload(task),
            self.bench,
        )

    @staticmethod
    def _judgment_workload(task: PowWowTaskSpec) -> JudgmentWorkload:
        match task.planning_phase:
            case PlanningPhase.SENIOR_INDEPENDENT_READING | PlanningPhase.STAFF_INDEPENDENT_READING:
                return JudgmentWorkload.INDEPENDENT_READING
            case _:
                return JudgmentWorkload.STANDARD

    def _local_harness_for(self, task: PowWowTaskSpec) -> LocalHarness | None:
        """The local harness this task belongs to, or ``None`` if it needs a CLI.

        Asked of the harness, not of the tier. A tier is how a plan names
        seniority; a harness is what actually answers. Keying this on
        ``tier == JUNIOR`` meant an operator who staffed junior with claude still
        got the local delegate, and the harness is the thing they changed.

        It answers with the harness rather than a boolean so a caller that has to
        report the refusal already holds the evidence, instead of re-deriving it
        behind an assertion.

        Independent of whether a delegate exists: "does this task need a worktree
        and a CLI capacity slot" has the same answer either way.
        """

        slot = self._task_bench_slot(task)
        if slot is None:
            return None
        harness = classify_harness(slot.harness)
        return harness if isinstance(harness, LocalHarness) else None

    def _is_junior_delegate(self, task: PowWowTaskSpec) -> bool:
        """Local-harness tasks run on the local model via the delegate path
        (not an external agent), when a delegate callback is available."""
        return self.delegate_fn is not None and self._local_harness_for(task) is not None

    def _authorize_spawn(
        self,
        task: PowWowTaskSpec,
        *,
        pow_wow_id: str,
        agent_name: str,
    ) -> CapabilityDenied | None:
        """Ask the grant ledger whether this agent may use what its plan gave it.

        This is the seam. The compiled plan says what an executor kind is
        permitted to do, `SpawnAuthority` turns that into a posture, and the
        process is launched with flags to match - all of it decided at compile
        time and none of it revocable. `capability_gate` is the other half: a
        runtime check against `tool_permission_requests`, where an operator can
        grant and revoke. The two have never met, because the principal that
        joins them - `AgentCaller` - was constructed nowhere in the codebase.

        It is constructed here, at the moment of spawning, because that is the
        one place where all three facts exist at once: who is acting (the
        harness), in what role (the task), and on whose behalf (the pow-wow).

        Only the *gated* capabilities are asked about. `read_repository` and
        `invoke_model` have no policy rule and would pass by having nothing to
        fail, so asking about them would put a check in the log that never checks
        anything. Asking about exactly `authority ∩ gated_capabilities()` is the
        honest statement of what is being authorized.

        Returns the first denial rather than all of them: an agent that may not
        write the repository is not going to be launched, and enumerating its
        other refusals is detail for a run that is not happening.
        """

        caller = AgentCaller(
            agent_name=agent_name,
            agent_role=task.role,
            # Required, not optional. Unscoped, the ledger answers with every
            # grant this agent name ever received in any pow-wow.
            pow_wow_id=pow_wow_id,
        )
        authority = self._task_spawn_authority(task)
        for capability in sorted(authority.capabilities & gated_capabilities()):
            verdict = check_capability(
                agent_name=caller.agent_name,
                agent_role=caller.agent_role,
                capability=capability,
                pow_wow_id=caller.pow_wow_id,
            )
            if isinstance(verdict, CapabilityDenied):
                return verdict
        return None

    def _build_capability_denied_result(
        self,
        task: PowWowTaskSpec,
        *,
        target_project: LinkedProject,
        agent_name: str,
        denial: CapabilityDenied,
    ) -> PowWowTaskResult:
        """Record the refusal, with the request that would lift it.

        A recorded failure rather than the `PermissionError` `ensure_capability`
        raises. That function is right for a tool call inside one workflow, where
        the caller is the only thing that dies. Here the caller is a scheduler
        holding the results of every sibling task in this pow-wow, and an
        exception would discard durable work to report a permission problem about
        one task. The refusal is the task's outcome; the pow-wow keeps going.

        Every denial names its remedy, because a capability an operator can grant
        and a message that does not say so is a gate that reads as a dead end.
        """

        reason = f"{denial.reason} -- {denial.remedy}"
        return PowWowTaskResult(
            task_name=task.task_name,
            role=task.role,
            status="failed",
            summary=f"{agent_name} was not authorized for {task.task_name}: {reason}",
            risks=(reason,),
            artifacts=(
                PowWowArtifact(
                    artifact_type="agent_capability_denied",
                    schema_version="agent_capability_denied.v1",
                    task_name=task.task_name,
                    content={
                        "schema_version": "agent_capability_denied.v1",
                        "mode": self.mode,
                        "agent_name": agent_name,
                        "agent_role": task.role,
                        "capability": denial.capability.value,
                        "reason": denial.reason,
                        "remedy": denial.remedy,
                        "task": task.to_payload(),
                        "target_project_id": target_project.id,
                    },
                ),
            ),
        )

    def _task_spawn_authority(self, task: PowWowTaskSpec) -> SpawnAuthority:
        """What a process spawned for this task may do.

        Two facts, intersected. The dispatch intent carries the ceiling the
        compiled plan declared for the milestone that asked for the work; the
        task's purpose carries what its role needs. One milestone fans out into
        an implementer, a reviewer, and a junior, so handing the ceiling to every
        task would give the reviewer ``write_repository``.

        A task that declares its own capabilities uses those instead of the
        purpose default, still narrowed by the ceiling, so a planner can be more
        specific than a role but never more permissive than the plan.
        """

        role = (
            SpawnAuthority.from_names(task.capabilities)
            if task.capabilities
            else authority_for_purpose(task.purpose)
        )
        return role.narrowed_to(self.spawn_ceiling)

    def _run_delegate_task(
        self,
        *,
        pow_wow_id: str,
        target_project: LinkedProject,
        task: PowWowTaskSpec,
        context: PowWowExecutionContext,
        dependency_results: Sequence[PowWowTaskResult] = (),
    ) -> PowWowTaskResult:
        """Run a junior task on the local model via delegate_task — a bounded
        prompt whose output is captured as a ledger artifact. No worktree. This
        is how the local junior tier coordinates with the external (claude/codex)
        tiers: through the durable ledger, not process IPC."""
        assert self.delegate_fn is not None  # guarded by _is_junior_delegate
        assert task.judgment is not None
        slot = self._task_bench_slot(task)
        assert slot is not None
        # The local lane is gated too. Its capabilities are ungated today, so
        # this passes, and that is the point: the check belongs at every way in,
        # not only at the ways that happen to spawn an external process. A local
        # model granted `run_command` tomorrow is checked because this line
        # already exists, rather than because somebody remembered.
        denial = self._authorize_spawn(task, pow_wow_id=pow_wow_id, agent_name=slot.harness.value)
        if denial is not None:
            return self._build_capability_denied_result(
                task,
                target_project=target_project,
                agent_name=slot.harness.value,
                denial=denial,
            )
        prompt = build_agent_task_prompt(
            task,
            context,
            dependency_results=dependency_results,
            dependency_compactor=self.dependency_compactor,
        )
        payload: dict[str, Any] = {}
        delegate_attempts = 0
        for attempt in range(2):
            delegate_attempts = attempt + 1
            attempt_prompt = prompt
            if attempt:
                attempt_prompt = (
                    f"{prompt}\n\n"
                    "Previous local delegate attempt returned an empty output. "
                    "Retry and return a non-empty answer."
                )
            try:
                payload = dict(
                    self.delegate_fn(
                        prompt=attempt_prompt,
                        task_name=task.task_name,
                        role=task.role,
                        tier=task.judgment.tier.value,
                        model=slot.model,
                        model_params={"cache_prompt": False},
                        # A resident delegate has no workflow of its own to
                        # record the model call against, and the pow-wow is the
                        # thing it belongs to. Directive delegates ignore it.
                        pow_wow_id=pow_wow_id,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - surface as a task failure, not a crash
                payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                break
            output_text = str(payload.get("output") or "").strip()
            if not (payload.get("ok") and not output_text):
                break
        output_text = str(payload.get("output") or "").strip()
        succeeded = bool(payload.get("ok")) and bool(output_text)
        if payload.get("ok") and not output_text:
            payload["error"] = "delegate returned ok with empty output"
        status: PowWowTaskStatus = "completed" if succeeded else "failed"
        risks = () if succeeded else (f"Junior delegate failed: {payload.get('error')}",)
        artifact = PowWowArtifact(
            artifact_type=DELEGATED_TASK_RUN_ARTIFACT_TYPE,
            schema_version="delegated_task_run.v1",
            task_name=task.task_name,
            content={
                "schema_version": "delegated_task_run.v1",
                "mode": "delegate",
                "tier": task.judgment.tier.value,
                "model": slot.model,
                "task": task.to_payload(),
                "target_project_id": target_project.id,
                "ok": succeeded,
                "output": payload.get("output"),
                "error": payload.get("error"),
                "metadata": payload.get("metadata", {}),
                "attempts": delegate_attempts,
                "auto_merge": AGENT_BRANCH_AUTO_MERGE,
            },
        )
        return PowWowTaskResult(
            task_name=task.task_name,
            role=task.role,
            status=status,
            summary=(
                f"Junior delegate ({task.judgment.tier.value}) "
                f"{'completed' if succeeded else 'failed'} on local model "
                f"{slot.model!r} via delegate_task."
            ),
            risks=tuple(risks),
            artifacts=(artifact,),
        )

    def _run_junior_batch(
        self,
        junior_tasks: Sequence[PowWowTaskSpec],
        *,
        pow_wow_id: str,
        target_project: LinkedProject,
        context: PowWowExecutionContext,
    ) -> dict[str, PowWowTaskResult]:
        """Run junior-tier delegate tasks CONCURRENTLY, bounded by the junior
        bench slot's capacity. Each is an independent bounded prompt -> local
        model -> ledger artifact; the ledger (WAL SQLite) serializes the writes
        safely, so N gemma4 juniors can run in parallel. Returns results keyed
        by task_name."""
        if not junior_tasks:
            return {}
        capacity = max(1, resolve_bench(Tier.JUNIOR, self.bench).capacity)
        if len(junior_tasks) == 1 or capacity == 1:
            return {
                task.task_name: self._run_delegate_task(
                    pow_wow_id=pow_wow_id,
                    target_project=target_project,
                    task=task,
                    context=context,
                )
                for task in junior_tasks
            }
        results: dict[str, PowWowTaskResult] = {}
        workers = min(capacity, len(junior_tasks))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self._run_delegate_task,
                    pow_wow_id=pow_wow_id,
                    target_project=target_project,
                    task=task,
                    context=context,
                ): task
                for task in junior_tasks
            }
            for future in concurrent.futures.as_completed(futures):
                task = futures[future]
                results[task.task_name] = future.result()
        return results

    def _allocate_worktree(
        self,
        *,
        source_repo: Path,
        pow_wow_id: str,
        task_name: str,
        base_commit_sha: str | None = None,
    ) -> WorktreeAllocation:
        """One fresh worktree, branched from the seed the intent declared.

        ``base_commit_sha`` is the dependency edge acting as a pipe: a chained
        milestone branches from its dependency's settled commit rather than
        from HEAD, which only moves at the CODE_MERGE gate. ``None`` branches
        from HEAD, the historical behavior. A declared seed the repository
        does not contain fails closed here, loudly, because branching from
        HEAD instead would silently rebuild the predecessor's work.
        """

        with self._worktree_lock:
            head_sha = run_git_command_for_output(source_repo, ["rev-parse", "HEAD"]).strip()
            if base_commit_sha is not None:
                commit_ref = f"{base_commit_sha}^{{commit}}"
                probe = subprocess.run(
                    ["git", "-C", str(source_repo), "cat-file", "-e", commit_ref],
                    capture_output=True,
                    text=True,
                    timeout=DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS,
                    check=False,
                )
                if probe.returncode != 0:
                    raise RuntimeError(
                        f"declared base commit {base_commit_sha} is not in {source_repo}; "
                        "the dependency's settled branch may have been pruned. Refusing to "
                        "seed from HEAD, which would silently drop the dependency's work."
                    )
            head_sha = base_commit_sha or head_sha
            self.worktree_root.mkdir(parents=True, exist_ok=True)
            branch_suffix = "-".join(
                (
                    _slugify_path_component(pow_wow_id),
                    _slugify_path_component(task_name),
                    uuid.uuid4().hex[:8],
                )
            )
            worktree_path = self.worktree_root / branch_suffix
            branch_name = f"{AGENT_WORKTREE_BRANCH_PREFIX}{branch_suffix}"
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(source_repo),
                    "worktree",
                    "add",
                    "-b",
                    branch_name,
                    str(worktree_path),
                    head_sha,
                ],
                capture_output=True,
                text=True,
                timeout=DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS,
                check=True,
            )
            return WorktreeAllocation(
                source_repo_path=str(source_repo),
                worktree_path=str(worktree_path),
                head_sha=head_sha,
                branch_name=branch_name,
                cleanup_policy=self.cleanup_policy,
                preserved=self.cleanup_policy == "preserve",
            )

    def _remove_worktree(self, source_repo: Path, worktree_path: Path) -> str | None:
        with self._worktree_lock:
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(source_repo),
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree_path),
                ],
                capture_output=True,
                text=True,
                timeout=DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS,
                check=False,
            )
        if proc.returncode == 0:
            return None
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)
        return proc.stderr.strip() or proc.stdout.strip()

    def _select_verification_commands(self, target_project: LinkedProject) -> tuple[str, ...]:
        if self.verification_commands:
            return self.verification_commands
        return tuple(target_project.verification_commands)


class FakeProcessPowWowExecutor(_WorktreePowWowExecutorBase):
    """Test backend that simulates an external agent inside isolated git worktrees."""

    mode = "fake_process"

    def __init__(
        self,
        *,
        worktree_root: Path,
        cleanup_policy: WorktreeCleanupPolicy = "remove",
        timeout_seconds: int = 30,
        verification_timeout_seconds: int = DEFAULT_VERIFICATION_COMMAND_TIMEOUT_SECONDS,
        verification_commands: Sequence[str] | None = None,
        agent_command: Sequence[str] | None = None,
    ) -> None:
        super().__init__(
            worktree_root=worktree_root,
            cleanup_policy=cleanup_policy,
            timeout_seconds=timeout_seconds,
            verification_timeout_seconds=verification_timeout_seconds,
            verification_commands=verification_commands,
        )
        self.agent_command = tuple(agent_command) if agent_command is not None else None

    def dispatch_pow_wow(
        self,
        pow_wow_id: str,
        target_project: LinkedProject,
        tasks: Sequence[PowWowTaskSpec],
        context: PowWowExecutionContext,
    ) -> PowWowRunResult:
        implementation_tasks = [task for task in tasks if is_implementation_task(task)]
        if not implementation_tasks:
            return PowWowRunResult(
                executor=type(self).__name__,
                mode=self.mode,
                pow_wow_id=pow_wow_id,
                target_project_id=target_project.id,
                target_project_path=str(target_project.expanded_path),
                status="BLOCKED",
                output_summary="No implementation tasks were available for worktree execution.",
                risks=("No isolated worktree was allocated.",),
                external_agents_started=False,
                auto_merge=AGENT_BRANCH_AUTO_MERGE,
            )

        task_results = tuple(
            self._run_implementation_task(
                pow_wow_id=pow_wow_id,
                target_project=target_project,
                task=task,
                context=context,
            )
            if is_implementation_task(task)
            else self._build_non_implementation_task_result(task, target_project)
            for task in tasks
        )
        changed_files = tuple(
            dict.fromkeys(
                file_name for task_result in task_results for file_name in task_result.changed_files
            )
        )
        verification_output = tuple(
            line for task_result in task_results for line in task_result.verification_output
        )
        run_status = derive_pow_wow_run_status(task_results)
        run_artifact = PowWowArtifact(
            artifact_type="pow_wow_external_run_result",
            schema_version="pow_wow_external_run_result.v1",
            content={
                "schema_version": "pow_wow_external_run_result.v1",
                "mode": self.mode,
                "status": run_status,
                "pow_wow_id": pow_wow_id,
                "target_project_id": target_project.id,
                "task_count": len(task_results),
                "implementation_task_count": len(implementation_tasks),
                "changed_files": list(changed_files),
                "cleanup_policy": self.cleanup_policy,
                "auto_merge": AGENT_BRANCH_AUTO_MERGE,
            },
        )
        return PowWowRunResult(
            executor=type(self).__name__,
            mode=self.mode,
            pow_wow_id=pow_wow_id,
            target_project_id=target_project.id,
            target_project_path=str(target_project.expanded_path),
            status=run_status,
            output_summary=(
                f"Fake external executor ran {len(implementation_tasks)} implementation task(s) "
                f"for {target_project.id}; status={run_status}; auto-merge remained disabled."
            ),
            tasks=task_results,
            changed_files=changed_files,
            verification_commands=self._select_verification_commands(target_project),
            verification_output=verification_output,
            risks=tuple(risk for task_result in task_results for risk in task_result.risks),
            artifacts=(run_artifact,),
            external_agents_started=True,
            auto_merge=AGENT_BRANCH_AUTO_MERGE,
        )

    def _build_non_implementation_task_result(
        self,
        task: PowWowTaskSpec,
        target_project: LinkedProject,
    ) -> PowWowTaskResult:
        artifact = PowWowArtifact(
            artifact_type="external_agent_task_plan",
            schema_version="external_agent_task_plan.v1",
            task_name=task.task_name,
            content={
                "schema_version": "external_agent_task_plan.v1",
                "mode": self.mode,
                "task": task.to_payload(),
                "target_project_id": target_project.id,
                "worktree_allocated": False,
                "reason": (
                    "FakeProcessPowWowExecutor allocates worktrees only for implementation tasks."
                ),
            },
        )
        return PowWowTaskResult(
            task_name=task.task_name,
            role=task.role,
            status="planned",
            summary=(
                f"Fake external executor left {task.task_name} planned; "
                "no implementation worktree was allocated for this role."
            ),
            artifacts=(artifact,),
        )

    def _run_implementation_task(
        self,
        *,
        pow_wow_id: str,
        target_project: LinkedProject,
        task: PowWowTaskSpec,
        context: PowWowExecutionContext,
    ) -> PowWowTaskResult:
        source_repo = target_project.expanded_path
        allocation = self._allocate_worktree(
            source_repo=source_repo,
            pow_wow_id=pow_wow_id,
            task_name=task.task_name,
            base_commit_sha=context.base_commit_sha,
        )
        worktree_path = Path(allocation.worktree_path)
        command_capture: CommandRunCapture | None = None
        verification_captures: tuple[CommandRunCapture, ...] = ()
        verification: VerificationOutcome = VerificationNotDeclared()
        verification_environment: dict[str, Any] | None = None
        checkpoint_eligible = False
        changed_files: tuple[str, ...] = ()
        diff_summary: dict[str, Any] = {}
        cleanup_error: str | None = None
        checkpoint: WorktreeCommitCheckpoint | None = None
        try:
            command = self.agent_command or self._build_default_agent_command(task, context)
            command_capture = run_captured_command(
                command,
                worktree_path,
                timeout_seconds=self.timeout_seconds,
            )
            changed_files = list_changed_worktree_files(
                worktree_path, base_head_sha=allocation.head_sha
            )
            diff_summary = summarize_worktree_diff(worktree_path, base_head_sha=allocation.head_sha)
            declared_verification = self._select_verification_commands(target_project)
            if declared_verification:
                gate_environment, stripped_names = verification_gate_environment(worktree_path)
                verification_environment = {"stripped": list(stripped_names)}
                verification_captures = tuple(
                    run_captured_shell_command(
                        verification_command,
                        worktree_path,
                        timeout_seconds=self.verification_timeout_seconds,
                        environment=gate_environment,
                    )
                    for verification_command in declared_verification
                )
            verification = classify_verification(declared_verification, verification_captures)
            checkpoint_eligible = command_capture.exit_code == 0
            if checkpoint_eligible and checkpoint_permitted(verification):
                checkpoint = _commit_worktree_checkpoint_at_lifecycle_boundary(
                    allocation,
                    task_name=task.task_name,
                )
        finally:
            if self.cleanup_policy == "remove":
                cleanup_error = self._remove_worktree(source_repo, worktree_path)

        uncertifiable = (
            uncertifiable_reason(verification, target_project_id=target_project.id)
            if checkpoint_eligible
            else None
        )
        exit_codes = [command_capture.exit_code if command_capture else 1]
        exit_codes.extend(capture.exit_code for capture in verification_captures)
        if checkpoint and checkpoint.error:
            exit_codes.append(1)
        if uncertifiable is not None:
            exit_codes.append(1)
        status: PowWowTaskStatus = (
            "completed" if all(code == 0 for code in exit_codes) else "failed"
        )
        risks = []
        if uncertifiable is not None:
            risks.append(uncertifiable)
        if cleanup_error:
            risks.append(f"Worktree cleanup failed: {cleanup_error}")
        if self.cleanup_policy == "preserve":
            risks.append(f"Worktree preserved for inspection: {worktree_path}")
        if checkpoint and checkpoint.error:
            risks.append(f"Worktree checkpoint failed: {checkpoint.error}")

        run_capture = {
            "schema_version": "external_agent_run.v1",
            "mode": self.mode,
            "task": task.to_payload(),
            "target_project_id": target_project.id,
            "worktree": allocation.to_payload(),
            "command": command_capture.to_payload() if command_capture else None,
            "changed_files": list(changed_files),
            "diff_summary": diff_summary,
            "verification": [capture.to_payload() for capture in verification_captures],
            "verification_environment": verification_environment,
            "commit_checkpoint": checkpoint.to_payload() if checkpoint else None,
            "cleanup_error": cleanup_error,
            "auto_merge": AGENT_BRANCH_AUTO_MERGE,
        }
        artifacts: tuple[PowWowArtifact, ...] = (
            PowWowArtifact(
                artifact_type="worktree_allocation",
                schema_version="worktree_allocation.v1",
                task_name=task.task_name,
                content={
                    "schema_version": "worktree_allocation.v1",
                    **allocation.to_payload(),
                },
            ),
            PowWowArtifact(
                artifact_type="external_agent_run",
                schema_version="external_agent_run.v1",
                task_name=task.task_name,
                content=run_capture,
            ),
        )
        if checkpoint is not None:
            artifacts = (
                *artifacts,
                PowWowArtifact(
                    artifact_type="worktree_commit_checkpoint",
                    schema_version="worktree_commit_checkpoint.v1",
                    task_name=task.task_name,
                    content={
                        "schema_version": "worktree_commit_checkpoint.v1",
                        "task_name": task.task_name,
                        "worktree": allocation.to_payload(),
                        **checkpoint.to_payload(),
                    },
                ),
            )
        verification_output = tuple(
            f"{capture.command} -> {capture.exit_code}\n{capture.stdout}{capture.stderr}"
            for capture in verification_captures
        )
        return PowWowTaskResult(
            task_name=task.task_name,
            role=task.role,
            status=status,
            summary=(
                f"Fake external process ran in isolated worktree {worktree_path}; "
                f"captured {len(changed_files)} changed file(s)."
            ),
            changed_files=changed_files,
            verification_commands=self._select_verification_commands(target_project),
            verification_output=verification_output,
            risks=tuple(risks),
            artifacts=artifacts,
        )

    def _build_default_agent_command(
        self,
        task: PowWowTaskSpec,
        context: PowWowExecutionContext,
    ) -> tuple[str, ...]:
        script = (
            "from pathlib import Path\n"
            "Path('fake_agent_output.txt').write_text("
            f"{task.task_name!r} + '\\n' + {context.goal!r} + '\\n', encoding='utf-8')\n"
            "print('fake external agent wrote fake_agent_output.txt')\n"
        )
        return (sys.executable, "-c", script)


class CliPowWowExecutor(_WorktreePowWowExecutorBase):
    """Batch executor that runs each tier's coding agent via its OWN headless
    CLI directly in the leased worktree.

    senior -> `claude --print --output-format json`, staff -> `codex exec`
    (read-only sandbox for review), junior -> local delegate. Clean stdout
    capture, exit codes, diff, verification, and a codex verdict - all to the
    durable ledger. The frontier CLIs are spawned directly and own their own
    agent loops; this path supplies the prompt and reads what they left behind.
    """

    mode = "cli"

    def __init__(
        self,
        *,
        worktree_root: Path,
        cleanup_policy: WorktreeCleanupPolicy = "remove",
        timeout_seconds: int = DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
        verification_timeout_seconds: int = DEFAULT_VERIFICATION_COMMAND_TIMEOUT_SECONDS,
        verification_commands: Sequence[str] | None = None,
        bench: Bench | None = None,
        delegate_fn: DelegateFn | None = None,
        dependency_compactor: ViewCompactor | None = None,
        agent_ledger_root: Path | None = None,
        coordination_command: CoordinationCommandFn | None = None,
        claude_bin: str = "claude",
        codex_bin: str = "codex",
        max_review_rounds: int = 4,
        artifact_writer: ArtifactWriter | None = None,
        supervisor_factory: type[StreamingCommandSupervisor] = StreamingCommandSupervisor,
        coordination_timeout_seconds: float = DEFAULT_COORDINATION_COMMAND_TIMEOUT_SECONDS,
        git_timeout_seconds: float = DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS,
        progress_assessment_timeout_seconds: float = DEFAULT_PROGRESS_ASSESSMENT_TIMEOUT_SECONDS,
        artifact_write_timeout_seconds: float = DEFAULT_ARTIFACT_WRITE_TIMEOUT_SECONDS,
        stream_drain_timeout_seconds: float = DEFAULT_STREAM_DRAIN_TIMEOUT_SECONDS,
        spawn_ceiling: SpawnAuthority | None = None,
    ) -> None:
        super().__init__(
            worktree_root=worktree_root,
            cleanup_policy=cleanup_policy,
            timeout_seconds=timeout_seconds,
            verification_timeout_seconds=verification_timeout_seconds,
            spawn_ceiling=spawn_ceiling,
            verification_commands=verification_commands,
            bench=bench,
            delegate_fn=delegate_fn,
            dependency_compactor=dependency_compactor,
            agent_ledger_root=agent_ledger_root,
        )
        self.claude_bin = claude_bin
        self.codex_bin = codex_bin
        self.coordination_command = coordination_command
        self.max_review_rounds = max_review_rounds
        self.artifact_writer = artifact_writer
        self.supervisor_factory = supervisor_factory
        self.coordination_timeout_seconds = coordination_timeout_seconds
        self.git_timeout_seconds = git_timeout_seconds
        self.progress_assessment_timeout_seconds = progress_assessment_timeout_seconds
        self.artifact_write_timeout_seconds = artifact_write_timeout_seconds
        self.stream_drain_timeout_seconds = stream_drain_timeout_seconds
        self._codex_auth_ok_cache: bool | None = None

    def _resolve_execution_task_id(
        self,
        *,
        pow_wow_id: str,
        task: PowWowTaskSpec,
        context: PowWowExecutionContext,
    ) -> str | None:
        existing = (context.task_ids_by_name or {}).get(task.task_name)
        if existing:
            return existing
        if not re.search(r"_(?:revision_)?r\d+$", task.task_name):
            return None
        if self.coordination_command is None:
            return None
        try:
            claimed = self.coordination_command(
                ClaimTask(
                    pow_wow_id=pow_wow_id,
                    task_name=task.task_name,
                    description=task.description,
                    blocked_by=task.blocked_by,
                )
            )
        except Exception:
            return None
        if isinstance(claimed, AcknowledgementResult):
            value = claimed.payload.values.get("task_id")
            return str(value) if value else None
        return None

    def _open_execution_attempt_lease(
        self,
        *,
        pow_wow_id: str,
        target_project: LinkedProject,
        task: PowWowTaskSpec,
        context: PowWowExecutionContext,
        harness: str,
        model: str | None,
        command: Sequence[str],
        cwd: Path,
        worktree: WorktreeAllocation | None,
        is_review: bool,
        source_revision: str | None = None,
        resumed_thread_id: str | None = None,
    ) -> ExecutionAttemptLease | None:
        if self.coordination_command is None:
            return None
        idempotency_key = build_execution_attempt_idempotency_key(
            pow_wow_id=pow_wow_id,
            target_project=target_project,
            task=task,
            harness=harness,
            model=model,
            dispatch_kind=self._resolve_task_dispatch_kind(task, context),
        )
        worker_id = f"cli:{harness}:{pow_wow_id}:{task.task_name}"
        task_id = self._resolve_execution_task_id(
            pow_wow_id=pow_wow_id,
            task=task,
            context=context,
        )
        attempt = ExecutionAttemptLease(
            idempotency_key=idempotency_key,
            worker_id=worker_id,
            task_id=task_id,
        )
        compensation = {
            "schema_version": "external_agent_compensation.v1",
            "strategy": "remove_or_reset_leased_worktree"
            if worktree is not None
            else "read_only_no_worktree",
            "cleanup_policy": self.cleanup_policy if worktree is not None else None,
            "source_repo_path": worktree.source_repo_path if worktree else str(cwd),
            "worktree_path": worktree.worktree_path if worktree else None,
            "head_sha": worktree.head_sha if worktree else None,
            "branch_name": worktree.branch_name if worktree else None,
            "is_review": is_review,
            "auto_reverse_patch": False,
        }
        doctrine_provenance = _build_engineering_doctrine_provenance(task)
        if doctrine_provenance is not None:
            compensation["engineering_doctrine"] = doctrine_provenance
        marketing_provenance = _build_marketing_site_doctrine_provenance(task)
        if marketing_provenance is not None:
            compensation["marketing_site_doctrine"] = marketing_provenance
        if source_revision is None:
            source_revision = (
                worktree.head_sha
                if worktree is not None
                else run_git_command_for_output(cwd, ("rev-parse", "HEAD")).strip()
            )
        open_command = OpenExecutionLease(
            idempotency_key=idempotency_key,
            worker_id=worker_id,
            timeout_seconds=self.timeout_seconds,
            agent_tier=task.judgment.tier.value if task.judgment else "unknown",
            agent_name=harness,
            task_role=task.role,
            model=model,
            target_project_id=target_project.id,
            planning_phase=task.planning_phase.value if task.planning_phase else None,
            source_revision=source_revision,
            permission_envelope_sha256=self._permission_envelope_sha256(task),
            resumed_thread_id=resumed_thread_id,
            intent_id=context.dispatch_intent_id,
            task_id=task_id,
            worktree_path=worktree.worktree_path if worktree else None,
            # The final argv item is the task prompt. Persisting it through the
            # argv-based coordination transport previously exceeded macOS
            # ARG_MAX on real milestones. The full contract remains in the
            # pow-wow artifacts and, on recovery, the checkpoint row.
            command=tuple(command[:-1]),
            compensation=compensation,
        )
        try:
            opened = self.coordination_command(open_command)
        except Exception as exc:  # noqa: BLE001 - execution should still be captured locally
            attempt.open_error = str(exc)
            return attempt
        if not isinstance(opened, EntityResult) or opened.field != "lease":
            attempt.open_error = f"coordination command returned malformed lease: {opened!r}"
            return attempt
        lease = opened.entity.values
        attempt.created = bool(opened.metadata.values.get("created"))
        attempt.lease_id = str(lease.get("lease_id") or "")
        attempt.open_status = str(lease.get("status") or "")
        result = lease.get("result")
        attempt.result = result if isinstance(result, Mapping) else None
        if attempt.created and attempt.open_status in _LEASE_ACTIVE_STATUSES:
            reach_lifecycle_transition(
                LifecycleTransitionPoint.AFTER_LEASE_STARTED,
                lease_id=attempt.lease_id,
                worker_id=attempt.worker_id,
                intent_id=context.dispatch_intent_id,
                task_id=task_id,
                task_name=task.task_name,
                harness=harness,
                worktree_path=worktree.worktree_path if worktree else None,
            )
        if attempt.open_status in LEASE_TERMINAL_STATUSES:
            attempt.reused_terminal = True
        elif attempt.open_status in _LEASE_ACTIVE_STATUSES and not attempt.created:
            attempt.blocked_existing_active = True
        return attempt

    def _build_existing_execution_attempt_capture(
        self,
        attempt: ExecutionAttemptLease | None,
        *,
        command: Sequence[str],
        cwd: Path,
    ) -> CommandRunCapture | None:
        if attempt is None:
            return None
        if attempt.reused_terminal:
            return build_command_capture_from_lease_result(
                attempt.result,
                fallback_command=command,
                cwd=cwd,
                status=attempt.open_status,
            )
        if attempt.blocked_existing_active:
            return CommandRunCapture(
                command=shlex.join(str(part) for part in command),
                cwd=str(cwd),
                stdout="",
                stderr=(
                    f"Execution lease {attempt.lease_id} is already "
                    f"{attempt.open_status}; not duplicating external process"
                ),
                exit_code=125,
            )
        return None

    def _complete_execution_attempt_lease(
        self,
        attempt: ExecutionAttemptLease | None,
        *,
        capture: CommandRunCapture,
        dirty_worktree: Mapping[str, Any],
        supervised_result: SupervisedCommandResult | None = None,
    ) -> None:
        if (
            self.coordination_command is None
            or attempt is None
            or not attempt.lease_id
            or attempt.reused_terminal
            or attempt.blocked_existing_active
        ):
            return
        fallback_reason = infer_frontier_fallback_reason(capture)
        status = classify_execution_lease_status(capture)
        inferred_failure = classify_failure(f"{capture.stderr}\n{capture.stdout}")
        agent_status = (
            supervised_result.agent_status.value
            if supervised_result
            else (
                AgentStatus.COMPLETED.value if status == "COMPLETED" else AgentStatus.FAILED.value
            )
        )
        agent_failure = (
            supervised_result.agent_failure
            if supervised_result
            else (None if status == "COMPLETED" else inferred_failure.value)
        )
        inferred_category = failure_category(agent_failure)
        result = {
            "schema_version": "external_agent_execution_attempt.v1",
            "status": status,
            "failure_reason": fallback_reason,
            "agent_status": agent_status,
            "agent_failure": agent_failure,
            "agent_failure_category": (
                supervised_result.agent_failure_category
                if supervised_result
                else (inferred_category.value if inferred_category else None)
            ),
            "supervisor_status": (
                supervised_result.supervisor_status.value
                if supervised_result
                else SupervisorStatus.COMPLETED.value
            ),
            "supervisor_failure": (
                supervised_result.supervisor_failure if supervised_result else None
            ),
            "persistence_status": (
                supervised_result.persistence_status.value
                if supervised_result
                else PersistenceStatus.COMPLETED.value
            ),
            "persistence_failure": (
                supervised_result.persistence_failure if supervised_result else None
            ),
            "next_action": (
                ExecutionTransition.SWITCH_TO_FALLBACK.value
                if warrants_provider_swap(fallback_reason)
                else None
            ),
            "replacement_policy": (
                "other_frontier_provider" if warrants_provider_swap(fallback_reason) else None
            ),
            "command_capture": build_command_capture_lease_payload(capture),
            "dirty_worktree": dict(dirty_worktree),
            "retry_policy": DISPATCH_RETRY_POLICY,
            "auto_merge": AGENT_BRANCH_AUTO_MERGE,
            "streaming_supervisor": (
                {
                    "event_count": supervised_result.event_count,
                    "transcript_artifact_id": supervised_result.transcript_artifact_id,
                    "checkpoint_id": supervised_result.checkpoint_id,
                    "checkpoint_artifact_ids": list(supervised_result.checkpoint_artifact_ids),
                    "checkpoint_reason": supervised_result.checkpoint_reason,
                    "preserve_worktree": supervised_result.preserve_worktree,
                    "supervisor_error": supervised_result.supervisor_error,
                    "activity_status": supervised_result.activity_status,
                    "progress_recommendation": supervised_result.progress_recommendation,
                }
                if supervised_result is not None
                else None
            ),
        }
        error = describe_execution_lease_error(
            capture,
            fallback_reason=fallback_reason,
            status=status,
            timeout_seconds=self.timeout_seconds,
        )
        complete_command = CompleteExecutionLease(
            lease_id=attempt.lease_id,
            status=ExecutionLeaseTerminalStatus(status),
            result=result,
            error=error,
        )
        try:
            completed = self.coordination_command(complete_command)
        except Exception as exc:  # noqa: BLE001 - task artifact still captures the run
            attempt.complete_error = str(exc)
            return
        if isinstance(completed, EntityResult) and completed.field == "lease":
            lease = completed.entity.values
            attempt.complete_status = str(lease.get("status") or status)
        else:
            attempt.complete_status = status
            attempt.complete_error = (
                f"coordination command returned malformed completion: {completed!r}"
            )

    def _has_valid_codex_authentication(self) -> bool:
        """Preflight codex auth once (via `codex login status`) so a revoked
        token fails fast with a clear message instead of a mid-run 401."""
        if self._codex_auth_ok_cache is None:
            try:
                proc = subprocess.run(
                    [self.codex_bin, "login", "status"],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self._codex_auth_ok_cache = proc.returncode == 0
            except Exception:  # noqa: BLE001 - treat any probe failure as not-authed
                self._codex_auth_ok_cache = False
        return self._codex_auth_ok_cache

    def _resolve_frontier_harness(self, slot: BenchSlot | None) -> HarnessKind:
        """Which CLI a slot spawns, or the refusal that says it spawns none.

        A task with no judgment role has no slot and no tier, and claude has
        always been the default for those; that stays. What is new is that a slot
        naming a local harness is answered here rather than falling into the same
        default. Falling into it is how a junior slot of ``pi``/``gemma4`` became
        ``claude --model gemma4`` and a ``401 Not logged in``.
        """

        if slot is None:
            return FrontierHarness.CLAUDE
        return classify_harness(slot.harness)

    def _build_local_harness_refusal_result(
        self,
        task: PowWowTaskSpec,
        *,
        target_project: LinkedProject,
        local: LocalHarness,
    ) -> PowWowTaskResult:
        """Refuse to spawn a CLI for a task that belongs on the local model.

        Reaching here means this executor was built without a delegate while its
        bench staffs the task's tier locally. That is a construction mistake, and
        it is recorded as this task's failure rather than raised, because the
        pow-wow's other tasks and their artifacts are durable work that an
        exception here would discard. The reason travels in ``risks``, which is
        what reaches the intent's error column.
        """

        message = (
            f"{local.describe()}, and this executor has no delegate callback to "
            f"call it with; task {task.task_name} cannot run"
        )
        return PowWowTaskResult(
            task_name=task.task_name,
            role=task.role,
            status="failed",
            summary=f"{message}.",
            risks=(message,),
            artifacts=(
                PowWowArtifact(
                    artifact_type="task_blocked",
                    schema_version="task_blocked.v1",
                    task_name=task.task_name,
                    content={
                        "schema_version": "task_blocked.v1",
                        "mode": self.mode,
                        "task": task.to_payload(),
                        "target_project_id": target_project.id,
                        "reason": message,
                        "harness": local.harness.value,
                        "auto_merge": AGENT_BRANCH_AUTO_MERGE,
                    },
                ),
            ),
        )

    @staticmethod
    def _planning_phase_of_result(result: PowWowTaskResult) -> PlanningPhase | None:
        for artifact in result.artifacts:
            task_payload = artifact.content.get("task")
            if not isinstance(task_payload, Mapping):
                continue
            raw_phase = task_payload.get("planning_phase")
            if isinstance(raw_phase, str):
                return PlanningPhase(raw_phase)
        return None

    @staticmethod
    def _authority_sha256(authority: SpawnAuthority) -> str:
        payload = {
            "capabilities": list(authority.to_names()),
            "posture": describe_posture(authority.posture()),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _permission_envelope_sha256(self, task: PowWowTaskSpec) -> str:
        return self._authority_sha256(self._task_spawn_authority(task))

    def _frontier_launch_decision(
        self,
        *,
        pow_wow_id: str,
        target_project: LinkedProject,
        task: PowWowTaskSpec,
        context: PowWowExecutionContext,
        dependency_results: Sequence[PowWowTaskResult],
        harness: FrontierHarness,
        model: str | None,
        source_revision: str,
    ) -> FrontierLaunchDecision:
        if task.planning_phase in {
            PlanningPhase.STAFF_INDEPENDENT_READING,
            PlanningPhase.STAFF_FINAL_REVIEW,
        }:
            return StartFreshIndependent("staff planning phases require independent judgment")
        if harness is not FrontierHarness.CODEX:
            return StartFreshBounded("the selected harness cannot resume Codex threads")
        if task.planning_phase is not PlanningPhase.SENIOR_OWNED_PLAN:
            return StartFreshBounded("task is not the senior reading-to-implementation boundary")
        source_result = next(
            (
                result
                for result in dependency_results
                if self._planning_phase_of_result(result)
                is PlanningPhase.SENIOR_INDEPENDENT_READING
            ),
            None,
        )
        if source_result is None:
            return StartFreshBounded("senior independent reading dependency is unavailable")
        source_task_id = (context.task_ids_by_name or {}).get(source_result.task_name)
        if not source_task_id:
            return StartFreshBounded("senior independent reading has no durable task id")
        if self.coordination_command is None:
            return StartFreshBounded("coordination ledger is unavailable")
        reader_slot = resolve_bench_for_workload(
            Tier.SENIOR,
            JudgmentWorkload.INDEPENDENT_READING,
            self.bench,
        )
        if reader_slot.harness is not Harness.CODEX:
            return StartFreshBounded("the configured senior reader is not a Codex thread")
        command = FindAgentContinuation(
            source_task_id=source_task_id,
            pow_wow_id=pow_wow_id,
            harness=harness.value,
            source_model=reader_slot.model,
            target_project_id=target_project.id,
            source_revision=source_revision,
        )
        try:
            found = self.coordination_command(command)
        except Exception as exc:  # noqa: BLE001 - a cold start remains valid
            return StartFreshBounded(f"continuation lookup failed: {type(exc).__name__}: {exc}")
        if not isinstance(found, EntityResult) or found.field != "continuation":
            return StartFreshBounded("continuation lookup returned a malformed result")
        if found.metadata.values.get("compatible") is not True:
            reason = str(found.metadata.values.get("reason") or "incompatible")
            return StartFreshBounded(f"continuation {reason}")
        thread_id = found.entity.values.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("compatible continuation requires a non-empty thread_id")
        source_permission = found.entity.values.get("permission_envelope_sha256")
        source_authority = authority_for_purpose(TaskPurpose.ADVISORY).narrowed_to(
            self.spawn_ceiling
        )
        target_authority = self._task_spawn_authority(task)
        expected_source_permission = self._authority_sha256(source_authority)
        target_permission = self._authority_sha256(target_authority)
        if source_permission != expected_source_permission:
            return StartFreshBounded("continuation source permission envelope mismatch")
        if not isinstance(source_authority.posture(), ReadOnlyInspection):
            return StartFreshBounded("continuation source is not read-only inspection")
        if not isinstance(target_authority.posture(), UnattendedImplementation):
            return StartFreshBounded("continuation target is not unattended implementation")
        return ResumeExisting(
            thread_id=thread_id,
            source_task_name=source_result.task_name,
            source_task_id=source_task_id,
            authority_transition=ReadOnlyToImplementation(
                source_permission_envelope_sha256=expected_source_permission,
                target_permission_envelope_sha256=target_permission,
            ),
            model_transition=ReaderToImplementationModelTransition(
                source_model=reader_slot.model,
                target_model=model,
            ),
        )

    def _build_agent_cli_command(
        self,
        harness: FrontierHarness,
        model: str | None,
        prompt: str,
        posture: SpawnPosture,
        reasoning_effort: str | None = None,
        continuation_thread_id: str | None = None,
    ) -> tuple[str, ...]:
        """The argv for one frontier agent run.

        Typed on ``FrontierHarness`` rather than ``str`` so the local harness is
        not expressible here, and matched exhaustively so a new frontier harness
        is a type error rather than a silent claude invocation.

        The fourth argument used to be ``is_review: bool``, and every task that
        boolean called false was launched with the sandbox off. It is now the
        posture derived from the compiled capability set, matched exhaustively
        for the same reason: a fourth posture must be a type error here rather
        than a silent fall-through to the bypass.

        The middle posture is the one that did not exist. A test runner and a
        repository validator both need a shell and neither should be able to edit
        what they are checking, and with only two postures available they were
        given an implementer's.
        """

        if continuation_thread_id is not None and harness is not FrontierHarness.CODEX:
            raise ValueError("only the Codex harness can resume a Codex thread")

        match harness:
            case FrontierHarness.CODEX:
                cmd = [self.codex_bin, "exec"]
                if continuation_thread_id is not None:
                    cmd.append("resume")
                cmd += ["--skip-git-repo-check", "--json"]
                cmd += _CODEX_SANDBOX_ARGS[describe_posture(posture)]
                if model:
                    cmd += ["--model", model]
                if reasoning_effort:
                    cmd += ["-c", f"model_reasoning_effort={reasoning_effort}"]
                if self.agent_ledger_root is not None:
                    cmd += codex_mcp_args(self.agent_ledger_root)
                if continuation_thread_id is not None:
                    cmd.append(continuation_thread_id)
                cmd.append(prompt)
                return tuple(cmd)
            case FrontierHarness.CLAUDE:
                cmd = [
                    self.claude_bin,
                    "--print",
                    "--output-format",
                    "stream-json",
                    "--verbose",
                ]
                cmd += _CLAUDE_PERMISSION_ARGS[describe_posture(posture)]
                if model:
                    cmd += ["--model", model]
                if reasoning_effort:
                    cmd += ["--effort", reasoning_effort]
                if self.agent_ledger_root is not None:
                    cmd += claude_mcp_args(self.agent_ledger_root)
                cmd.append(prompt)
                return tuple(cmd)
        assert_never(harness)

    def _run_frontier_command(
        self,
        command: Sequence[str],
        cwd: Path,
        *,
        execution_attempt: ExecutionAttemptLease | None,
        harness: str,
        env: Mapping[str, str] | None,
        source_repo_path: Path | None,
        base_head_sha: str | None,
        saga_id: str,
        pow_wow_id: str,
        task_contract: str,
    ) -> tuple[CommandRunCapture, SupervisedCommandResult | None]:
        env = _headless_agent_environment(env)
        if execution_attempt is not None and execution_attempt.open_error:
            return (
                CommandRunCapture(
                    command=shlex.join(str(part) for part in command),
                    cwd=str(cwd),
                    stdout="",
                    stderr=(
                        "execution lease could not be opened; refusing unsupervised "
                        f"frontier process: {execution_attempt.open_error}"
                    ),
                    exit_code=125,
                ),
                None,
            )
        if (
            self.coordination_command is None
            or self.artifact_writer is None
            or execution_attempt is None
            or not execution_attempt.lease_id
        ):
            return (
                run_captured_command(
                    command,
                    cwd,
                    timeout_seconds=self.timeout_seconds,
                    env=env,
                ),
                None,
            )
        supervisor = self.supervisor_factory(
            coordination_command=self.coordination_command,
            artifact_writer=self.artifact_writer,
            progress_assessor=(
                self._assess_stalled_progress if self.delegate_fn is not None else None
            ),
            coordination_timeout_seconds=self.coordination_timeout_seconds,
            git_timeout_seconds=self.git_timeout_seconds,
            progress_assessment_timeout_seconds=self.progress_assessment_timeout_seconds,
            artifact_write_timeout_seconds=self.artifact_write_timeout_seconds,
            stream_drain_timeout_seconds=self.stream_drain_timeout_seconds,
        )
        try:
            with profiled_step(
                "frontier_execution_supervisor",
                workflow_type="agent_execution",
                lease_id=execution_attempt.lease_id,
                harness=harness,
                saga_id=saga_id,
                pow_wow_id=pow_wow_id,
            ):
                result = asyncio.run(
                    supervisor.run(
                        command,
                        cwd,
                        lease=execution_attempt,
                        harness=harness,
                        timeout_seconds=self.timeout_seconds,
                        env=env,
                        source_repo_path=source_repo_path,
                        base_head_sha=base_head_sha,
                        saga_id=saga_id,
                        pow_wow_id=pow_wow_id,
                        task_contract=task_contract,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - convert supervisor failure to recovery state
            error = f"streaming supervisor failed: {type(exc).__name__}: {exc}"
            transcript_artifact_id: str | None = None
            checkpoint_id: str | None = None
            checkpoint_error = error
            try:
                transcript_ref = self.artifact_writer.write_text(
                    role="agent_execution_transcript",
                    text=json.dumps(
                        {
                            "source": "lifecycle",
                            "kind": "supervisor.failed",
                            "payload": {"error": error},
                        },
                        sort_keys=True,
                    )
                    + "\n",
                    workflow_id=None,
                    schema_version="agent_execution_transcript.v1",
                    mime_type="application/x-ndjson",
                )
                transcript_artifact_id = str(transcript_ref.artifact_id)
                checkpoint_result = self.coordination_command(
                    CreateExecutionCheckpoint(
                        lease_id=execution_attempt.lease_id,
                        reason="supervisor_error",
                        status="FAILED",
                        saga_id=saga_id,
                        pow_wow_id=pow_wow_id,
                        worktree_path=str(cwd),
                        source_repo_path=(str(source_repo_path) if source_repo_path else None),
                        base_head_sha=base_head_sha,
                        transcript_artifact_id=transcript_artifact_id,
                        task_contract=task_contract[:50_000],
                        event_summary="supervisor.failed",
                        error=error,
                    )
                )
                if isinstance(checkpoint_result, EntityResult):
                    checkpoint_id = (
                        str(checkpoint_result.entity.values.get("checkpoint_id") or "") or None
                    )
            except Exception as checkpoint_exc:  # noqa: BLE001
                checkpoint_error = (
                    f"{error}; failed to persist recovery checkpoint: "
                    f"{type(checkpoint_exc).__name__}: {checkpoint_exc}"
                )
            capture = CommandRunCapture(
                command=shlex.join(str(part) for part in command),
                cwd=str(cwd),
                stdout="",
                stderr=checkpoint_error,
                exit_code=130,
            )
            result = SupervisedCommandResult(
                capture=capture,
                deadline_reached=False,
                cancel_requested=False,
                transcript_artifact_id=transcript_artifact_id,
                checkpoint_id=checkpoint_id,
                checkpoint_artifact_ids=(
                    (transcript_artifact_id,) if transcript_artifact_id else ()
                ),
                checkpoint_reason="supervisor_error",
                preserve_worktree=True,
                event_count=0,
                supervisor_error=checkpoint_error,
                agent_status=AgentStatus.UNKNOWN,
                agent_failure=None,
                agent_failure_category=None,
                supervisor_status=SupervisorStatus.FAILED,
                supervisor_failure=checkpoint_error,
                persistence_status=(
                    PersistenceStatus.COMPLETED
                    if transcript_artifact_id
                    else PersistenceStatus.FAILED
                ),
                persistence_failure=(
                    None
                    if transcript_artifact_id
                    else InfrastructureFailure.ARTIFACT_WRITE_FAILED.value
                ),
            )
        return result.capture, result

    def _assess_stalled_progress(self, evidence: Mapping[str, object]) -> Mapping[str, object]:
        """Ask the local junior tier for an advisory, machine-readable decision."""

        if self.delegate_fn is None:
            raise RuntimeError("junior progress assessor is not configured")
        slot = resolve_bench(Tier.JUNIOR, self.bench)
        prompt = (
            "A deterministic process supervisor observed no meaningful progress from a "
            "senior/staff frontier agent. Heartbeats prove only ownership/liveness and "
            "must not be treated as work. Review the visible evidence below. Return one "
            "JSON object only with schema_version='execution_progress_assessment.v1', "
            "recommendation equal to CONTINUE, CHECKPOINT, SPLIT, or PAUSE_OPERATOR, "
            "a concise rationale, and continuations as a JSON list. SPLIT requires at "
            "least two concrete continuations. You are advisory: the deterministic "
            "supervisor alone may signal the process.\n\n" + json.dumps(evidence, sort_keys=True)
        )
        payload = dict(
            self.delegate_fn(
                prompt=prompt,
                task_name=f"progress_assessment_{str(evidence.get('lease_id') or '')[:12]}",
                role="progress_assessor",
                tier=Tier.JUNIOR.value,
                model=slot.model,
                model_params={"cache_prompt": False},
                timeout_seconds=self.progress_assessment_timeout_seconds,
            )
        )
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("error") or "junior delegate failed"))
        output = str(payload.get("output") or "").strip()
        if output.startswith("```"):
            output = re.sub(r"^```(?:json)?\s*|\s*```$", "", output, flags=re.IGNORECASE)
        try:
            decision = json.loads(output)
        except json.JSONDecodeError:
            start, end = output.find("{"), output.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("junior progress assessor returned no JSON object") from None
            decision = json.loads(output[start : end + 1])
        if not isinstance(decision, Mapping):
            raise ValueError("junior progress assessment must be a JSON object")
        recommendation = str(decision.get("recommendation") or "").upper()
        continuations = decision.get("continuations")
        if recommendation == "SPLIT" and (
            not isinstance(continuations, list) or len(continuations) < 2
        ):
            raise ValueError("SPLIT progress assessment requires at least two continuations")
        return dict(decision)

    def _build_non_implementation_task_result(
        self, task: PowWowTaskSpec, target_project: LinkedProject
    ) -> PowWowTaskResult:
        artifact = PowWowArtifact(
            artifact_type="cli_agent_task_plan",
            schema_version="cli_agent_task_plan.v1",
            task_name=task.task_name,
            content={
                "schema_version": "cli_agent_task_plan.v1",
                "mode": self.mode,
                "task": task.to_payload(),
                "target_project_id": target_project.id,
                "worktree_allocated": False,
            },
        )
        return PowWowTaskResult(
            task_name=task.task_name,
            role=task.role,
            status="planned",
            summary=f"CLI executor left {task.task_name} planned (no agent role).",
            artifacts=(artifact,),
        )

    def _select_alternate_frontier_slot(
        self, failed_harness: FrontierHarness
    ) -> tuple[FrontierHarness, BenchSlot] | None:
        """Resolve the other frontier provider from the configured bench.

        "The other one" is only a total function over a two-member set, which is
        why this takes ``FrontierHarness``. Over the wider ``Harness`` it read
        ``pi`` as not-claude and answered claude, naming a cross-provider
        fallback for a harness that never runs a provider at all.

        The harness is returned with the slot so the caller does not have to
        re-derive from ``slot.harness`` a fact this already decided.
        """

        alternate = (
            FrontierHarness.CODEX
            if failed_harness is FrontierHarness.CLAUDE
            else FrontierHarness.CLAUDE
        )
        slot = next(
            (slot for slot in self.bench.values() if slot.harness.value == alternate.value),
            None,
        )
        return None if slot is None else (alternate, slot)

    def _execute_frontier_fallback_task(
        self,
        *,
        pow_wow_id: str,
        target_project: LinkedProject,
        task: PowWowTaskSpec,
        context: PowWowExecutionContext,
        dependency_results: Sequence[PowWowTaskResult] = (),
        failed_harness: FrontierHarness,
        failed_model: str | None,
        failure_reason: FrontierFallbackReason,
        failed_capture: CommandRunCapture,
        failed_attempt: ExecutionAttemptLease | None,
        failed_supervised_result: SupervisedCommandResult | None,
        is_review: bool,
        worktree: WorktreeAllocation | None,
        changed_files: tuple[str, ...] = (),
        diff_summary: Mapping[str, Any] | None = None,
    ) -> PowWowTaskResult | None:
        alternate = self._select_alternate_frontier_slot(failed_harness)
        if alternate is None:
            return None
        alternate_frontier, alternate_slot = alternate
        alternate_harness = alternate_frontier.value
        alternate_model = alternate_slot.model
        cwd = Path(worktree.worktree_path) if worktree else target_project.expanded_path
        prompt = (
            f"The primary {failed_harness} process was unavailable because it hit "
            f"{failure_reason}. Act as its one bounded cross-provider replacement.\n\n"
            + build_agent_task_prompt(
                task,
                context,
                dependency_results=dependency_results,
                dependency_compactor=self.dependency_compactor,
                audit_context_block=self._audit_context_block_for(
                    task,
                    target_project=target_project,
                    repo_path=cwd,
                ),
            )
        )
        # The replacement inherits the posture, not the review flag. A fallback
        # that widened authority because the first process died would be the one
        # place a crash buys permissions.
        fallback_authority = self._task_spawn_authority(task)
        command = self._build_agent_cli_command(
            alternate_frontier,
            alternate_model,
            prompt,
            fallback_authority.posture(),
            reasoning_effort=alternate_slot.reasoning_effort,
        )
        alternate_attempt: ExecutionAttemptLease | None = None
        supervised_result: SupervisedCommandResult | None = None
        if (
            alternate_frontier is FrontierHarness.CODEX
            and not self._has_valid_codex_authentication()
        ):
            alternate_capture = CommandRunCapture(
                command=shlex.join(command),
                cwd=str(cwd),
                stdout="",
                stderr="codex authentication invalid or expired — run `codex login`",
                exit_code=126,
            )
        else:
            alternate_attempt = self._open_execution_attempt_lease(
                pow_wow_id=pow_wow_id,
                target_project=target_project,
                task=task,
                context=context,
                harness=alternate_harness,
                model=alternate_model,
                command=command,
                cwd=cwd,
                worktree=worktree,
                is_review=is_review,
            )
            self._record_fallback_transition(
                failed_attempt=failed_attempt,
                replacement_attempt=alternate_attempt,
                failed_harness=failed_harness,
                replacement_harness=alternate_harness,
                failure_reason=failure_reason,
                supervised_result=failed_supervised_result,
            )
            alternate_capture = self._build_existing_execution_attempt_capture(
                alternate_attempt,
                command=command,
                cwd=cwd,
            )
            if alternate_capture is None:
                alternate_capture, supervised_result = self._run_frontier_command(
                    command,
                    cwd,
                    execution_attempt=alternate_attempt,
                    harness=alternate_harness,
                    env=build_assigned_worktree_environment(context) if worktree else None,
                    source_repo_path=target_project.expanded_path,
                    base_head_sha=worktree.head_sha if worktree else None,
                    saga_id=context.saga_id,
                    pow_wow_id=pow_wow_id,
                    task_contract=prompt,
                )

        fallback_changed_files = (
            list_changed_worktree_files(
                cwd,
                base_head_sha=worktree.head_sha if not is_review else None,
            )
            if worktree
            else ()
        )
        fallback_diff_summary = (
            summarize_worktree_diff(
                cwd,
                base_head_sha=worktree.head_sha if not is_review else None,
            )
            if worktree
            else {}
        )
        declared_verification = self._select_verification_commands(target_project)
        verification_captures: tuple[CommandRunCapture, ...] = ()
        verification_environment: dict[str, Any] | None = None
        if (
            declared_verification
            and worktree
            and alternate_capture.exit_code == 0
            and not (supervised_result and supervised_result.checkpoint_reason)
        ):
            gate_environment, stripped_names = verification_gate_environment(cwd)
            verification_environment = {"stripped": list(stripped_names)}
            verification_captures = tuple(
                run_captured_shell_command(
                    command_text,
                    cwd,
                    timeout_seconds=self.verification_timeout_seconds,
                    environment=gate_environment,
                )
                for command_text in declared_verification
            )
        verification = classify_verification(declared_verification, verification_captures)
        checkpoint_eligible = (
            worktree is not None and not is_review and alternate_capture.exit_code == 0
        )
        checkpoint: WorktreeCommitCheckpoint | None = None
        if worktree is not None and checkpoint_eligible and checkpoint_permitted(verification):
            checkpoint = _commit_worktree_checkpoint_at_lifecycle_boundary(
                worktree,
                task_name=task.task_name,
            )
        if alternate_attempt is not None:
            self._complete_execution_attempt_lease(
                alternate_attempt,
                capture=alternate_capture,
                dirty_worktree={
                    "schema_version": "dirty_worktree.v1",
                    "source_repo_path": str(target_project.expanded_path),
                    "worktree_path": str(cwd) if worktree else None,
                    "head_sha": worktree.head_sha if worktree else None,
                    "branch_name": worktree.branch_name if worktree else None,
                    "changed_files": list(fallback_changed_files),
                    "diff_summary": fallback_diff_summary,
                    "commit_checkpoint": checkpoint.to_payload() if checkpoint else None,
                    "cleanup_policy": self.cleanup_policy if worktree else None,
                    "cleanup_requested": False,
                    "cleanup_applied": False,
                    "cleanup_deferred": bool(worktree),
                    "cleanup_error": None,
                },
                supervised_result=supervised_result,
            )

        uncertifiable = (
            uncertifiable_reason(verification, target_project_id=target_project.id)
            if checkpoint_eligible
            else None
        )
        exit_codes = [alternate_capture.exit_code]
        exit_codes.extend(capture.exit_code for capture in verification_captures)
        if checkpoint and checkpoint.error:
            exit_codes.append(1)
        if uncertifiable is not None:
            exit_codes.append(1)
        status: PowWowTaskStatus = (
            "completed" if all(exit_code == 0 for exit_code in exit_codes) else "failed"
        )
        output_text = extract_agent_cli_output(alternate_capture.stdout)
        wrapper = PowWowArtifact(
            artifact_type=FRONTIER_FALLBACK_RUN_ARTIFACT_TYPE,
            schema_version="frontier_fallback_run.v2",
            task_name=task.task_name,
            content={
                "schema_version": "frontier_fallback_run.v2",
                "engineering_doctrine": _build_engineering_doctrine_provenance(task),
                "mode": self.mode,
                "reason": failure_reason,
                "failed_harness": failed_harness,
                "failed_model": failed_model,
                "fallback_harness": alternate_harness,
                "fallback_model": alternate_model,
                "fallback_reasoning_effort": alternate_slot.reasoning_effort,
                "is_review": is_review,
                "spawn_posture": describe_posture(fallback_authority.posture()),
                "permitted_capabilities": list(fallback_authority.to_names()),
                "task": task.to_payload(),
                "target_project_id": target_project.id,
                "worktree": worktree.to_payload() if worktree else None,
                "failed_command": failed_capture.to_payload(),
                "fallback_execution_lease": (
                    alternate_attempt.to_payload() if alternate_attempt is not None else None
                ),
                "streaming_supervisor": (
                    {
                        "event_count": supervised_result.event_count,
                        "transcript_artifact_id": supervised_result.transcript_artifact_id,
                        "checkpoint_id": supervised_result.checkpoint_id,
                        "checkpoint_artifact_ids": list(supervised_result.checkpoint_artifact_ids),
                        "checkpoint_reason": supervised_result.checkpoint_reason,
                        "preserve_worktree": supervised_result.preserve_worktree,
                        "supervisor_error": supervised_result.supervisor_error,
                    }
                    if supervised_result is not None
                    else None
                ),
                "fallback_command": alternate_capture.to_payload(),
                "fallback_output": output_text,
                "changed_files_before_fallback": list(changed_files),
                "diff_summary_before_fallback": dict(diff_summary or {}),
                "changed_files": list(fallback_changed_files),
                "diff_summary": fallback_diff_summary,
                "verification": [capture.to_payload() for capture in verification_captures],
                "verification_environment": verification_environment,
                "commit_checkpoint": checkpoint.to_payload() if checkpoint else None,
                "auto_merge": AGENT_BRANCH_AUTO_MERGE,
            },
        )
        risks = [f"{failed_harness} hit {failure_reason}; fresh {alternate_harness} fallback used."]
        if uncertifiable is not None:
            risks.append(uncertifiable)
        if status == "failed":
            risks.append(
                f"Cross-provider fallback exited {alternate_capture.exit_code}; no local junior "
                "downgrade was attempted."
            )
        if alternate_attempt and alternate_attempt.open_error:
            risks.append(f"Fallback execution lease open failed: {alternate_attempt.open_error}")
        if alternate_attempt and alternate_attempt.complete_error:
            risks.append(
                f"Fallback execution lease completion failed: {alternate_attempt.complete_error}"
            )
        if checkpoint and checkpoint.error:
            risks.append(f"Worktree checkpoint failed: {checkpoint.error}")
        return PowWowTaskResult(
            task_name=task.task_name,
            role=task.role,
            status=status,
            summary=(
                f"{failed_harness} hit {failure_reason} for {task.task_name}; fresh "
                f"{alternate_harness} fallback "
                f"{'completed' if status == 'completed' else 'failed'}."
            ),
            changed_files=fallback_changed_files,
            risks=tuple(risks),
            artifacts=(
                wrapper,
                *(
                    (
                        PowWowArtifact(
                            artifact_type="worktree_commit_checkpoint",
                            schema_version="worktree_commit_checkpoint.v1",
                            task_name=task.task_name,
                            content={
                                "schema_version": "worktree_commit_checkpoint.v1",
                                "task_name": task.task_name,
                                "worktree": worktree.to_payload(),
                                **checkpoint.to_payload(),
                            },
                        ),
                    )
                    if checkpoint is not None and worktree is not None
                    else ()
                ),
            ),
        )

    def _record_fallback_transition(
        self,
        *,
        failed_attempt: ExecutionAttemptLease | None,
        replacement_attempt: ExecutionAttemptLease | None,
        failed_harness: str,
        replacement_harness: str,
        failure_reason: FrontierFallbackReason,
        supervised_result: SupervisedCommandResult | None,
    ) -> None:
        """Append the provider handoff to the failed lease's durable event stream."""

        if (
            self.coordination_command is None
            or failed_attempt is None
            or not failed_attempt.lease_id
            or replacement_attempt is None
            or not replacement_attempt.lease_id
            or supervised_result is None
        ):
            return
        payload: dict[str, object] = {
            "action": ExecutionTransition.SWITCH_TO_FALLBACK.value,
            "reason": failure_reason,
            "failed_harness": failed_harness,
            "replacement_harness": replacement_harness,
            "replacement_lease_id": replacement_attempt.lease_id,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        command = AppendExecutionEvent(
            lease_id=failed_attempt.lease_id,
            sequence=supervised_result.event_count + 1,
            occurred_at=time.time(),
            source="lifecycle",
            kind="provider_fallback.started",
            payload=payload,
            payload_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )
        try:
            self.coordination_command(command)
        except Exception:
            # The terminal lease result still records next_action. A duplicate
            # replay or transient event-write failure must not suppress the
            # bounded replacement itself.
            return

    def _audit_context_block_for(
        self,
        task: PowWowTaskSpec,
        *,
        target_project: LinkedProject,
        repo_path: Path,
    ) -> str:
        """The predecessor's partitioned audit as a prompt block, or empty.

        Empty is the honest degraded mode everywhere: no consuming phase, no
        coordination ledger, no prior audit, an unreadable one, or an audit
        commit this repository does not know are all just a cold start, which
        is exactly what every dispatch was before audits existed. Review tasks
        return empty before any lookup happens - reviewer independence caught a
        false success on 2026-08-10 precisely because the reviewer re-read the
        worktree itself, so audits flow only forward along same-tier edges and
        never into a review.
        """

        tier = audit_consumer_tier(task.planning_phase)
        if tier is None or is_review_task(task) or self.coordination_command is None:
            return ""
        try:
            found = self.coordination_command(
                LatestRepoAudit(target_project_id=target_project.id, tier=tier.value)
            )
        except Exception:  # noqa: BLE001 - a lost lookup is a cold start, not a failure
            return ""
        if not isinstance(found, CollectionResult) or not found.items:
            return ""
        raw_content = found.items[0].values.get("content")
        try:
            payload = json.loads(raw_content) if isinstance(raw_content, str) else raw_content
            if not isinstance(payload, dict):
                return ""
            audit = RepoAudit.from_payload(payload)
        except (RepoAuditError, json.JSONDecodeError):
            return ""
        if audit.target_project_id != target_project.id:
            return ""
        try:
            head_sha = run_git_command_for_output(repo_path, ("rev-parse", "HEAD")).strip()
            changed_files = tuple(
                line
                for line in run_git_command_for_output(
                    repo_path, ("diff", "--name-only", audit.commit_sha, "HEAD")
                ).splitlines()
                if line
            )
        except Exception:  # noqa: BLE001 - an unknown audit sha means the diff is undecidable
            return ""
        return render_audit_context_block(
            audit,
            head_sha=head_sha,
            changed_files=changed_files,
        )

    def _run_agent_task(
        self,
        *,
        pow_wow_id: str,
        target_project: LinkedProject,
        task: PowWowTaskSpec,
        context: PowWowExecutionContext,
        dependency_results: Sequence[PowWowTaskResult] = (),
        worktree: WorktreeAllocation,
        cleanup_worktree: bool = True,
    ) -> PowWowTaskResult:
        source_repo = target_project.expanded_path
        worktree_path = Path(worktree.worktree_path)
        worktree_context = build_assigned_worktree_context(context, worktree_path)
        slot = self._task_bench_slot(task)
        frontier = self._resolve_frontier_harness(slot)
        if isinstance(frontier, LocalHarness):
            return self._build_local_harness_refusal_result(
                task,
                target_project=target_project,
                local=frontier,
            )
        harness = frontier.value
        denial = self._authorize_spawn(task, pow_wow_id=pow_wow_id, agent_name=harness)
        if denial is not None:
            return self._build_capability_denied_result(
                task,
                target_project=target_project,
                agent_name=harness,
                denial=denial,
            )
        model = slot.model if slot else None
        # What it says, and only that. It used to double as the spawn switch,
        # which is why it sniffed the task's *name*: a task called
        # `review_next_step` got a read-only process while not being a review to
        # the revision loop, which asks `is_review_task` instead. Authority now
        # comes from the capability set, so this can go back to being one
        # question with one answer.
        is_review = is_review_task(task) or (
            task.judgment is not None and task.judgment.name == "reviewer"
        )
        posture = self._task_spawn_authority(task).posture()
        reviewed_commit_sha = (
            run_git_command_for_output(worktree_path, ("rev-parse", "HEAD")).strip()
            if is_review
            else None
        )
        if frontier is FrontierHarness.CODEX and not self._has_valid_codex_authentication():
            message = "codex authentication invalid or expired — run `codex login`"
            return PowWowTaskResult(
                task_name=task.task_name,
                role=task.role,
                status="failed",
                summary=f"{message} and retry.",
                risks=(message,),
                artifacts=(
                    PowWowArtifact(
                        artifact_type=CLI_AGENT_RUN_ARTIFACT_TYPE,
                        schema_version="cli_agent_run.v1",
                        task_name=task.task_name,
                        content={
                            "schema_version": "cli_agent_run.v1",
                            "engineering_doctrine": _build_engineering_doctrine_provenance(task),
                            "mode": self.mode,
                            "harness": harness,
                            "model": model,
                            "is_review": is_review,
                            "spawn_posture": describe_posture(posture),
                            "permitted_capabilities": list(
                                self._task_spawn_authority(task).to_names()
                            ),
                            "task": task.to_payload(),
                            "ok": False,
                            "error": message,
                            "auto_merge": AGENT_BRANCH_AUTO_MERGE,
                        },
                    ),
                ),
            )
        launch_decision = self._frontier_launch_decision(
            pow_wow_id=pow_wow_id,
            target_project=target_project,
            task=task,
            context=worktree_context,
            dependency_results=dependency_results,
            harness=frontier,
            model=model,
            source_revision=worktree.head_sha,
        )
        continuation_thread_id: str | None = None
        match launch_decision:
            case ResumeExisting(thread_id=thread_id):
                continuation_thread_id = thread_id
                prompt = build_resumed_senior_implementation_prompt(
                    task,
                    worktree_context,
                    dependency_results=dependency_results,
                    dependency_compactor=self.dependency_compactor,
                )
            case StartFreshBounded() | StartFreshIndependent():
                prompt = build_agent_task_prompt(
                    task,
                    worktree_context,
                    dependency_results=dependency_results,
                    dependency_compactor=self.dependency_compactor,
                    audit_context_block=self._audit_context_block_for(
                        task,
                        target_project=target_project,
                        repo_path=worktree_path,
                    ),
                )
        command = self._build_agent_cli_command(
            frontier,
            model,
            prompt,
            posture,
            reasoning_effort=slot.reasoning_effort if slot else None,
            continuation_thread_id=continuation_thread_id,
        )
        cleanup_error: str | None = None
        command_capture: CommandRunCapture | None = None
        supervised_result: SupervisedCommandResult | None = None
        execution_attempt = self._open_execution_attempt_lease(
            pow_wow_id=pow_wow_id,
            target_project=target_project,
            task=task,
            context=worktree_context,
            harness=harness,
            model=model,
            command=command,
            cwd=worktree_path,
            worktree=worktree,
            is_review=is_review,
            source_revision=worktree.head_sha,
            resumed_thread_id=continuation_thread_id,
        )
        verification_captures: tuple[CommandRunCapture, ...] = ()
        verification: VerificationOutcome = VerificationNotDeclared()
        verification_environment: dict[str, Any] | None = None
        checkpoint_eligible = False
        changed_files: tuple[str, ...] = ()
        diff_summary: dict[str, Any] = {}
        checkpoint: WorktreeCommitCheckpoint | None = None
        try:
            command_capture = self._build_existing_execution_attempt_capture(
                execution_attempt,
                command=command,
                cwd=worktree_path,
            )
            if command_capture is None:
                command_capture, supervised_result = self._run_frontier_command(
                    command,
                    worktree_path,
                    execution_attempt=execution_attempt,
                    harness=harness,
                    env=build_assigned_worktree_environment(worktree_context),
                    source_repo_path=source_repo,
                    base_head_sha=worktree.head_sha,
                    saga_id=context.saga_id,
                    pow_wow_id=pow_wow_id,
                    task_contract=prompt,
                )
            changed_files = list_changed_worktree_files(
                worktree_path,
                base_head_sha=worktree.head_sha if not is_review else None,
            )
            diff_summary = summarize_worktree_diff(
                worktree_path,
                base_head_sha=worktree.head_sha if not is_review else None,
            )
            fallback_reason = infer_frontier_fallback_reason(command_capture)
            if (
                command_capture.exit_code != 0
                and fallback_reason is not None
                and not (supervised_result and supervised_result.checkpoint_reason)
            ):
                fallback_result = self._execute_frontier_fallback_task(
                    pow_wow_id=pow_wow_id,
                    target_project=target_project,
                    task=task,
                    context=worktree_context,
                    dependency_results=dependency_results,
                    failed_harness=frontier,
                    failed_model=model,
                    failure_reason=fallback_reason,
                    failed_capture=command_capture,
                    failed_attempt=execution_attempt,
                    failed_supervised_result=supervised_result,
                    is_review=is_review,
                    worktree=worktree,
                    changed_files=changed_files,
                    diff_summary=diff_summary,
                )
                if fallback_result is not None:
                    return fallback_result
            declared_verification = self._select_verification_commands(target_project)
            if declared_verification and command_capture.exit_code == 0:
                gate_environment, stripped_names = verification_gate_environment(worktree_path)
                verification_environment = {"stripped": list(stripped_names)}
                verification_captures = tuple(
                    run_captured_shell_command(
                        vc,
                        worktree_path,
                        timeout_seconds=self.verification_timeout_seconds,
                        environment=gate_environment,
                    )
                    for vc in declared_verification
                )
            verification = classify_verification(declared_verification, verification_captures)
            checkpoint_eligible = not is_review and command_capture.exit_code == 0
            if checkpoint_eligible and checkpoint_permitted(verification):
                checkpoint = _commit_worktree_checkpoint_at_lifecycle_boundary(
                    worktree,
                    task_name=task.task_name,
                )
        finally:
            preserve_for_checkpoint = bool(
                supervised_result and supervised_result.preserve_worktree
            )
            if cleanup_worktree and self.cleanup_policy == "remove" and not preserve_for_checkpoint:
                cleanup_error = self._remove_worktree(source_repo, worktree_path)
            if command_capture is not None:
                self._complete_execution_attempt_lease(
                    execution_attempt,
                    capture=command_capture,
                    dirty_worktree={
                        "schema_version": "dirty_worktree.v1",
                        "source_repo_path": str(source_repo),
                        "worktree_path": str(worktree_path),
                        "head_sha": worktree.head_sha,
                        "branch_name": worktree.branch_name,
                        "changed_files": list(changed_files),
                        "diff_summary": diff_summary,
                        "commit_checkpoint": checkpoint.to_payload() if checkpoint else None,
                        "cleanup_policy": self.cleanup_policy,
                        "cleanup_requested": cleanup_worktree,
                        "cleanup_applied": (
                            cleanup_worktree
                            and self.cleanup_policy == "remove"
                            and cleanup_error is None
                        ),
                        "cleanup_deferred": not cleanup_worktree,
                        "cleanup_error": cleanup_error,
                    },
                    supervised_result=supervised_result,
                )

        if command_capture is None:
            command_capture = CommandRunCapture(
                command=shlex.join(str(part) for part in command),
                cwd=str(worktree_path),
                stdout="",
                stderr="CLI command did not produce a capture",
                exit_code=127,
            )
        uncertifiable = (
            uncertifiable_reason(verification, target_project_id=target_project.id)
            if checkpoint_eligible
            else None
        )
        exit_codes = [command_capture.exit_code]
        exit_codes.extend(capture.exit_code for capture in verification_captures)
        if checkpoint and checkpoint.error:
            exit_codes.append(1)
        if uncertifiable is not None:
            exit_codes.append(1)
        review_mutated_worktree = is_review and bool(changed_files)
        if review_mutated_worktree:
            exit_codes.append(1)
        status: PowWowTaskStatus = "completed" if all(c == 0 for c in exit_codes) else "failed"
        output_text = extract_agent_cli_output(command_capture.stdout)
        parsed_verdict = ReviewVerdict.parse(output_text) if is_review else None
        risks: list[str] = []
        if uncertifiable is not None:
            risks.append(uncertifiable)
        worktree_excerpt = _harness_failure_excerpt(command_capture, output_text)
        if worktree_excerpt is not None:
            risks.append(f"{harness} reported: {worktree_excerpt}")
        if cleanup_error:
            risks.append(f"Worktree cleanup failed: {cleanup_error}")
        if self.cleanup_policy == "preserve":
            risks.append(f"Worktree preserved for inspection: {worktree_path}")
        if execution_attempt and execution_attempt.open_error:
            risks.append(f"Execution lease open failed: {execution_attempt.open_error}")
        if execution_attempt and execution_attempt.complete_error:
            risks.append(f"Execution lease completion failed: {execution_attempt.complete_error}")
        if execution_attempt and execution_attempt.blocked_existing_active:
            risks.append(
                f"Execution lease {execution_attempt.lease_id} is already active; "
                "external process was not duplicated."
            )
        if checkpoint and checkpoint.error:
            risks.append(f"Worktree checkpoint failed: {checkpoint.error}")
        if review_mutated_worktree:
            risks.append(
                "Reviewer violated the read-only boundary; uncommitted worktree "
                "changes were preserved as failure evidence."
            )
        if supervised_result and supervised_result.checkpoint_id:
            risks.append(
                "Execution paused at durable checkpoint "
                f"{supervised_result.checkpoint_id}; worktree preserved: {worktree_path}"
            )

        run_capture = {
            "schema_version": "cli_agent_run.v1",
            "engineering_doctrine": _build_engineering_doctrine_provenance(task),
            "mode": self.mode,
            "harness": harness,
            "model": model,
            "is_review": is_review,
            "launch_decision": _launch_decision_payload(launch_decision),
            "spawn_posture": describe_posture(posture),
            "permitted_capabilities": list(self._task_spawn_authority(task).to_names()),
            "task": task.to_payload(),
            "target_project_id": target_project.id,
            "worktree": worktree.to_payload(),
            "execution_lease": execution_attempt.to_payload()
            if execution_attempt is not None
            else None,
            "streaming_supervisor": (
                {
                    "event_count": supervised_result.event_count,
                    "transcript_artifact_id": supervised_result.transcript_artifact_id,
                    "checkpoint_id": supervised_result.checkpoint_id,
                    "checkpoint_artifact_ids": list(supervised_result.checkpoint_artifact_ids),
                    "checkpoint_reason": supervised_result.checkpoint_reason,
                    "preserve_worktree": supervised_result.preserve_worktree,
                    "supervisor_error": supervised_result.supervisor_error,
                }
                if supervised_result is not None
                else None
            ),
            "command": command_capture.to_payload(),
            "output": output_text,
            "verdict": output_text if is_review else None,
            "changed_files": list(changed_files),
            "diff_summary": diff_summary,
            "verification": [capture.to_payload() for capture in verification_captures],
            "verification_environment": verification_environment,
            "commit_checkpoint": checkpoint.to_payload() if checkpoint else None,
            "cleanup_error": cleanup_error,
            "auto_merge": AGENT_BRANCH_AUTO_MERGE,
        }
        verification_output = tuple(
            f"{capture.command} -> {capture.exit_code}\n{capture.stdout}{capture.stderr}"
            for capture in verification_captures
        )
        review_artifacts: tuple[PowWowArtifact, ...] = ()
        if parsed_verdict is not None:
            reviewer_tier = (
                ReviewerTier.STAFF
                if task.judgment and task.judgment.tier is Tier.STAFF
                else ReviewerTier.SENIOR
                if task.judgment and task.judgment.tier is Tier.SENIOR
                else ReviewerTier.OPERATOR
            )
            round_match = re.search(r"_r(\d+)$", task.task_name)
            attempt_number = int(round_match.group(1)) + 1 if round_match else 1
            origin = context.review_origin
            if isinstance(origin, str):
                origin = ReviewOrigin(origin)
            review_artifacts = (
                PowWowArtifact(
                    artifact_type="review_result",
                    schema_version="review_result.v1",
                    task_name=task.task_name,
                    content={
                        "schema_version": "review_result.v1",
                        "engineering_doctrine": _build_engineering_doctrine_provenance(task),
                        "verdict": parsed_verdict.disposition.value,
                        "decision_line": parsed_verdict.decision_line,
                        "finding_severity": classify_finding_severity(
                            parsed_verdict.disposition
                        ).value,
                        "review_origin": origin.value,
                        "reviewer_tier": reviewer_tier.value,
                        "harness": harness,
                        "model": model,
                        "reasoning_effort": slot.reasoning_effort if slot else None,
                        "execution_lease_id": (
                            execution_attempt.lease_id if execution_attempt else None
                        ),
                        "task_id": execution_attempt.task_id if execution_attempt else None,
                        "reviewed_commit_sha": reviewed_commit_sha,
                        "base_sha": context.review_base_sha or worktree.head_sha,
                        "attempt_number": attempt_number,
                        "completion_status": (
                            ReviewCompletionStatus.COMPLETED.value
                            if status == "completed"
                            else ReviewCompletionStatus.FAILED.value
                        ),
                        "review_text": parsed_verdict.text,
                        "provenance_stamped_by": "pow_wow_executor",
                    },
                ),
            )
        return PowWowTaskResult(
            task_name=task.task_name,
            role=task.role,
            status=status,
            summary=(
                f"{harness} {'review' if is_review else 'run'} for {task.task_name} "
                f"in worktree {worktree_path}; exit={command_capture.exit_code}."
            ),
            changed_files=changed_files,
            verification_commands=self._select_verification_commands(target_project),
            verification_output=verification_output,
            risks=tuple(risks),
            artifacts=(
                PowWowArtifact(
                    artifact_type="worktree_allocation",
                    schema_version="worktree_allocation.v1",
                    task_name=task.task_name,
                    content={"schema_version": "worktree_allocation.v1", **worktree.to_payload()},
                ),
                PowWowArtifact(
                    artifact_type=CLI_AGENT_RUN_ARTIFACT_TYPE,
                    schema_version="cli_agent_run.v1",
                    task_name=task.task_name,
                    content=run_capture,
                ),
                *review_artifacts,
                *(
                    (
                        PowWowArtifact(
                            artifact_type="worktree_commit_checkpoint",
                            schema_version="worktree_commit_checkpoint.v1",
                            task_name=task.task_name,
                            content={
                                "schema_version": "worktree_commit_checkpoint.v1",
                                "task_name": task.task_name,
                                "worktree": worktree.to_payload(),
                                **checkpoint.to_payload(),
                            },
                        ),
                    )
                    if checkpoint is not None
                    else ()
                ),
            ),
        )

    def _run_advisory_agent_task(
        self,
        *,
        pow_wow_id: str,
        target_project: LinkedProject,
        task: PowWowTaskSpec,
        context: PowWowExecutionContext,
        dependency_results: Sequence[PowWowTaskResult] = (),
    ) -> PowWowTaskResult:
        slot = self._task_bench_slot(task)
        frontier = self._resolve_frontier_harness(slot)
        if isinstance(frontier, LocalHarness):
            return self._build_local_harness_refusal_result(
                task,
                target_project=target_project,
                local=frontier,
            )
        harness = frontier.value
        denial = self._authorize_spawn(task, pow_wow_id=pow_wow_id, agent_name=harness)
        if denial is not None:
            return self._build_capability_denied_result(
                task,
                target_project=target_project,
                agent_name=harness,
                denial=denial,
            )
        model = slot.model if slot else None
        if frontier is FrontierHarness.CODEX and not self._has_valid_codex_authentication():
            message = "codex authentication invalid or expired — run `codex login`"
            return PowWowTaskResult(
                task_name=task.task_name,
                role=task.role,
                status="failed",
                summary=f"{message} and retry.",
                risks=(message,),
                artifacts=(
                    PowWowArtifact(
                        artifact_type=CLI_AGENT_RUN_ARTIFACT_TYPE,
                        schema_version="cli_agent_run.v1",
                        task_name=task.task_name,
                        content={
                            "schema_version": "cli_agent_run.v1",
                            "engineering_doctrine": _build_engineering_doctrine_provenance(task),
                            "mode": self.mode,
                            "dispatch_kind": "advisory",
                            "harness": harness,
                            "model": model,
                            "is_review": True,
                            "task": task.to_payload(),
                            "target_project_id": target_project.id,
                            "worktree": None,
                            "ok": False,
                            "error": message,
                            "auto_merge": AGENT_BRANCH_AUTO_MERGE,
                        },
                    ),
                ),
            )

        prompt = build_agent_task_prompt(
            task,
            context,
            dependency_results=dependency_results,
            dependency_compactor=self.dependency_compactor,
            audit_context_block=self._audit_context_block_for(
                task,
                target_project=target_project,
                repo_path=target_project.expanded_path,
            ),
        )
        # Advisory work is explicitly read-only/no-worktree. The command builder
        # treats review=True as the non-mutating CLI posture: codex read-only
        # sandbox and claude without dangerous skip permissions.
        command = self._build_agent_cli_command(
            frontier,
            model,
            prompt,
            self._task_spawn_authority(task).posture(),
            reasoning_effort=slot.reasoning_effort if slot else None,
        )
        execution_attempt = self._open_execution_attempt_lease(
            pow_wow_id=pow_wow_id,
            target_project=target_project,
            task=task,
            context=context,
            harness=harness,
            model=model,
            command=command,
            cwd=target_project.expanded_path,
            worktree=None,
            is_review=True,
        )
        command_capture = self._build_existing_execution_attempt_capture(
            execution_attempt,
            command=command,
            cwd=target_project.expanded_path,
        )
        supervised_result: SupervisedCommandResult | None = None
        if command_capture is None:
            command_capture, supervised_result = self._run_frontier_command(
                command,
                target_project.expanded_path,
                execution_attempt=execution_attempt,
                harness=harness,
                env=None,
                source_repo_path=target_project.expanded_path,
                base_head_sha=None,
                saga_id=context.saga_id,
                pow_wow_id=pow_wow_id,
                task_contract=prompt,
            )
        self._complete_execution_attempt_lease(
            execution_attempt,
            capture=command_capture,
            dirty_worktree={
                "schema_version": "dirty_worktree.v1",
                "source_repo_path": str(target_project.expanded_path),
                "worktree_path": None,
                "head_sha": None,
                "changed_files": [],
                "diff_summary": {},
                "cleanup_policy": None,
                "cleanup_requested": False,
                "cleanup_applied": False,
                "cleanup_deferred": False,
                "cleanup_error": None,
            },
            supervised_result=supervised_result,
        )
        fallback_reason = infer_frontier_fallback_reason(command_capture)
        if (
            command_capture.exit_code != 0
            and fallback_reason is not None
            and not (supervised_result and supervised_result.checkpoint_reason)
        ):
            fallback_result = self._execute_frontier_fallback_task(
                pow_wow_id=pow_wow_id,
                target_project=target_project,
                task=task,
                context=context,
                dependency_results=dependency_results,
                failed_harness=frontier,
                failed_model=model,
                failure_reason=fallback_reason,
                failed_capture=command_capture,
                failed_attempt=execution_attempt,
                failed_supervised_result=supervised_result,
                is_review=True,
                worktree=None,
            )
            if fallback_result is not None:
                return fallback_result
        output_text = extract_agent_cli_output(command_capture.stdout)
        status: PowWowTaskStatus = "completed" if command_capture.exit_code == 0 else "failed"
        risks = (
            (f"{harness} advisory exited {command_capture.exit_code}",)
            if command_capture.exit_code != 0
            else ()
        )
        advisory_excerpt = _harness_failure_excerpt(command_capture, output_text)
        if advisory_excerpt is not None:
            risks += (f"{harness} reported: {advisory_excerpt}",)
        if execution_attempt and execution_attempt.open_error:
            risks += (f"Execution lease open failed: {execution_attempt.open_error}",)
        if execution_attempt and execution_attempt.complete_error:
            risks += (f"Execution lease completion failed: {execution_attempt.complete_error}",)
        if execution_attempt and execution_attempt.blocked_existing_active:
            risks += (
                f"Execution lease {execution_attempt.lease_id} is already active; "
                "external process was not duplicated.",
            )
        return PowWowTaskResult(
            task_name=task.task_name,
            role=task.role,
            status=status,
            summary=(
                f"{harness} advisory run for {task.task_name} in "
                f"{target_project.expanded_path}; exit={command_capture.exit_code}."
            ),
            risks=risks,
            artifacts=(
                PowWowArtifact(
                    artifact_type=CLI_AGENT_RUN_ARTIFACT_TYPE,
                    schema_version="cli_agent_run.v1",
                    task_name=task.task_name,
                    content={
                        "schema_version": "cli_agent_run.v1",
                        "engineering_doctrine": _build_engineering_doctrine_provenance(task),
                        "mode": self.mode,
                        "dispatch_kind": "advisory",
                        "harness": harness,
                        "model": model,
                        "is_review": True,
                        "task": task.to_payload(),
                        "target_project_id": target_project.id,
                        "worktree": None,
                        "execution_lease": execution_attempt.to_payload()
                        if execution_attempt is not None
                        else None,
                        "streaming_supervisor": (
                            {
                                "event_count": supervised_result.event_count,
                                "transcript_artifact_id": (
                                    supervised_result.transcript_artifact_id
                                ),
                                "checkpoint_id": supervised_result.checkpoint_id,
                                "checkpoint_artifact_ids": list(
                                    supervised_result.checkpoint_artifact_ids
                                ),
                                "checkpoint_reason": supervised_result.checkpoint_reason,
                                "preserve_worktree": supervised_result.preserve_worktree,
                                "supervisor_error": supervised_result.supervisor_error,
                            }
                            if supervised_result is not None
                            else None
                        ),
                        "command": command_capture.to_payload(),
                        "output": output_text,
                        "verdict": output_text,
                        "changed_files": [],
                        "diff_summary": {},
                        "verification": [],
                        "auto_merge": AGENT_BRANCH_AUTO_MERGE,
                    },
                ),
            ),
        )

    def _resolve_task_dispatch_kind(
        self,
        task: PowWowTaskSpec,
        context: PowWowExecutionContext,
    ) -> DispatchKind:
        return task.dispatch_kind or context.dispatch_kind

    def _resolve_task_tier(self, task: PowWowTaskSpec) -> Tier | None:
        return task.judgment.tier if task.judgment else None

    def _resolve_task_worktree_group(self, task: PowWowTaskSpec) -> str:
        return task.worktree_group or task.task_name

    def _checkpoint_worktree_allocation(
        self,
        *,
        target_project: LinkedProject,
        context: PowWowExecutionContext,
    ) -> WorktreeAllocation | None:
        if not context.reuse_checkpoint_worktree or not context.checkpoint_worktree_path:
            return None
        worktree_path = Path(context.checkpoint_worktree_path)
        if not worktree_path.is_dir():
            raise RuntimeError(
                f"checkpoint worktree is missing: {context.checkpoint_worktree_path}"
            )
        branch_name = run_git_command_for_output(
            worktree_path, ("branch", "--show-current")
        ).strip()
        if not branch_name:
            raise RuntimeError("checkpoint worktree must remain on its allocated branch")
        current_head = run_git_command_for_output(worktree_path, ("rev-parse", "HEAD")).strip()
        if context.reviewed_commit_sha and current_head != context.reviewed_commit_sha:
            raise RuntimeError(
                "recovery review worktree HEAD drifted from the requested commit "
                f"(expected {context.reviewed_commit_sha}, found {current_head})"
            )
        head_sha = (
            context.checkpoint_base_head_sha
            or run_git_command_for_output(worktree_path, ("rev-parse", "HEAD")).strip()
        )
        return WorktreeAllocation(
            source_repo_path=str(target_project.expanded_path),
            worktree_path=str(worktree_path),
            head_sha=head_sha,
            branch_name=branch_name,
            cleanup_policy=self.cleanup_policy,
            preserved=True,
        )

    def _apply_checkpoint_patch(
        self,
        *,
        allocation: WorktreeAllocation,
        context: PowWowExecutionContext,
    ) -> None:
        artifact_id = context.checkpoint_patch_artifact_id
        if not artifact_id or context.reuse_checkpoint_worktree or self.artifact_writer is None:
            return
        patch = self.artifact_writer.read_text(artifact_id)
        if not patch.strip():
            return
        completed = subprocess.run(
            ["git", "-C", allocation.worktree_path, "apply", "--binary", "--3way", "-"],
            input=patch,
            capture_output=True,
            text=True,
            timeout=self.git_timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "failed to restore checkpoint patch into continuation worktree: "
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )

    def _build_blocked_task_result(
        self,
        task: PowWowTaskSpec,
        *,
        target_project: LinkedProject,
        reason: str,
    ) -> PowWowTaskResult:
        artifact = PowWowArtifact(
            artifact_type="task_blocked",
            schema_version="task_blocked.v1",
            task_name=task.task_name,
            content={
                "schema_version": "task_blocked.v1",
                "mode": self.mode,
                "task": task.to_payload(),
                "target_project_id": target_project.id,
                "reason": reason,
                "auto_merge": AGENT_BRANCH_AUTO_MERGE,
            },
        )
        return PowWowTaskResult(
            task_name=task.task_name,
            role=task.role,
            status="blocked",
            summary=f"Task {task.task_name} blocked: {reason}",
            risks=(reason,),
            artifacts=(artifact,),
        )

    def _run_browser_acceptance_task(
        self,
        *,
        pow_wow_id: str,
        target_project: LinkedProject,
        task: PowWowTaskSpec,
        lease: _CodeWorktreeLease,
    ) -> PowWowTaskResult:
        profile = target_project.browser_acceptance
        if profile is None:
            return self._build_blocked_task_result(
                task,
                target_project=target_project,
                reason="linked project has no browser_acceptance profile",
            )
        if self.artifact_writer is None:
            return self._build_blocked_task_result(
                task,
                target_project=target_project,
                reason="browser acceptance requires a durable artifact writer",
            )
        session = LocalPreviewSession(
            command_template=profile.preview_command,
            cwd=Path(lease.allocation.worktree_path),
            target_url_template=profile.target_url_template,
            readiness_path=profile.readiness_path,
            startup_timeout_seconds=profile.startup_timeout_seconds,
        )
        try:
            with session:
                request = BrowserAcceptanceRequest(
                    target_url=session.target_url,
                    viewports=profile.viewports,
                    required_paths=profile.required_paths,
                    allowed_hosts=profile.allowed_hosts,
                    required_selectors=profile.required_selectors,
                    bounded_selectors=profile.bounded_selectors,
                    timeout_seconds=profile.capture_timeout_seconds,
                )
                browser_artifact_writer = cast(
                    BrowserAcceptanceArtifactWriter, self.artifact_writer
                )
                run = BrowserAcceptanceRunner(browser_artifact_writer).run(
                    request,
                    workflow_id=pow_wow_id,
                )
            preview = session.build_process_evidence()
        except PreviewStartError as exc:
            content = {
                "schema_version": "browser_acceptance_task_result.v1",
                "status": "BLOCKED",
                "summary": str(exc),
                "preview": exc.evidence.model_dump(mode="json"),
                "request_artifact_id": None,
                "evidence_artifact_id": None,
                "captures": [],
            }
            return PowWowTaskResult(
                task_name=task.task_name,
                role=task.role,
                status="blocked",
                summary=str(exc),
                risks=(str(exc),),
                artifacts=(
                    PowWowArtifact(
                        artifact_type="browser_acceptance",
                        schema_version="browser_acceptance_task_result.v1",
                        task_name=task.task_name,
                        content=content,
                    ),
                ),
            )
        except Exception as exc:  # noqa: BLE001 - host failure must be durable and blocking
            session.close()
            preview = session.build_process_evidence()
            summary = f"Browser acceptance host operation failed: {type(exc).__name__}: {exc}"
            return PowWowTaskResult(
                task_name=task.task_name,
                role=task.role,
                status="failed",
                summary=summary,
                risks=(summary,),
                artifacts=(
                    PowWowArtifact(
                        artifact_type="browser_acceptance",
                        schema_version="browser_acceptance_task_result.v1",
                        task_name=task.task_name,
                        content={
                            "schema_version": "browser_acceptance_task_result.v1",
                            "status": "FAILED",
                            "summary": summary,
                            "preview": preview.model_dump(mode="json"),
                            "request_artifact_id": None,
                            "evidence_artifact_id": None,
                            "captures": [],
                        },
                    ),
                ),
            )
        evidence = run.evidence
        cleanup_passed = preview.direct_child_reaped and preview.process_group_reaped
        passed = evidence.status is BrowserAcceptanceStatus.PASSED and cleanup_passed
        if passed:
            status: PowWowTaskStatus = "completed"
        elif evidence.status in {
            BrowserAcceptanceStatus.BLOCKED,
            BrowserAcceptanceStatus.CANCELLED,
        }:
            status = "blocked"
        else:
            status = "failed"
        risk_items: list[str] = []
        for capture in evidence.captures:
            risk_items.extend(f"console: {value}" for value in capture.console_errors)
            risk_items.extend(f"page exception: {value}" for value in capture.page_errors)
            risk_items.extend(f"network: {value}" for value in capture.failed_requests)
            risk_items.extend(f"assertion: {value}" for value in capture.assertion_failures)
            if capture.horizontal_overflow:
                risk_items.append("horizontal overflow")
            if capture.screenshot_artifact_id is None:
                risk_items.append("screenshot missing")
            if capture.trace_artifact_id is None:
                risk_items.append("trace missing")
            if capture.cancelled:
                risk_items.append("capture cancelled")
        risks = tuple(risk_items)
        if not cleanup_passed:
            risks = (*risks, "local preview process group was not fully reaped")
        content = {
            "schema_version": "browser_acceptance_task_result.v1",
            "status": evidence.status.value if cleanup_passed else "FAILED",
            "summary": evidence.summary,
            "preview": preview.model_dump(mode="json"),
            "request_artifact_id": run.request_artifact_id,
            "evidence_artifact_id": run.evidence_artifact_id,
            "captures": [capture.model_dump(mode="json") for capture in evidence.captures],
        }
        return PowWowTaskResult(
            task_name=task.task_name,
            role=task.role,
            status=status,
            summary=evidence.summary,
            risks=risks,
            artifacts=(
                PowWowArtifact(
                    artifact_type="browser_acceptance",
                    schema_version="browser_acceptance_task_result.v1",
                    task_name=task.task_name,
                    content=content,
                ),
            ),
        )

    def _run_scheduled_task(
        self,
        *,
        pow_wow_id: str,
        target_project: LinkedProject,
        task: PowWowTaskSpec,
        context: PowWowExecutionContext,
        dependency_results: Sequence[PowWowTaskResult],
        code_worktrees: dict[str, _CodeWorktreeLease],
        code_worktree_lock: threading.Lock,
    ) -> PowWowTaskResult:
        if task.purpose is TaskPurpose.RECOVERY_REVISION:
            group = self._resolve_task_worktree_group(task)
            with code_worktree_lock:
                lease = code_worktrees.get(group)
                if lease is None:
                    allocation = self._checkpoint_worktree_allocation(
                        target_project=target_project,
                        context=context,
                    )
                    if allocation is None:
                        raise RuntimeError(
                            "recovery review requires an exact retained-commit worktree"
                        )
                    lease = _CodeWorktreeLease(group=group, allocation=allocation)
                    code_worktrees[group] = lease
            checkpoint = _commit_worktree_checkpoint_at_lifecycle_boundary(
                lease.allocation,
                task_name=task.task_name,
            )
            if checkpoint.error or not checkpoint.changed_from_base or not checkpoint.commit_sha:
                return self._build_blocked_task_result(
                    task,
                    target_project=target_project,
                    reason=(
                        checkpoint.error or "retained commit has no change from the recorded base"
                    ),
                )
            changed_files = tuple(checkpoint.checkpointed_files)
            return PowWowTaskResult(
                task_name=task.task_name,
                role=task.role,
                status="completed",
                summary=(
                    "Recovery anchor validated retained commit "
                    f"{checkpoint.commit_sha} against base {checkpoint.base_head_sha}; "
                    "no implementation model was started."
                ),
                changed_files=changed_files,
                artifacts=(
                    PowWowArtifact(
                        artifact_type="recovery_review_anchor",
                        schema_version="recovery_review_anchor.v1",
                        task_name=task.task_name,
                        content={
                            "schema_version": "recovery_review_anchor.v1",
                            "reviewed_commit_sha": checkpoint.commit_sha,
                            "base_sha": checkpoint.base_head_sha,
                            "branch": checkpoint.branch_name,
                            "changed_files": list(changed_files),
                            "implementation_model_started": False,
                        },
                    ),
                    PowWowArtifact(
                        artifact_type="worktree_commit_checkpoint",
                        schema_version="worktree_commit_checkpoint.v1",
                        task_name=task.task_name,
                        content={
                            "schema_version": "worktree_commit_checkpoint.v1",
                            "task_name": task.task_name,
                            "worktree": lease.allocation.to_payload(),
                            **checkpoint.to_payload(),
                        },
                    ),
                ),
            )
        if task.purpose is TaskPurpose.BROWSER_ACCEPTANCE:
            group = self._resolve_task_worktree_group(task)
            with code_worktree_lock:
                lease = code_worktrees.get(group)
            if lease is None:
                return self._build_blocked_task_result(
                    task,
                    target_project=target_project,
                    reason="browser acceptance has no completed implementation worktree",
                )
            return self._run_browser_acceptance_task(
                pow_wow_id=pow_wow_id,
                target_project=target_project,
                task=task,
                lease=lease,
            )
        local = self._local_harness_for(task)
        if local is not None:
            # Asked before the delegate is checked, so a local task with no
            # delegate is refused here by name rather than falling through to the
            # frontier paths below and being launched as claude. Falling through
            # is what produced `claude --model gemma4`.
            if self.delegate_fn is None:
                return self._build_local_harness_refusal_result(
                    task,
                    target_project=target_project,
                    local=local,
                )
            return self._run_delegate_task(
                pow_wow_id=pow_wow_id,
                target_project=target_project,
                task=task,
                context=context,
                dependency_results=dependency_results,
            )
        if not is_agent_task(task):
            return self._build_non_implementation_task_result(task, target_project)
        if self._resolve_task_dispatch_kind(task, context) == "advisory":
            return self._run_advisory_agent_task(
                pow_wow_id=pow_wow_id,
                target_project=target_project,
                task=task,
                context=context,
                dependency_results=dependency_results,
            )

        group = self._resolve_task_worktree_group(task)
        with code_worktree_lock:
            lease = code_worktrees.get(group)
            if lease is None:
                allocation = self._checkpoint_worktree_allocation(
                    target_project=target_project,
                    context=context,
                )
                if allocation is None:
                    allocation = self._allocate_worktree(
                        source_repo=target_project.expanded_path,
                        pow_wow_id=pow_wow_id,
                        task_name=group,
                        base_commit_sha=context.base_commit_sha,
                    )
                    self._apply_checkpoint_patch(
                        allocation=allocation,
                        context=context,
                    )
                lease = _CodeWorktreeLease(group=group, allocation=allocation)
                code_worktrees[group] = lease
        return self._run_agent_task(
            pow_wow_id=pow_wow_id,
            target_project=target_project,
            task=task,
            context=context,
            dependency_results=dependency_results,
            worktree=lease.allocation,
            cleanup_worktree=False,
        )

    def _run_dependency_scheduled_tasks(
        self,
        *,
        pow_wow_id: str,
        target_project: LinkedProject,
        tasks: Sequence[PowWowTaskSpec],
        context: PowWowExecutionContext,
        code_worktrees: dict[str, _CodeWorktreeLease],
    ) -> tuple[PowWowTaskResult, ...]:
        names = [task.task_name for task in tasks]
        duplicate_names = sorted({name for name in names if names.count(name) > 1})
        if duplicate_names:
            return tuple(
                self._build_blocked_task_result(
                    task,
                    target_project=target_project,
                    reason=f"duplicate task names are invalid: {', '.join(duplicate_names)}",
                )
                for task in tasks
            )

        task_by_name = {task.task_name: task for task in tasks}
        results: dict[str, PowWowTaskResult] = {}
        pending: list[PowWowTaskSpec] = list(tasks)
        running: dict[
            concurrent.futures.Future[PowWowTaskResult],
            tuple[PowWowTaskSpec, Tier | None, str | None],
        ] = {}
        active_by_tier = {tier: 0 for tier in Tier}
        active_code_groups: set[str] = set()
        code_worktree_lock = threading.Lock()
        tier_capacities = {tier: max(1, resolve_bench(tier, self.bench).capacity) for tier in Tier}
        max_workers = max(1, sum(tier_capacities.values()))

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            while pending or running:
                started = False
                for task in list(pending):
                    missing = [dep for dep in task.blocked_by if dep not in task_by_name]
                    if missing:
                        results[task.task_name] = self._build_blocked_task_result(
                            task,
                            target_project=target_project,
                            reason=f"missing dependencies: {', '.join(missing)}",
                        )
                        pending.remove(task)
                        started = True
                        continue

                    resolved_deps = [results[dep] for dep in task.blocked_by if dep in results]
                    if len(resolved_deps) < len(task.blocked_by):
                        continue
                    failed_deps = [dep for dep in resolved_deps if dep.status != "completed"]
                    if failed_deps:
                        # Name the failing layer, not just the graph. "did not
                        # complete" alone told an operator the schedule broke
                        # when what actually broke was inside a dependency;
                        # carrying each dependency's own summary is what lets
                        # the settled row say why (LyricPlayer m3's empty
                        # checkout surfaced only as this line's vagueness).
                        detail = "; ".join(
                            f"{dep.task_name} ({dep.status}): {dep.summary}".strip()
                            for dep in failed_deps
                        )
                        results[task.task_name] = self._build_blocked_task_result(
                            task,
                            target_project=target_project,
                            reason=f"dependencies did not complete: {detail}",
                        )
                        pending.remove(task)
                        started = True
                        continue

                    tier = self._resolve_task_tier(task)
                    if tier is not None and active_by_tier[tier] >= tier_capacities[tier]:
                        continue
                    code_group = (
                        self._resolve_task_worktree_group(task)
                        if is_agent_task(task)
                        and self._local_harness_for(task) is None
                        and self._resolve_task_dispatch_kind(task, context) == "code"
                        else None
                    )
                    if code_group is not None and code_group in active_code_groups:
                        continue

                    pending.remove(task)
                    if tier is not None:
                        active_by_tier[tier] += 1
                    if code_group is not None:
                        active_code_groups.add(code_group)
                    emit_progress(
                        f"starting {task.role} turn: {task.task_name}",
                        phase="task_started",
                        pow_wow_id=pow_wow_id,
                        task_name=task.task_name,
                        role=task.role,
                        tier=tier.value if tier is not None else None,
                    )
                    task_context = contextvars.copy_context()
                    future = pool.submit(
                        task_context.run,
                        self._run_scheduled_task,
                        pow_wow_id=pow_wow_id,
                        target_project=target_project,
                        task=task,
                        context=context,
                        dependency_results=tuple(resolved_deps),
                        code_worktrees=code_worktrees,
                        code_worktree_lock=code_worktree_lock,
                    )
                    running[future] = (task, tier, code_group)
                    started = True

                if not running:
                    if pending and not started:
                        for task in pending:
                            results[task.task_name] = self._build_blocked_task_result(
                                task,
                                target_project=target_project,
                                reason="dependency cycle or unsatisfied dependencies",
                            )
                        pending.clear()
                    continue

                done, _ = concurrent.futures.wait(
                    running,
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    task, tier, code_group = running.pop(future)
                    if tier is not None:
                        active_by_tier[tier] -= 1
                    if code_group is not None:
                        active_code_groups.remove(code_group)
                    try:
                        task_result = future.result()
                        task_result = persist_planning_evidence(
                            pow_wow_id=pow_wow_id,
                            task=task,
                            result=task_result,
                            context=context,
                            coordination_command=self.coordination_command,
                        )
                        # Beside the evidence, and behind the same barrier: the
                        # audit must be durable before any same-tier successor
                        # builds a prompt that would go looking for it. The sha
                        # is the host's fact - the source repo the advisory
                        # reading actually ran in - not the agent's claim.
                        task_result = persist_repo_audit(
                            pow_wow_id=pow_wow_id,
                            task=task,
                            result=task_result,
                            context=context,
                            resolve_head_sha=lambda: run_git_command_for_output(
                                target_project.expanded_path, ("rev-parse", "HEAD")
                            ),
                            coordination_command=self.coordination_command,
                        )
                        results[task.task_name] = task_result
                    except Exception as exc:  # noqa: BLE001 - task failure, not reactor failure
                        results[task.task_name] = PowWowTaskResult(
                            task_name=task.task_name,
                            role=task.role,
                            status="failed",
                            summary=(f"Task {task.task_name} crashed: {type(exc).__name__}: {exc}"),
                            risks=(f"{type(exc).__name__}: {exc}",),
                        )
                    # The result's own words, not just its status. `finished
                    # failed` and `finished completed` were the only two lines
                    # this could produce, so the reason a junior task failed -
                    # `401 Not logged in`, in the incident this fixes - reached
                    # nothing but `agent_execution_leases.result_json`.
                    result = results[task.task_name]
                    emit_progress(
                        (
                            f"{task.role} turn {task.task_name} finished "
                            f"{result.status}: {result.summary}"
                        ),
                        phase="task_completed",
                        pow_wow_id=pow_wow_id,
                        task_name=task.task_name,
                        role=task.role,
                        tier=tier.value if tier is not None else None,
                        status=result.status,
                        risks=result.risks,
                    )
        return tuple(results[task.task_name] for task in tasks)

    def _find_blocked_review(
        self,
        tasks: Sequence[PowWowTaskSpec],
        results: Sequence[PowWowTaskResult],
        context: PowWowExecutionContext,
    ) -> tuple[PowWowTaskSpec, PowWowTaskResult, str, ReviewDisposition] | None:
        """Return the code-kind review task whose verdict blocks or escalates."""
        results_by_name = {result.task_name: result for result in results}
        for task in tasks:
            if not is_agent_task(task) or self._local_harness_for(task) is not None:
                continue
            if not is_review_task(task):
                continue
            if self._resolve_task_dispatch_kind(task, context) != "code":
                continue
            result = results_by_name.get(task.task_name)
            if result is None or result.status != "completed":
                continue
            verdict = extract_review_verdict_text(result)
            if not verdict:
                continue
            disposition = review_verdict_disposition(verdict)
            if disposition.requests_changes or disposition is ReviewDisposition.ESCALATE:
                return task, result, verdict, disposition
        return None

    def _find_review_implementer(
        self,
        review_task: PowWowTaskSpec,
        tasks: Sequence[PowWowTaskSpec],
        context: PowWowExecutionContext,
    ) -> PowWowTaskSpec | None:
        task_by_name = {task.task_name: task for task in tasks}
        candidates = [task_by_name.get(name) for name in review_task.blocked_by]
        candidates.extend(
            task
            for task in tasks
            if self._resolve_task_worktree_group(task)
            == self._resolve_task_worktree_group(review_task)
        )
        for task in candidates:
            if task is None or not is_agent_task(task) or self._local_harness_for(task) is not None:
                continue
            if is_review_task(task):
                continue
            if self._resolve_task_dispatch_kind(task, context) == "code":
                return task
        return None

    def _classify_review_convergence(
        self,
        previous_verdict: str,
        verdict: str,
        round_number: int,
    ) -> tuple[str, str]:
        """Judge whether another revision round is worth its cost.

        Returns (classification, source): classification is progress | circling
        | escalate. Deterministic checks run first; the junior delegate breaks
        the remaining ties. Unavailable or unparseable classification defaults
        to progress because the hard round cap already bounds the loop.
        """
        normalize = lambda text: " ".join(text.split()).casefold()  # noqa: E731
        if normalize(previous_verdict) == normalize(verdict):
            return "circling", "identical_verdicts"
        if self.delegate_fn is None:
            return "progress", "no_classifier"
        slot = resolve_bench(Tier.JUNIOR, self.bench)
        prompt = (
            "Two consecutive code-review verdicts for the same change follow. "
            "Answer with exactly one word on the first line: PROGRESS if the "
            "newer verdict raises new substantive correctness or safety "
            "findings or acknowledges fixes; CIRCLING if it repeats earlier "
            "findings or has descended into style, naming, or formatting "
            "preferences; ESCALATE if reviewer and implementer fundamentally "
            "disagree on the approach.\n\n"
            f"--- Verdict round {round_number - 1} ---\n{previous_verdict[:2000]}\n\n"
            f"--- Verdict round {round_number} ---\n{verdict[:2000]}"
        )
        try:
            payload = dict(
                self.delegate_fn(
                    prompt=prompt,
                    task_name=f"review_convergence_r{round_number}",
                    role="review_convergence_classifier",
                    tier=Tier.JUNIOR.value,
                    model=slot.model,
                    model_params={"cache_prompt": False},
                )
            )
        except Exception as exc:  # noqa: BLE001 - classifier failure must not fail the loop
            return "progress", f"classifier_error: {type(exc).__name__}: {exc}"
        output = str(payload.get("output") or "")
        for line in output.splitlines():
            token = line.strip().strip(".,:;!").casefold()
            if not token:
                continue
            if token in {"progress", "circling", "escalate"}:
                return token, "junior_delegate"
            break
        return "progress", "unparseable_classification"

    @staticmethod
    def _build_typed_review_artifact(result: PowWowTaskResult) -> PowWowArtifact:
        reviews = [
            artifact
            for artifact in result.artifacts
            if artifact.artifact_type == "review_result"
            and artifact.schema_version == "review_result.v1"
        ]
        if len(reviews) != 1:
            raise ValueError("blocking review must contain exactly one typed review_result.v1")
        return reviews[0]

    def _persist_revision_artifact(
        self,
        *,
        pow_wow_id: str,
        artifact: PowWowArtifact,
        task_id: str | None,
    ) -> PowWowArtifact:
        """Persist review-boundary evidence before starting the next model.

        When the executor is used without a coordination transport, normal
        end-of-run persistence resolves the pending reference instead.
        """

        if artifact.persisted_artifact_id is not None or self.coordination_command is None:
            return artifact
        submitted = self.coordination_command(
            SubmitArtifact(
                pow_wow_id=pow_wow_id,
                artifact_type=artifact.artifact_type,
                content=json.dumps(artifact.to_payload(), indent=2, sort_keys=True),
                schema_version=artifact.schema_version,
                task_id=task_id,
            )
        )
        if not isinstance(submitted, AcknowledgementResult):
            raise RuntimeError("bounded revision artifact persistence returned malformed evidence")
        artifact_id = submitted.payload.values.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise RuntimeError("bounded revision artifact persistence returned no artifact_id")
        return replace(artifact, persisted_artifact_id=artifact_id)

    def _record_bounded_revision_context(
        self,
        *,
        pow_wow_id: str,
        target_project: LinkedProject,
        context: PowWowExecutionContext,
        implementer_task: PowWowTaskSpec,
        review_result: PowWowTaskResult,
        lease: _CodeWorktreeLease,
    ) -> PowWowTaskResult:
        """Durably bind a BLOCK to its exact revision inputs before revision."""

        original_review_artifact = self._build_typed_review_artifact(review_result)
        review_artifact = original_review_artifact
        review_task_id = review_artifact.content.get("task_id")
        review_artifact = self._persist_revision_artifact(
            pow_wow_id=pow_wow_id,
            artifact=review_artifact,
            task_id=str(review_task_id) if review_task_id else None,
        )
        envelope = build_bounded_revision_context_from_review(
            review_result=review_artifact.content,
            review_task_name=review_result.task_name,
            review_artifact_id=review_artifact.persisted_artifact_id,
            retained_branch=(context.recovery_retained_branch or lease.allocation.branch_name),
            retained_worktree_path=lease.allocation.worktree_path,
            original_task_name=implementer_task.task_name,
            original_task_contract=(
                context.recovery_original_task_contract or implementer_task.description
            ),
            permission_envelope=(
                context.recovery_permission_envelope
                or "Revise only after staff BLOCK; inherit original permissions; no widening."
            ),
            verification_commands=self._select_verification_commands(target_project),
        )
        bounded_artifact = PowWowArtifact(
            artifact_type="bounded_revision_context",
            schema_version="bounded_revision_context.v1",
            task_name=review_result.task_name,
            content=envelope.to_payload(),
        )
        bounded_artifact = self._persist_revision_artifact(
            pow_wow_id=pow_wow_id,
            artifact=bounded_artifact,
            task_id=str(review_task_id) if review_task_id else None,
        )
        reach_lifecycle_transition(
            LifecycleTransitionPoint.AFTER_REVIEW_BLOCK_RECORDED,
            pow_wow_id=pow_wow_id,
            review_task_name=review_result.task_name,
            review_artifact_id=review_artifact.persisted_artifact_id,
            bounded_revision_artifact_id=bounded_artifact.persisted_artifact_id,
            retained_worktree_path=lease.allocation.worktree_path,
        )
        artifacts = tuple(
            review_artifact if artifact is original_review_artifact else artifact
            for artifact in review_result.artifacts
        )
        return replace(
            review_result,
            artifacts=(*artifacts, bounded_artifact),
        )

    @staticmethod
    def _build_revision_description(
        review_result: PowWowTaskResult,
        *,
        round_number: int,
    ) -> str:
        """Resolve and verify the sole model-facing revision envelope."""

        review_artifact = CliPowWowExecutor._build_typed_review_artifact(review_result)
        bounded = [
            artifact
            for artifact in review_result.artifacts
            if artifact.artifact_type == "bounded_revision_context"
            and artifact.schema_version == "bounded_revision_context.v1"
        ]
        if len(bounded) != 1:
            raise ValueError("revision requires exactly one bounded_revision_context.v1")
        content = bounded[0].content
        reviewer_output = content.get("reviewer_output")
        target = content.get("target")
        scope = content.get("revision_scope")
        verification = content.get("verification")
        if not isinstance(reviewer_output, Mapping):
            raise ValueError("bounded revision context has malformed reviewer_output")
        if not isinstance(target, Mapping):
            raise ValueError("bounded revision context has malformed target")
        if not isinstance(scope, Mapping):
            raise ValueError("bounded revision context has malformed revision_scope")
        if not isinstance(verification, Mapping):
            raise ValueError("bounded revision context has malformed verification")
        review_text = review_artifact.content.get("review_text")
        if not isinstance(review_text, str) or not review_text:
            raise ValueError("bounded revision context cannot resolve complete review text")
        digest = hashlib.sha256(review_text.encode("utf-8")).hexdigest()
        if digest != reviewer_output.get("review_text_sha256"):
            raise ValueError("bounded revision reviewer output integrity check failed")
        commands = verification.get("commands")
        if not isinstance(commands, list) or not all(
            isinstance(command, str) and command for command in commands
        ):
            raise ValueError("bounded revision verification commands are malformed")
        return (
            f"Staff requested changes. Execute bounded revision round {round_number} "
            "using only the typed revision envelope below. Do not widen it.\n\n"
            f"Base commit: {target.get('base_commit_sha')}\n"
            f"Blocked commit: {target.get('blocked_commit_sha')}\n"
            f"Retained branch: {target.get('retained_branch')}\n"
            f"Retained worktree: {target.get('retained_worktree_path')}\n"
            f"Original task: {scope.get('original_task_name')}\n"
            f"Original contract: {scope.get('original_task_contract')}\n"
            f"Permission envelope: {scope.get('permission_envelope')}\n"
            f"Allowed change: {scope.get('allowed_change')}\n"
            f"Forbidden change: {scope.get('forbidden_change')}\n"
            "Verification to rerun:\n"
            + ("\n".join(f"- {command}" for command in commands) or "- none recorded")
            + "\n\nComplete integrity-verified reviewer output:\n"
            + review_text
        )

    def _run_review_revision_rounds(
        self,
        *,
        pow_wow_id: str,
        target_project: LinkedProject,
        tasks: Sequence[PowWowTaskSpec],
        context: PowWowExecutionContext,
        task_results: tuple[PowWowTaskResult, ...],
        code_worktrees: dict[str, _CodeWorktreeLease],
    ) -> tuple[PowWowTaskResult, ...]:
        """Bounded senior<->staff negotiation after a blocking review verdict.

        The implementer revises in the same worktree with the review verbatim,
        the reviewer re-reviews the new diff, and the loop ends on APPROVE, the
        hard round cap, or a convergence classifier calling the exchange
        circling or a fundamental disagreement. An unresolved block fails the
        run visibly instead of completing with a buried objection.

        An ESCALATE verdict is a request for the operator, not for another
        revision round: it skips (or exits) the negotiation loop and is
        surfaced as a REVIEW_ESCALATION approval request carrying the review
        text.
        """
        blocked = self._find_blocked_review(tasks, task_results, context)
        if blocked is None:
            return task_results
        review_task, blocked_review_result, verdict, disposition = blocked
        if disposition is ReviewDisposition.ESCALATE:
            return (
                *task_results,
                self._escalate_review_to_operator(
                    pow_wow_id=pow_wow_id,
                    context=context,
                    review_task=review_task,
                    review_result=blocked_review_result,
                    verdict=verdict,
                    rounds_run=0,
                ),
            )
        if self.max_review_rounds < 1:
            return task_results
        implementer_task = self._find_review_implementer(review_task, tasks, context)
        lease = code_worktrees.get(self._resolve_task_worktree_group(review_task))
        browser_task = next(
            (
                task
                for task in tasks
                if task.purpose is TaskPurpose.BROWSER_ACCEPTANCE
                and self._resolve_task_worktree_group(task)
                == self._resolve_task_worktree_group(review_task)
            ),
            None,
        )
        results = list(task_results)
        if implementer_task is None or lease is None:
            results.append(
                self._build_unresolved_review_result(
                    review_task,
                    rounds_run=0,
                    reason=(
                        "review verdict requested changes but no implementer "
                        "task or live worktree was available for a revision"
                    ),
                )
            )
            return tuple(results)

        try:
            recorded_review = self._record_bounded_revision_context(
                pow_wow_id=pow_wow_id,
                target_project=target_project,
                context=context,
                implementer_task=implementer_task,
                review_result=blocked_review_result,
                lease=lease,
            )
        except (RuntimeError, ValueError) as exc:
            results.append(
                self._build_unresolved_review_result(
                    review_task,
                    rounds_run=0,
                    reason=f"bounded revision context could not be persisted: {exc}",
                )
            )
            return tuple(results)
        results = [
            recorded_review if result is blocked_review_result else result for result in results
        ]

        previous_verdict = verdict
        revision_context_result = recorded_review
        converged = False
        rounds_run = 0
        stop_reason = f"round cap of {self.max_review_rounds} reached"
        for round_number in range(1, self.max_review_rounds + 1):
            rounds_run = round_number
            emit_progress(
                f"staff BLOCK triggered senior revision round {round_number}",
                phase="review_revision_started",
                pow_wow_id=pow_wow_id,
                review_task=review_task.task_name,
                round=round_number,
            )
            reach_lifecycle_transition(
                LifecycleTransitionPoint.AFTER_REVISION_STARTED,
                pow_wow_id=pow_wow_id,
                review_task_name=review_task.task_name,
                implementer_task_name=implementer_task.task_name,
                round=round_number,
                retained_worktree_path=lease.allocation.worktree_path,
            )
            try:
                revision_description = self._build_revision_description(
                    revision_context_result,
                    round_number=round_number,
                )
            except ValueError as exc:
                results.append(
                    self._build_unresolved_review_result(
                        review_task,
                        rounds_run=rounds_run - 1,
                        reason=f"bounded revision context validation failed: {exc}",
                    )
                )
                return tuple(results)
            revision_task = replace(
                implementer_task,
                task_name=f"{implementer_task.task_name}_revision_r{round_number}",
                description=revision_description,
                blocked_by=(),
                success_criteria=("Every reviewer finding is addressed or explicitly rebutted.",),
            )
            revision_result = self._run_agent_task(
                pow_wow_id=pow_wow_id,
                target_project=target_project,
                task=revision_task,
                context=context,
                worktree=lease.allocation,
                cleanup_worktree=False,
            )
            results.append(revision_result)
            if revision_result.status != "completed":
                stop_reason = f"revision round {round_number} failed"
                break

            re_review_dependencies: tuple[PowWowTaskResult, ...] = ()
            if browser_task is not None:
                repeated_browser_task = replace(
                    browser_task,
                    task_name=f"{browser_task.task_name}_r{round_number}",
                    blocked_by=(),
                )
                browser_result = self._run_browser_acceptance_task(
                    pow_wow_id=pow_wow_id,
                    target_project=target_project,
                    task=repeated_browser_task,
                    lease=lease,
                )
                results.append(browser_result)
                if browser_result.status != "completed":
                    stop_reason = f"browser acceptance after revision round {round_number} failed"
                    break
                re_review_dependencies = (browser_result,)

            re_review_task = replace(
                review_task,
                task_name=f"{review_task.task_name}_r{round_number}",
                description=(
                    f"Re-review the updated diff in this worktree "
                    f"(revision round {round_number}). Your previous findings:\n"
                    f"{previous_verdict}\n\n"
                    "Start your response with APPROVE or BLOCK on the first "
                    "line, then justify. APPROVE only if the findings are "
                    "addressed and the change has sufficient guardrails."
                ),
                blocked_by=(),
            )
            re_review_result = self._run_agent_task(
                pow_wow_id=pow_wow_id,
                target_project=target_project,
                task=re_review_task,
                context=context,
                dependency_results=re_review_dependencies,
                worktree=lease.allocation,
                cleanup_worktree=False,
            )
            emit_progress(
                (f"staff re-review round {round_number} finished {re_review_result.status}"),
                phase="review_revision_completed",
                pow_wow_id=pow_wow_id,
                review_task=re_review_task.task_name,
                round=round_number,
                status=re_review_result.status,
            )
            if re_review_result.status != "completed":
                results.append(re_review_result)
                stop_reason = f"re-review round {round_number} failed"
                break
            new_verdict = extract_review_verdict_text(re_review_result) or ""
            new_disposition = (
                review_verdict_disposition(new_verdict)
                if new_verdict
                else ReviewDisposition.UNCLASSIFIED
            )
            if new_disposition is ReviewDisposition.ESCALATE:
                # Not convergence: the reviewer asked for the operator, and
                # recording it as converged would bury the escalation behind a
                # generic merge-gate failure.
                results.append(re_review_result)
                results.append(
                    self._escalate_review_to_operator(
                        pow_wow_id=pow_wow_id,
                        context=context,
                        review_task=review_task,
                        review_result=re_review_result,
                        verdict=new_verdict,
                        rounds_run=round_number,
                    )
                )
                return tuple(results)
            if not new_verdict or not new_disposition.requests_changes:
                results.append(re_review_result)
                converged = True
                break
            try:
                re_review_result = self._record_bounded_revision_context(
                    pow_wow_id=pow_wow_id,
                    target_project=target_project,
                    context=context,
                    implementer_task=implementer_task,
                    review_result=re_review_result,
                    lease=lease,
                )
            except (RuntimeError, ValueError) as exc:
                results.append(re_review_result)
                stop_reason = f"bounded revision context could not be persisted: {exc}"
                break
            classification, classifier_source = self._classify_review_convergence(
                previous_verdict, new_verdict, round_number
            )
            convergence_artifact = PowWowArtifact(
                artifact_type="review_convergence",
                schema_version="review_convergence.v1",
                task_name=re_review_result.task_name,
                content={
                    "schema_version": "review_convergence.v1",
                    "round": round_number,
                    "classification": classification,
                    "classifier_source": classifier_source,
                    "previous_verdict_excerpt": previous_verdict[:1000],
                    "verdict_excerpt": new_verdict[:1000],
                },
            )
            results.append(
                replace(
                    re_review_result,
                    artifacts=(*re_review_result.artifacts, convergence_artifact),
                )
            )
            if classification != "progress":
                stop_reason = f"convergence classifier judged the exchange {classification}"
                break
            previous_verdict = new_verdict
            revision_context_result = re_review_result

        if not converged:
            results.append(
                self._build_unresolved_review_result(
                    review_task,
                    rounds_run=rounds_run,
                    reason=stop_reason,
                )
            )
        return tuple(results)

    def _escalate_review_to_operator(
        self,
        *,
        pow_wow_id: str,
        context: PowWowExecutionContext,
        review_task: PowWowTaskSpec,
        review_result: PowWowTaskResult,
        verdict: str,
        rounds_run: int,
    ) -> PowWowTaskResult:
        """Surface an ESCALATE verdict as an operator decision, then fail closed.

        The run still fails - nothing merges on an escalated review - but the
        failure names the operator route instead of imitating an unparseable
        verdict, and the review text travels on a REVIEW_ESCALATION approval
        request so the decision can be made without exhuming the transcript.
        A submission failure is recorded on the result rather than raised: the
        escalation must still fail the run closed even when the ledger is down.
        """

        review_text = verdict
        review_task_id: str | None = None
        for artifact in review_result.artifacts:
            if (
                artifact.artifact_type != "review_result"
                or artifact.schema_version != "review_result.v1"
            ):
                continue
            text = artifact.content.get("review_text")
            if isinstance(text, str) and text:
                review_text = text
            raw_task_id = artifact.content.get("task_id")
            review_task_id = str(raw_task_id) if raw_task_id else None
            break
        approval_id: str | None = None
        submission_error: str | None = None
        if self.coordination_command is not None:
            try:
                submitted = self.coordination_command(
                    SubmitApprovalRequest(
                        saga_id=context.saga_id,
                        request_type=ApprovalRequestType.REVIEW_ESCALATION.value,
                        requested_by="pow_wow_executor",
                        payload={
                            "schema_version": "review_escalation.v1",
                            "pow_wow_id": pow_wow_id,
                            "dispatch_intent_id": context.dispatch_intent_id,
                            "review_task_name": review_result.task_name,
                            "review_task_id": review_task_id,
                            "revision_rounds_run": rounds_run,
                            "review_text": review_text,
                        },
                    )
                )
                if not isinstance(submitted, AcknowledgementResult):
                    raise RuntimeError("review escalation submission returned malformed evidence")
                raw_approval_id = submitted.payload.values.get("approval_id")
                if not isinstance(raw_approval_id, str) or not raw_approval_id:
                    raise RuntimeError("review escalation submission returned no approval_id")
                approval_id = raw_approval_id
            except Exception as exc:  # noqa: BLE001 - escalation must outlive a ledger failure
                submission_error = f"{type(exc).__name__}: {exc}"
        emit_progress(
            (f"staff review escalated to the operator after {rounds_run} revision round(s)"),
            phase="review_escalated",
            pow_wow_id=pow_wow_id,
            review_task=review_result.task_name,
            rounds_run=rounds_run,
            approval_id=approval_id,
        )
        if approval_id is not None:
            route = (
                f"approval request {approval_id} carries the review text "
                "and awaits an operator decision"
            )
        elif submission_error is not None:
            route = (
                "the REVIEW_ESCALATION approval request could not be submitted "
                f"({submission_error}); the review text is preserved on this "
                "result's review_escalation artifact"
            )
        else:
            route = (
                "no coordination transport is configured, so the escalation is "
                "recorded only on this result's review_escalation artifact"
            )
        summary = (
            f"Staff review escalated to the operator after {rounds_run} revision round(s); {route}."
        )
        escalation_artifact = PowWowArtifact(
            artifact_type="review_escalation",
            schema_version="review_escalation.v1",
            task_name=review_result.task_name,
            content={
                "schema_version": "review_escalation.v1",
                "saga_id": context.saga_id,
                "pow_wow_id": pow_wow_id,
                "dispatch_intent_id": context.dispatch_intent_id,
                "review_task_name": review_result.task_name,
                "review_task_id": review_task_id,
                "revision_rounds_run": rounds_run,
                "approval_id": approval_id,
                "submission_error": submission_error,
                "review_text": review_text,
            },
        )
        return PowWowTaskResult(
            task_name=f"{review_task.task_name}_escalated",
            role=review_task.role,
            status="failed",
            summary=summary,
            risks=(summary,),
            artifacts=(escalation_artifact,),
        )

    def _build_unresolved_review_result(
        self,
        review_task: PowWowTaskSpec,
        *,
        rounds_run: int,
        reason: str,
    ) -> PowWowTaskResult:
        summary = (
            f"Review verdict still requests changes after {rounds_run} "
            f"revision round(s) ({reason}); failing closed to the operator gate."
        )
        return PowWowTaskResult(
            task_name=f"{review_task.task_name}_unresolved",
            role=review_task.role,
            status="failed",
            summary=summary,
            risks=(summary,),
        )

    def _require_approved_code_review(
        self,
        *,
        tasks: Sequence[PowWowTaskSpec],
        context: PowWowExecutionContext,
        task_results: tuple[PowWowTaskResult, ...],
    ) -> tuple[PowWowTaskResult, ...]:
        review_tasks = [
            task
            for task in tasks
            if is_review_task(task) and self._resolve_task_dispatch_kind(task, context) == "code"
        ]
        if not review_tasks:
            return task_results
        typed_reviews = [
            artifact.content
            for result in task_results
            for artifact in result.artifacts
            if artifact.artifact_type == "review_result"
            and artifact.schema_version == "review_result.v1"
        ]
        if typed_reviews and typed_reviews[-1].get("verdict") == ReviewDisposition.APPROVE.value:
            return task_results
        if typed_reviews and typed_reviews[-1].get("verdict") == ReviewDisposition.ESCALATE.value:
            reason = (
                "final typed staff review escalated to the operator; "
                "resolve the pending REVIEW_ESCALATION approval request"
            )
        elif typed_reviews:
            reason = "final typed staff review did not approve"
        else:
            reason = self._missing_review_reason(review_tasks[-1], task_results)
        return (
            *task_results,
            self._build_unresolved_review_result(
                review_tasks[-1],
                rounds_run=0,
                reason=reason,
            ),
        )

    def _missing_review_reason(
        self,
        review_task: PowWowTaskSpec,
        task_results: Sequence[PowWowTaskResult],
    ) -> str:
        """Why no typed review exists, separating "reviewed badly" from "never ran".

        These are different failures with different fixes and they used to share
        one sentence. A review task blocked by a failed implementation produced
        "staff review produced no typed review_result.v1 evidence", which reads
        as a reviewer or parser fault and sends an operator to debug the review
        path. The actual cause was upstream and already recorded on the review
        task's own result, one field away.

        The distinction is worth a branch because the recovery differs. An
        unparsed verdict is what `recover_unparsed_staff_review` exists for; a
        review that never ran has nothing to reparse, and pointing an operator at
        that verb wastes the trip.
        """

        result = next(
            (item for item in task_results if item.task_name == review_task.task_name),
            None,
        )
        if result is not None and result.status != "completed":
            cause = "; ".join(result.risks) or result.summary
            return (
                f"staff review never ran (task {result.status}): {cause}. "
                "There is no verdict to reparse; fix the upstream failure and re-dispatch"
            )
        return "staff review produced no typed review_result.v1 evidence"

    def _capture_code_patches(
        self,
        code_worktrees: dict[str, _CodeWorktreeLease],
    ) -> tuple[PowWowArtifact, ...]:
        artifacts: list[PowWowArtifact] = []
        for lease in code_worktrees.values():
            try:
                payload = build_worktree_code_patch(
                    Path(lease.allocation.worktree_path),
                    group=lease.group,
                    head_sha=lease.allocation.head_sha,
                    branch_name=lease.allocation.branch_name,
                )
            except Exception as exc:  # noqa: BLE001 - capture failure is a risk, not a crash
                payload = {
                    "schema_version": "code_patch.v2",
                    "worktree_group": lease.group,
                    "base_head_sha": lease.allocation.head_sha,
                    "branch_name": lease.allocation.branch_name,
                    "patch": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            if payload.get("patch"):
                artifacts.append(
                    PowWowArtifact(
                        artifact_type="code_patch",
                        schema_version="code_patch.v2",
                        content=payload,
                    )
                )
        return tuple(artifacts)

    def dispatch_pow_wow(
        self,
        pow_wow_id: str,
        target_project: LinkedProject,
        tasks: Sequence[PowWowTaskSpec],
        context: PowWowExecutionContext,
    ) -> PowWowRunResult:
        try:
            validate_planning_visibility_contract(
                tasks,
                required=any(task.planning_phase is not None for task in tasks),
            )
        except PlanningContractError as exc:
            return PowWowRunResult(
                executor=type(self).__name__,
                mode=self.mode,
                pow_wow_id=pow_wow_id,
                target_project_id=target_project.id,
                target_project_path=str(target_project.expanded_path),
                status="BLOCKED",
                output_summary=f"Invalid planning visibility contract: {exc}",
                risks=(str(exc),),
                external_agents_started=False,
                auto_merge=AGENT_BRANCH_AUTO_MERGE,
            )
        agent_tasks = [task for task in tasks if is_agent_task(task)]
        if not agent_tasks:
            return PowWowRunResult(
                executor=type(self).__name__,
                mode=self.mode,
                pow_wow_id=pow_wow_id,
                target_project_id=target_project.id,
                target_project_path=str(target_project.expanded_path),
                status="BLOCKED",
                output_summary="No agent tasks were available for CLI execution.",
                risks=("No isolated worktree was allocated.",),
                external_agents_started=False,
                auto_merge=AGENT_BRANCH_AUTO_MERGE,
            )

        cli_tasks = [task for task in agent_tasks if self._local_harness_for(task) is None]
        cleanup_errors: list[str] = []
        # The caller owns the lease dict so the review revision loop can keep
        # working in the leased trees after the scheduled DAG completes, and so
        # cleanup still happens if either phase raises.
        code_worktrees: dict[str, _CodeWorktreeLease] = {}
        task_results: tuple[PowWowTaskResult, ...] = ()
        try:
            task_results = self._run_dependency_scheduled_tasks(
                pow_wow_id=pow_wow_id,
                target_project=target_project,
                tasks=tasks,
                context=context,
                code_worktrees=code_worktrees,
            )
            task_results = self._run_review_revision_rounds(
                pow_wow_id=pow_wow_id,
                target_project=target_project,
                tasks=tasks,
                context=context,
                task_results=task_results,
                code_worktrees=code_worktrees,
            )
            task_results = self._require_approved_code_review(
                tasks=tasks,
                context=context,
                task_results=task_results,
            )
            patch_artifacts = self._capture_code_patches(code_worktrees)
        finally:
            if self.cleanup_policy == "remove":
                for lease in code_worktrees.values():
                    preserve = any(
                        artifact.content.get("streaming_supervisor", {}).get("preserve_worktree")
                        for result in task_results
                        for artifact in result.artifacts
                        if isinstance(artifact.content.get("streaming_supervisor"), Mapping)
                        and str(lease.allocation.worktree_path)
                        in json.dumps(artifact.content, sort_keys=True)
                    )
                    if preserve:
                        cleanup_errors.append(
                            "Worktree preserved for durable execution checkpoint: "
                            f"{lease.allocation.worktree_path}"
                        )
                        continue
                    cleanup_error = self._remove_worktree(
                        target_project.expanded_path, Path(lease.allocation.worktree_path)
                    )
                    if cleanup_error:
                        cleanup_errors.append(
                            f"Worktree cleanup failed for {lease.group}: {cleanup_error}"
                        )

        changed_files = tuple(dict.fromkeys(f for tr in task_results for f in tr.changed_files))
        verification_output = tuple(line for tr in task_results for line in tr.verification_output)
        run_status = derive_pow_wow_run_status(task_results)
        run_artifact = PowWowArtifact(
            artifact_type="pow_wow_cli_run_result",
            schema_version="pow_wow_cli_run_result.v1",
            content={
                "schema_version": "pow_wow_cli_run_result.v1",
                "mode": self.mode,
                "pow_wow_id": pow_wow_id,
                "target_project_id": target_project.id,
                "task_count": len(task_results),
                "agent_task_count": len(agent_tasks),
                "code_worktree_count": len(code_worktrees),
                "changed_files": list(changed_files),
                "cleanup_policy": self.cleanup_policy,
                "auto_merge": AGENT_BRANCH_AUTO_MERGE,
            },
        )
        return PowWowRunResult(
            executor=type(self).__name__,
            mode=self.mode,
            pow_wow_id=pow_wow_id,
            target_project_id=target_project.id,
            target_project_path=str(target_project.expanded_path),
            status=run_status,
            output_summary=(
                f"CLI executor ran {len(agent_tasks)} agent task(s) for {target_project.id}; "
                f"status={run_status}; auto-merge remained disabled."
            ),
            tasks=task_results,
            changed_files=changed_files,
            verification_commands=self._select_verification_commands(target_project),
            verification_output=verification_output,
            risks=tuple([risk for tr in task_results for risk in tr.risks] + list(cleanup_errors)),
            artifacts=(run_artifact, *patch_artifacts),
            external_agents_started=bool(cli_tasks),
            auto_merge=AGENT_BRANCH_AUTO_MERGE,
        )
