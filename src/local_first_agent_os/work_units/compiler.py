# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Compile a parsed DesignDoc into an immutable execution plan.

Compilation is where document authority is bounded. The parser reports what the
author wrote; the compiler decides what may run, injects the guardrails the
runtime requires, and refuses anything it cannot make safe. A compile failure
prevents a WorkUnit from existing at all, which is the cheapest place to stop bad
work.

Compile-time validation checks the plan. It never checks the world: whether the
repository is in the expected state, whether tests pass today, or whether a
credential exists belongs to the runtime ``VALIDATE`` phase, because those facts
can change between compiling and running.
"""

from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ..capabilities import Capability
from .design_doc import (
    Diagnostic,
    DiagnosticSeverity,
    MilestoneCandidate,
    ParsedDesignDoc,
    SourceSpan,
)
from .executors import (
    EXECUTOR_REGISTRY,
    ApprovalRequirement,
    ExecutorKind,
    default_executor_for_phase,
    lookup_executor,
)
from .lifecycle import FailureClass, LifecyclePhase, phase_ordinal
from .permissions import ACTION_CAPABILITIES, capabilities_for_actions
from .plan import (
    COMPILER_VERSION,
    DEFAULT_AUTHORITY_POLICY,
    SCHEMA_VERSION_COMPILED_WORK_PLAN,
    ApprovalPolicy,
    CompiledMilestone,
    CompiledWorkPlan,
    DependencyEdge,
    DocumentContext,
    FailurePolicy,
    NoDeliveryRequired,
    PermissionPolicy,
    PlanSource,
    RequiredDelivery,
    SourceProvenance,
    ToolPolicy,
    default_lifecycle_binding,
)

_BLOCKING_QUESTION_RE: Final = re.compile(r"\bblocking\b", re.IGNORECASE)

# The capabilities without which an executor cannot act at all. An envelope may
# legitimately narrow anything else an executor declares, but a milestone whose
# executor declares one of these and whose compiled policy lost it can only ever
# spawn a process that changes nothing - observed live on 2026-08-10, when a
# prose-only envelope compiled every implement milestone to an empty tool policy
# and the failure surfaced as an empty diff two dispatches deep.
_ACT_CAPABILITIES: Final = (Capability.WRITE_REPOSITORY, Capability.RUN_COMMAND)


def _narrowest_envelope_action(capability: Capability) -> str:
    """The envelope action that grants exactly this capability and nothing more.

    Derived from the mapping rather than hardcoded, so a renamed action cannot
    leave this suggestion pointing at a vocabulary word that no longer exists.
    """

    for action, granted in ACTION_CAPABILITIES.items():
        if granted == (capability,):
            return action.value
    return capability.value


class ValidationStatus(StrEnum):
    """The three outcomes of compiling, which are genuinely different.

    ``VALID`` may execute. ``BLOCKED`` compiled cleanly but names an unresolved
    condition that must be answered first. ``INVALID`` never becomes executable
    without editing the document.
    """

    VALID = "VALID"
    BLOCKED = "BLOCKED"
    INVALID = "INVALID"


@dataclass(frozen=True)
class CompiledPlanOutcome:
    """A plan that serialized successfully, with whatever still blocks it."""

    plan: CompiledWorkPlan
    diagnostics: tuple[Diagnostic, ...]
    execution_blockers: tuple[str, ...]

    @property
    def validation_status(self) -> ValidationStatus:
        return ValidationStatus.BLOCKED if self.execution_blockers else ValidationStatus.VALID

    @property
    def runnable(self) -> bool:
        return not self.execution_blockers


@dataclass(frozen=True)
class CompilationRejected:
    """The document cannot produce a plan. Nothing is persisted as executable."""

    diagnostics: tuple[Diagnostic, ...]

    @property
    def validation_status(self) -> ValidationStatus:
        return ValidationStatus.INVALID

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.severity is DiagnosticSeverity.ERROR)


CompilationOutcome = CompiledPlanOutcome | CompilationRejected


def _error(code: str, message: str, span: SourceSpan | None) -> Diagnostic:
    return Diagnostic(DiagnosticSeverity.ERROR, code, message, span)


def _blocking_questions(parsed: ParsedDesignDoc) -> tuple[str, ...]:
    """Unresolved questions the author marked as blocking.

    The marker is deterministic syntax rather than a judgment call: a question is
    execution-blocking when its text says so. A model does not get to decide that
    an open question is fine.
    """

    return tuple(
        question
        for question in parsed.unresolved_questions
        if _BLOCKING_QUESTION_RE.search(question)
    )


def _permission_policy(parsed: ParsedDesignDoc) -> PermissionPolicy | None:
    envelope = parsed.permission_envelope
    if envelope is None:
        return None
    autonomous = capabilities_for_actions(envelope.autonomous)
    requested = capabilities_for_actions(tuple(item.action for item in envelope.requested))
    denied = capabilities_for_actions(envelope.denied_without_approval)
    return PermissionPolicy(
        autonomous_capabilities=tuple(item.value for item in autonomous),
        approval_required_capabilities=tuple(item.value for item in requested),
        denied_capabilities=tuple(item.value for item in denied),
    )


def _resolve_executor(
    candidate: MilestoneCandidate,
    phase: LifecyclePhase,
    diagnostics: list[Diagnostic],
) -> ExecutorKind | None:
    if candidate.executor_kind is None:
        return default_executor_for_phase(phase)
    declaration = lookup_executor(candidate.executor_kind)
    if declaration is None:
        diagnostics.append(
            _error(
                "unregistered_executor",
                (
                    f"milestone {candidate.declared_key!r} names executor "
                    f"{candidate.executor_kind!r}, which is not in the registry"
                ),
                candidate.span,
            )
        )
        return None
    if declaration.phase is not phase:
        diagnostics.append(
            _error(
                "executor_phase_mismatch",
                (
                    f"milestone {candidate.declared_key!r} is in phase {phase.value} but "
                    f"executor {declaration.kind.value!r} is declared for "
                    f"{declaration.phase.value}"
                ),
                candidate.span,
            )
        )
        return None
    return declaration.kind


def _approval_policy(
    candidate: MilestoneCandidate,
    executor_kind: ExecutorKind,
    diagnostics: list[Diagnostic],
) -> ApprovalPolicy | None:
    declaration = EXECUTOR_REGISTRY[executor_kind]
    match declaration.approval:
        case ApprovalRequirement.ALWAYS:
            required = True
        case ApprovalRequirement.NEVER:
            if candidate.requires_operator_approval:
                diagnostics.append(
                    _error(
                        "approval_not_available",
                        (
                            f"milestone {candidate.declared_key!r} requests operator approval "
                            f"but executor {executor_kind.value!r} has no approval gate"
                        ),
                        candidate.span,
                    )
                )
                return None
            required = False
        case ApprovalRequirement.OPTIONAL:
            required = candidate.requires_operator_approval
    prompt = (
        f"Approve milestone {candidate.declared_key} ({candidate.title}) "
        f"before {executor_kind.value} proceeds."
    )
    return ApprovalPolicy(required=required, prompt=prompt)


def _detect_cycle(
    edges: dict[str, set[str]],
    keys: tuple[str, ...],
) -> tuple[str, ...] | None:
    """Return one cycle as an ordered key path, or ``None`` when acyclic.

    Reporting the path rather than a boolean is deliberate: the author has to fix
    a specific loop, and "your plan has a cycle" is not enough to find it.
    """

    WHITE, GREY, BLACK = 0, 1, 2
    color = dict.fromkeys(keys, WHITE)
    stack: list[str] = []

    def visit(node: str) -> tuple[str, ...] | None:
        color[node] = GREY
        stack.append(node)
        for neighbour in sorted(edges.get(node, ())):
            if color.get(neighbour, WHITE) == GREY:
                start = stack.index(neighbour)
                return tuple(stack[start:]) + (neighbour,)
            if color.get(neighbour, WHITE) == WHITE:
                found = visit(neighbour)
                if found is not None:
                    return found
        color[node] = BLACK
        stack.pop()
        return None

    for key in keys:
        if color[key] == WHITE:
            found = visit(key)
            if found is not None:
                return found
    return None


def _transitive_dependencies(
    key: str,
    edges: dict[str, set[str]],
) -> set[str]:
    seen: set[str] = set()
    frontier = list(edges.get(key, ()))
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(edges.get(current, ()))
    return seen


def _apply_lifecycle_policy(
    milestones: list[CompiledMilestone],
    edges: dict[str, set[str]],
    spans: dict[str, SourceSpan],
    diagnostics: list[Diagnostic],
) -> None:
    """Enforce the policies a document may not weaken.

    These are the rules that make the lifecycle mean something: implementation
    follows planning, implementation is verified, and delivery follows
    verification. They are checked against the compiled graph rather than trusted
    from the prose.
    """

    by_phase: dict[LifecyclePhase, list[CompiledMilestone]] = defaultdict(list)
    for milestone in milestones:
        by_phase[milestone.phase].append(milestone)

    plan_keys = {item.stable_key for item in by_phase[LifecyclePhase.PLAN]}
    implement_milestones = by_phase[LifecyclePhase.IMPLEMENT]
    verify_keys = {item.stable_key for item in by_phase[LifecyclePhase.VERIFY]}

    for milestone in implement_milestones:
        if not plan_keys:
            diagnostics.append(
                _error(
                    "implement_without_plan",
                    (
                        f"milestone {milestone.stable_key!r} implements work but the document "
                        "declares no PLAN milestone for it to follow"
                    ),
                    spans.get(milestone.stable_key),
                )
            )
            continue
        reachable = _transitive_dependencies(milestone.stable_key, edges)
        if not (reachable & plan_keys):
            diagnostics.append(
                _error(
                    "implement_without_plan_prerequisite",
                    (
                        f"milestone {milestone.stable_key!r} does not depend, directly or "
                        "transitively, on any PLAN milestone"
                    ),
                    spans.get(milestone.stable_key),
                )
            )

    if implement_milestones and not verify_keys:
        for milestone in implement_milestones:
            diagnostics.append(
                _error(
                    "missing_verification",
                    (
                        f"milestone {milestone.stable_key!r} changes the system but the document "
                        "declares no VERIFY milestone"
                    ),
                    spans.get(milestone.stable_key),
                )
            )
    else:
        verified: set[str] = set()
        for verify_milestone in by_phase[LifecyclePhase.VERIFY]:
            verified |= _transitive_dependencies(verify_milestone.stable_key, edges)
        for milestone in implement_milestones:
            if milestone.stable_key not in verified:
                diagnostics.append(
                    _error(
                        "missing_verification",
                        (
                            f"milestone {milestone.stable_key!r} is not covered by any VERIFY "
                            "milestone's dependencies"
                        ),
                        spans.get(milestone.stable_key),
                    )
                )

    for milestone in by_phase[LifecyclePhase.DELIVER]:
        if not verify_keys:
            continue
        reachable = _transitive_dependencies(milestone.stable_key, edges)
        if not (reachable & verify_keys):
            diagnostics.append(
                _error(
                    "deliver_without_verification",
                    (
                        f"milestone {milestone.stable_key!r} delivers without depending on any "
                        "VERIFY milestone"
                    ),
                    spans.get(milestone.stable_key),
                )
            )


def compile_design_doc(
    parsed: ParsedDesignDoc,
    *,
    design_doc_revision_id: str,
) -> CompilationOutcome:
    """Turn a parsed document into a compiled plan or a rejection.

    The order of work matters and is fixed: structural errors from parsing stop
    everything, then phases and executors are resolved, then the dependency graph
    is validated, then lifecycle policy is applied to the graph, and only a clean
    result is serialized and hashed.
    """

    diagnostics: list[Diagnostic] = list(parsed.diagnostics)
    if any(item.severity is DiagnosticSeverity.ERROR for item in diagnostics):
        return CompilationRejected(tuple(diagnostics))

    ordinals: dict[LifecyclePhase, int] = defaultdict(int)
    milestones: list[CompiledMilestone] = []
    capability_blockers: list[str] = []
    spans: dict[str, SourceSpan] = {}
    declared_keys = {candidate.declared_key for candidate in parsed.milestone_candidates}
    permission_policy = _permission_policy(parsed)
    capability_ceiling = (
        frozenset(permission_policy.capability_ceiling) if permission_policy is not None else None
    )

    for candidate in parsed.milestone_candidates:
        spans[candidate.declared_key] = candidate.span
        if candidate.declared_phase is None:
            diagnostics.append(
                _error(
                    "missing_phase",
                    (
                        f"milestone {candidate.declared_key!r} declares no lifecycle phase; "
                        "a phase is required and cannot be assumed"
                    ),
                    candidate.span,
                )
            )
            continue
        phase = candidate.declared_phase.phase
        executor_kind = _resolve_executor(candidate, phase, diagnostics)
        if executor_kind is None:
            continue
        approval_policy = _approval_policy(candidate, executor_kind, diagnostics)
        if approval_policy is None:
            continue
        if not candidate.acceptance_criteria:
            diagnostics.append(
                _error(
                    "missing_acceptance_criteria",
                    (
                        f"milestone {candidate.declared_key!r} declares no acceptance criteria, "
                        "so nothing could decide whether it succeeded"
                    ),
                    candidate.span,
                )
            )
            continue
        declaration = EXECUTOR_REGISTRY[executor_kind]
        unproducible = sorted(
            set(candidate.required_artifacts) - set(declaration.required_artifact_types)
        )
        if unproducible:
            # The union below is what makes this worth rejecting rather than
            # warning about. A document-declared type joins the milestone's
            # requirements whether or not anything can emit it, so an unknown
            # name becomes a requirement that no run can ever satisfy. Failing
            # here costs a recompile; failing at runtime costs an agent hour and
            # is indistinguishable from the agent having done poor work.
            producible = ", ".join(item.value for item in declaration.required_artifact_types)
            diagnostics.append(
                _error(
                    "unproducible_required_artifact",
                    (
                        f"milestone {candidate.declared_key!r} requires "
                        f"{', '.join(repr(item) for item in unproducible)}, which its executor "
                        f"{executor_kind.value!r} does not produce; it produces {producible}"
                    ),
                    candidate.span,
                )
            )
            continue
        required_artifacts = tuple(
            sorted({*candidate.required_artifacts, *declaration.required_artifact_types})
        )
        ordinals[phase] += 1
        permitted = tuple(
            capability.value
            for capability in declaration.permitted_tools
            if capability_ceiling is None or capability.value in capability_ceiling
        )
        stripped_act_capabilities = tuple(
            capability
            for capability in _ACT_CAPABILITIES
            if capability in declaration.permitted_tools and capability.value not in permitted
        )
        if stripped_act_capabilities:
            named = ", ".join(item.value for item in stripped_act_capabilities)
            fixes = ", ".join(
                repr(_narrowest_envelope_action(item)) for item in stripped_act_capabilities
            )
            capability_blockers.append(
                f"milestone {candidate.declared_key!r} ({executor_kind.value}) compiled to a "
                f"tool policy without {named}, so its executor can never act; declare {fixes} "
                "as autonomous in the permission envelope, or change the milestone's executor"
            )
        milestones.append(
            CompiledMilestone(
                stable_key=candidate.declared_key,
                title=candidate.title,
                description=candidate.description,
                phase=phase,
                ordinal=ordinals[phase],
                dependencies=tuple(sorted(set(candidate.dependencies))),
                acceptance_criteria=candidate.acceptance_criteria,
                required_artifacts=required_artifacts,
                executor_kind=executor_kind,
                tool_policy=ToolPolicy(permitted),
                approval_policy=approval_policy,
                failure_policy=FailurePolicy(
                    default_class=FailureClass.NONRECOVERABLE,
                    max_attempts=declaration.retry.max_attempts,
                    blocks_phase=True,
                ),
                source_provenance=SourceProvenance(
                    design_doc_id=parsed.identity.design_doc_id,
                    design_doc_revision_id=design_doc_revision_id,
                    source_heading=candidate.source_heading,
                    source_start=candidate.span.start,
                    source_end=candidate.span.end,
                ),
                timeout_seconds=declaration.timeout_seconds,
                phase_inferred=candidate.declared_phase.inferred,
            )
        )

    if any(item.severity is DiagnosticSeverity.ERROR for item in diagnostics):
        return CompilationRejected(tuple(diagnostics))

    edges: dict[str, set[str]] = {item.stable_key: set(item.dependencies) for item in milestones}
    phases_by_key = {item.stable_key: item.phase for item in milestones}

    for milestone in milestones:
        for dependency in milestone.dependencies:
            if dependency == milestone.stable_key:
                diagnostics.append(
                    _error(
                        "self_dependency",
                        f"milestone {milestone.stable_key!r} depends on itself",
                        spans.get(milestone.stable_key),
                    )
                )
                continue
            if dependency not in declared_keys:
                diagnostics.append(
                    _error(
                        "unresolved_dependency",
                        (
                            f"milestone {milestone.stable_key!r} depends on {dependency!r}, "
                            "which is not a compiled milestone"
                        ),
                        spans.get(milestone.stable_key),
                    )
                )
                continue
            dependency_phase = phases_by_key.get(dependency)
            if dependency_phase is None:
                continue
            if phase_ordinal(dependency_phase) > phase_ordinal(milestone.phase):
                diagnostics.append(
                    _error(
                        "future_phase_dependency",
                        (
                            f"{milestone.phase.value} milestone {milestone.stable_key!r} depends "
                            f"on {dependency_phase.value} milestone {dependency!r}; dependencies "
                            "may only point at the same or an earlier phase"
                        ),
                        spans.get(milestone.stable_key),
                    )
                )

    cycle = _detect_cycle(edges, tuple(item.stable_key for item in milestones))
    if cycle is not None:
        diagnostics.append(
            _error(
                "dependency_cycle",
                "dependency cycle: " + " -> ".join(cycle),
                spans.get(cycle[0]),
            )
        )

    if any(item.severity is DiagnosticSeverity.ERROR for item in diagnostics):
        return CompilationRejected(tuple(diagnostics))

    _apply_lifecycle_policy(milestones, edges, spans, diagnostics)
    if any(item.severity is DiagnosticSeverity.ERROR for item in diagnostics):
        return CompilationRejected(tuple(diagnostics))

    dependency_edges = tuple(
        DependencyEdge(milestone_key=milestone.stable_key, depends_on_key=dependency)
        for milestone in milestones
        for dependency in sorted(milestone.dependencies)
    )

    required_final_artifacts = tuple(
        sorted(
            {
                *parsed.required_artifacts,
                *(
                    artifact
                    for milestone in milestones
                    if milestone.phase is LifecyclePhase.DELIVER
                    for artifact in milestone.required_artifacts
                ),
            }
        )
    )
    if required_final_artifacts:
        delivery_contract = RequiredDelivery(artifact_types=required_final_artifacts)
    elif any(item.phase is LifecyclePhase.IMPLEMENT for item in milestones):
        diagnostics.append(
            _error(
                "missing_delivery_contract",
                (
                    "the plan changes the system but declares neither a DELIVER milestone "
                    "nor document-level Required Artifacts"
                ),
                None,
            )
        )
        return CompilationRejected(tuple(diagnostics))
    else:
        delivery_contract = NoDeliveryRequired(
            reason="the plan is advisory-only and declares no implementation milestone"
        )

    target_project_id, target_blocker, target_notice = _resolve_target_project(
        parsed.declared_target_project_id
    )

    plan = CompiledWorkPlan(
        schema_version=SCHEMA_VERSION_COMPILED_WORK_PLAN,
        compiler_version=COMPILER_VERSION,
        work_unit_identity=(f"{parsed.identity.design_doc_id}@{parsed.content_hash}"),
        target_project_id=target_project_id,
        source=PlanSource(
            design_doc_id=parsed.identity.design_doc_id,
            design_doc_revision_id=design_doc_revision_id,
            content_hash=parsed.content_hash,
        ),
        lifecycle=default_lifecycle_binding(),
        milestones=tuple(milestones),
        dependency_edges=dependency_edges,
        document_context=DocumentContext(
            motivation=parsed.motivation,
            requirements=parsed.requirements,
            non_goals=parsed.non_goals,
            constraints=parsed.constraints,
            assumptions=parsed.assumptions,
            acceptance_criteria=parsed.acceptance_criteria,
            failure_modes=parsed.failure_modes,
            rollout=parsed.rollout,
        ),
        authority_policy=DEFAULT_AUTHORITY_POLICY,
        declared_delivery_pace=parsed.declared_delivery_pace,
        delivery_contract=delivery_contract,
        permission_policy=permission_policy,
    )

    blockers: list[str] = [
        f"unresolved blocking question: {question}" for question in _blocking_questions(parsed)
    ]
    blockers.extend(capability_blockers)
    if parsed.declared_target_project_id is None:
        # An execution blocker, because silence here already cost a real
        # dispatch. Five design documents in docs/ had no `Target project:` line,
        # every one resolved to the project-center default, and the compiler
        # reported VALID and runnable with zero diagnostics. Starting one sent a
        # frontier agent to read an unrelated repository for context while
        # implementing in a worktree of this one, and the failure surfaced four
        # layers later as a complaint about missing staff-review evidence.
        #
        # Blocking rather than inferring from the document's own location, which
        # is the tempting fix. The compiler is pure and offline so that one
        # document compiles to one plan hash on every host; a default read from
        # the filesystem would make the same text produce different plans in
        # different checkouts, and the plan hash is what approval binds to.
        #
        # A blocker refuses a run rather than refusing a compile: the document
        # still compiles and can be read, it just cannot start until it says
        # which repository it means. The cost of the line is one line, and the
        # cost of guessing wrong is a frontier dispatch against the wrong tree.
        blockers.append(
            "the document declares no target project, so it cannot be started. "
            "Add a 'Target project: <id>' line naming the repository this document "
            f"means. Without it this would run against {target_project_id!r}, the "
            "project-center default."
        )
    if target_blocker is not None:
        blockers.append(target_blocker)
    if target_notice is not None:
        # Reported rather than logged: creating a directory and editing the
        # operator's registry is a side effect, and it belongs where they read
        # the rest of what compiling did.
        diagnostics.append(
            Diagnostic(
                severity=DiagnosticSeverity.INFO,
                code="target_project_scaffolded",
                message=target_notice,
            )
        )
    for item in diagnostics:
        if item.severity is DiagnosticSeverity.WARNING and item.code == "ambiguous_inferred_phase":
            blockers.append(item.message)

    return CompiledPlanOutcome(
        plan=plan,
        diagnostics=tuple(diagnostics),
        execution_blockers=tuple(blockers),
    )


def _resolve_target_project(declared: str | None) -> tuple[str, str | None, str | None]:
    """Resolve the document's target to a project id, adopting one if needed.

    An unregistered target used to become an execution blocker. It no longer
    does: a missing target directory is an instruction to make one, not a reason
    to refuse a milestone. `adopt_unregistered_target` creates the repository
    under the current working directory and registers it, so the id every later
    `project_by_id` looks up is real by the time anything asks.

    Silence still resolves to the project-center default rather than inventing a
    name. That default is a registered project the operator chose, and supplying
    a missing target is this function's job in a way that overriding a chosen one
    is not.

    A blocker survives for exactly one case: an id that cannot become a directory
    name. That is a typo the operator has to see, and guessing at what they meant
    would be worse than saying so.
    """

    from ..project_center import load_project_center
    from ..project_scaffold import adopt_unregistered_target
    from ..settings import get_settings

    center = load_project_center()
    if declared is None:
        return center.default_saga_project, None, None
    known = {project.id for project in center.projects}
    if declared in known:
        return declared, None, None

    try:
        adopted = adopt_unregistered_target(declared, settings=get_settings())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        return declared, (
            f"target project {declared!r} is not registered and could not be "
            f"adopted: {error}. Known projects: {', '.join(sorted(known))}"
        ), None
    verb = "created" if adopted["created"] else "adopted existing"
    return declared, None, (
        f"{verb} target project {declared!r} at {adopted['path']} and registered it"
    )


__all__ = [
    "CompilationOutcome",
    "CompilationRejected",
    "CompiledPlanOutcome",
    "ValidationStatus",
    "compile_design_doc",
]
