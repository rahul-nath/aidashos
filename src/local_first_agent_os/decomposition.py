# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Planner front-end that turns one intent into a durable task DAG.

The executor already knows how to run a dependency-aware graph of
``PowWowTaskSpec`` values. This module keeps decomposition separate from
execution: a planner can be deterministic, local-model backed, or frontier-model
backed, but the output contract stays the same validated task DAG.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from .coordination.contracts import DispatchKind
from .pow_wow import PowWowTaskSpec
from .pow_wow.cast import CastMember, build_cast_tasks
from .pow_wow.planning import PlanningContractError, validate_planning_visibility_contract
from .pow_wow.protocol import PlanningPhase, ReferencePack, TaskPurpose
from .project_center import LinkedProject
from .staffing import JudgmentRole
from .vocabulary import DispatchTier

SCHEMA_VERSION_DECOMPOSITION_PLAN = "decomposition_plan.v1"
SCHEMA_VERSION_MINI_GAWD_DOC = "mini_gawd_doc.v1"


class DecompositionError(ValueError):
    """Raised when a planner emits an invalid or unsafe task DAG."""


@dataclass(frozen=True)
class TimeBudgetPhase:
    phase: str
    hours: str
    deliverable: str

    def to_payload(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class MiniGawdDecision:
    decision_id: str
    decision: str
    rationale: str
    date: str

    def to_payload(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class MiniGawdDoc:
    project: str
    status: str
    date: str
    time_budget: tuple[TimeBudgetPhase, ...]
    theory: str
    why: str
    golden_flow: tuple[str, ...]
    in_scope: tuple[str, ...]
    non_goals: tuple[str, ...]
    unit_of_work: str
    lifecycle: tuple[str, ...]
    data_model: tuple[str, ...]
    failure_that_matters: str
    verification: tuple[str, ...]
    decisions: tuple[MiniGawdDecision, ...]
    deferred: tuple[str, ...]
    priority_order: tuple[str, ...] = (
        "correctness",
        "stability",
        "debuggability",
        "throughput",
        "latency",
    )
    schema_version: str = SCHEMA_VERSION_MINI_GAWD_DOC
    doc_version: str = "v4-mini"

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "doc_version": self.doc_version,
            "project": self.project,
            "status": self.status,
            "date": self.date,
            "priority_order": list(self.priority_order),
            "time_budget": [phase.to_payload() for phase in self.time_budget],
            "theory": self.theory,
            "why": self.why,
            "golden_flow": list(self.golden_flow),
            "scope": {
                "in": list(self.in_scope),
                "non_goals": list(self.non_goals),
            },
            "core_design": {
                "unit_of_work": self.unit_of_work,
                "lifecycle": list(self.lifecycle),
                "data_model": list(self.data_model),
            },
            "failure_that_matters": self.failure_that_matters,
            "verification": list(self.verification),
            "decision_log": [decision.to_payload() for decision in self.decisions],
            "deferred": list(self.deferred),
        }


@dataclass(frozen=True)
class DecompositionPlan:
    planner: str
    rationale: str
    mini_gawd: MiniGawdDoc
    tasks: tuple[PowWowTaskSpec, ...]
    planner_prompt: str
    schema_version: str = SCHEMA_VERSION_DECOMPOSITION_PLAN

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "planner": self.planner,
            "rationale": self.rationale,
            "mini_gawd": self.mini_gawd.to_payload(),
            "planner_prompt": self.planner_prompt,
            "tasks": [task.to_payload() for task in self.tasks],
        }


class DecompositionPlanner(Protocol):
    def plan(
        self,
        *,
        intent_id: str,
        tier: DispatchTier,
        kind: DispatchKind,
        prompt: str,
        target_project: LinkedProject,
        intent: Mapping[str, Any],
    ) -> DecompositionPlan: ...


PlannerFn = Callable[[str], Mapping[str, Any]]


class PromptedDecompositionPlanner:
    """Model-backed planner adapter with the same validated DAG output.

    The injected callable owns the model/vendor/process choice. This keeps the
    concrete model call a secret of the composition layer while this module owns
    the planner prompt and DAG validation contract.
    """

    def __init__(self, planner_fn: PlannerFn, *, name: str = "prompted.v1") -> None:
        self.planner_fn = planner_fn
        self.name = name

    def plan(
        self,
        *,
        intent_id: str,
        tier: DispatchTier,
        kind: DispatchKind,
        prompt: str,
        target_project: LinkedProject,
        intent: Mapping[str, Any],
    ) -> DecompositionPlan:
        del intent
        planner_prompt = build_decomposition_prompt(
            intent_id=intent_id,
            tier=tier,
            kind=kind,
            prompt=prompt,
            target_project=target_project,
        )
        payload = self.planner_fn(planner_prompt)
        tasks = parse_task_specs_from_planner_payload(payload, default_dispatch_kind=kind)
        tasks = _apply_project_reference_packs(tasks, target_project)
        tasks = _add_marketing_site_browser_acceptance(tasks, target_project, kind=kind)
        _validate_task_dag(tasks)
        mini_gawd = parse_mini_gawd_from_planner_payload(payload)
        rationale = str(payload.get("rationale") or "Prompted planner emitted task DAG.")
        return DecompositionPlan(
            planner=self.name,
            rationale=rationale,
            mini_gawd=mini_gawd,
            tasks=tasks,
            planner_prompt=planner_prompt,
        )


def _apply_project_reference_packs(
    tasks: Sequence[PowWowTaskSpec],
    target_project: LinkedProject,
) -> tuple[PowWowTaskSpec, ...]:
    # Keep planner-compatible project doubles and older callers working while
    # LinkedProject configurations adopt the optional contract field.
    required: tuple[ReferencePack, ...] = tuple(getattr(target_project, "reference_packs", ()))
    if not required:
        return tuple(tasks)
    return tuple(
        replace(
            task,
            reference_packs=task.reference_packs
            + tuple(pack for pack in required if pack not in task.reference_packs),
        )
        for task in tasks
    )


def _add_marketing_site_browser_acceptance(
    tasks: Sequence[PowWowTaskSpec],
    target_project: LinkedProject,
    *,
    kind: DispatchKind,
) -> tuple[PowWowTaskSpec, ...]:
    """Insert one host-owned responsive check between implementation and review."""

    required = tuple(getattr(target_project, "reference_packs", ()))
    if kind is not DispatchKind.CODE or ReferencePack.MARKETING_SITE not in required:
        return tuple(tasks)
    implementation = next(
        (task for task in tasks if task.purpose is TaskPurpose.IMPLEMENTATION),
        None,
    )
    review = next(
        (task for task in tasks if task.purpose is TaskPurpose.REVIEW),
        None,
    )
    if implementation is None or review is None:
        raise DecompositionError(
            "marketing-site code work requires implementation and staff review tasks"
        )
    browser_task = PowWowTaskSpec(
        task_name=f"{implementation.task_name}_browser_acceptance",
        role="browser_acceptance",
        description=(
            "Run the host-owned isolated local preview and capture every configured mobile and "
            "desktop path. Fail on missing captures, overflow, clipped required elements, "
            "console errors, failed requests, or preview-process cleanup failure."
        ),
        success_criteria=(
            "Every configured path and viewport has a durable screenshot artifact.",
            "Overflow, console, network, selector, and process cleanup evidence passes.",
        ),
        purpose=TaskPurpose.BROWSER_ACCEPTANCE,
        dispatch_kind=DispatchKind.CODE,
        blocked_by=(implementation.task_name,),
        worktree_group=implementation.worktree_group,
        reference_packs=(ReferencePack.MARKETING_SITE,),
    )
    revised_review = replace(
        review,
        blocked_by=tuple(
            browser_task.task_name if name == implementation.task_name else name
            for name in review.blocked_by
        ),
    )
    if browser_task.task_name not in revised_review.blocked_by:
        revised_review = replace(
            revised_review,
            blocked_by=(*revised_review.blocked_by, browser_task.task_name),
        )
    output: list[PowWowTaskSpec] = []
    for task in tasks:
        if task is review:
            output.extend((browser_task, revised_review))
        else:
            output.append(task)
    return tuple(output)


def build_decomposition_prompt(
    *,
    intent_id: str,
    tier: DispatchTier,
    kind: DispatchKind,
    prompt: str,
    target_project: LinkedProject,
) -> str:
    """Prompt contract for a model planner that emits the same DAG schema.

    This borrows the decomposition discipline from managed-agent planning
    recipes while keeping this repo's substrate: ledger tasks, dependencies,
    tiered bench resolution, worktree isolation, and approval gates.
    """
    return (
        "You are a planner for a durable local agent control plane.\n"
        "Decompose the operator intent into a small task DAG. Output JSON only.\n\n"
        "Planning rules:\n"
        "- Plan big, execute small: split ambiguous work into bounded tasks.\n"
        "- Use junior tasks for cheap context extraction or independent drafts.\n"
        "- Use senior tasks for implementation or synthesis.\n"
        "- Use staff tasks for final review, verdicts, and risk calls.\n"
        "- Use blocked_by to encode dependencies; do not rely on chat memory.\n"
        "- Use dispatch_kind=code only for tasks that need an isolated worktree.\n"
        "- Never plan merge, deploy, purchase, or external communications without an "
        "approval gate.\n\n"
        "Required JSON shape:\n"
        "{\n"
        '  "rationale": "short reason for the decomposition",\n'
        '  "mini_gawd": {\n'
        '    "project": "project or target name",\n'
        '    "status": "Status-Quo|MVP|Scoped",\n'
        '    "time_budget": [{"phase": "name", "hours": "0.5h", "deliverable": "thing"}],\n'
        '    "theory": "archetype and computational shape",\n'
        '    "why": "concrete pain this removes",\n'
        '    "golden_flow": ["step 1", "step 2"],\n'
        '    "scope": {"in": ["included"], "non_goals": ["cut"]},\n'
        '    "core_design": {\n'
        '      "unit_of_work": "unit",\n'
        '      "lifecycle": ["created", "done|failed"],\n'
        '      "data_model": ["entity/storage/consistency note"]\n'
        "    },\n"
        '    "failure_that_matters": "top failure and recovery",\n'
        '    "verification": ["smoke proof"],\n'
        '    "decision_log": [{"decision_id": "D1", "decision": "x", "rationale": "y"}],\n'
        '    "deferred": ["cut for later"]\n'
        "  },\n"
        '  "tasks": [\n'
        "    {\n"
        '      "task_name": "snake_case_unique_name",\n'
        '      "role": "role_name",\n'
        '      "tier": "junior|senior|staff",\n'
        f'      "dispatch_kind": "{"|".join(item.value for item in DispatchKind)}",\n'
        '      "planning_phase": "one required typed planning phase or null",\n'
        '      "reference_packs": ["marketing_site"],\n'
        '      "description": "bounded work instruction",\n'
        '      "success_criteria": ["observable completion condition"],\n'
        '      "blocked_by": ["earlier_task_name"],\n'
        '      "worktree_group": "optional shared group for code tasks"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "For every code plan, emit exactly these five planning phases. "
        "Use planning_phase values senior_independent_reading, junior_verification_plan, "
        "senior_owned_plan, staff_independent_reading, and staff_final_review. "
        "The senior and staff independent-reading tasks have no dependencies. "
        "The junior plan is blocked only by "
        "the senior reading. The senior-owned plan is blocked by both of those tasks. The "
        "staff final review is blocked by the staff reading and senior-owned plan. Junior "
        "output is non-exhaustive and cannot define or narrow the problem boundary.\n\n"
        "For tasks that design, implement, verify, or review a customer-facing marketing "
        'website, set reference_packs to ["marketing_site"]. Use an empty array for '
        "unrelated work. The host validates and injects the versioned cross-harness doctrine.\n\n"
        f"Intent id: {intent_id}\n"
        f"Requested tier: {tier.value}\n"
        f"Intent kind: {kind}\n"
        f"Target project: {target_project.id} ({target_project.kind})\n"
        f"Target read-only: {target_project.read_only}\n"
        f"Operator intent:\n{prompt}\n"
    )


class RuleBasedDecompositionPlanner:
    """Conservative default planner used when no model planner is injected."""

    def plan(
        self,
        *,
        intent_id: str,
        tier: DispatchTier,
        kind: DispatchKind,
        prompt: str,
        target_project: LinkedProject,
        intent: Mapping[str, Any],
    ) -> DecompositionPlan:
        del intent
        planner_prompt = build_decomposition_prompt(
            intent_id=intent_id,
            tier=tier,
            kind=kind,
            prompt=prompt,
            target_project=target_project,
        )
        prefix = _task_prefix(intent_id)
        if kind is DispatchKind.CAST:
            tasks = _cast_plan(prefix, prompt)
            rationale = (
                "Cast work runs several stances concurrently and reduces them with a "
                "synthesizer, because the disagreement between stances is the output."
            )
        elif kind is DispatchKind.CODE:
            tasks = _code_plan(prefix, prompt)
            rationale = (
                "Code work is split into local context extraction, senior implementation, "
                "and staff review in the same code worktree."
            )
        elif tier == DispatchTier.STAFF:
            tasks = _staff_advisory_plan(prefix, prompt)
            rationale = (
                "Staff advisory work gets cheap junior context, senior synthesis, "
                "then a staff verdict."
            )
        elif tier == DispatchTier.SENIOR:
            tasks = _senior_advisory_plan(prefix, prompt)
            rationale = "Senior advisory work gets a junior context pass before synthesis."
        else:
            tasks = (
                _task(
                    name=f"{prefix}_junior_answer",
                    role="junior",
                    tier=DispatchTier.JUNIOR,
                    dispatch_kind=DispatchKind.ADVISORY,
                    description=prompt,
                    success_criteria=(
                        "Answer the operator intent directly.",
                        "Keep the result bounded and cite any uncertainty.",
                    ),
                ),
            )
            rationale = "Junior advisory work can run as one bounded local-model task."
        tasks = _apply_project_reference_packs(tasks, target_project)
        tasks = _add_marketing_site_browser_acceptance(tasks, target_project, kind=kind)
        _validate_task_dag(tasks)
        mini_gawd = _mini_gawd_for_plan(
            intent_id=intent_id,
            kind=kind,
            prompt=prompt,
            target_project=target_project,
            rationale=rationale,
            tasks=tasks,
        )
        return DecompositionPlan(
            planner="rule_based.v1",
            rationale=rationale,
            mini_gawd=mini_gawd,
            tasks=tasks,
            planner_prompt=planner_prompt,
        )


def parse_task_specs_from_planner_payload(
    payload: Mapping[str, Any],
    *,
    default_dispatch_kind: DispatchKind,
) -> tuple[PowWowTaskSpec, ...]:
    """Parse a model planner's JSON payload into the executor's task contract."""
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, Sequence) or isinstance(raw_tasks, (str, bytes)):
        raise DecompositionError("planner payload must contain a tasks array")
    tasks: list[PowWowTaskSpec] = []
    for raw in raw_tasks:
        if not isinstance(raw, Mapping):
            raise DecompositionError("planner task entries must be objects")
        task_name = _clean_task_name(str(raw.get("task_name") or ""))
        if not task_name:
            raise DecompositionError("planner task is missing task_name")
        tier = DispatchTier(str(raw.get("tier") or "junior"))
        raw_dispatch_kind = str(raw.get("dispatch_kind") or default_dispatch_kind.value)
        try:
            dispatch_kind = DispatchKind(raw_dispatch_kind)
        except ValueError as exc:
            raise DecompositionError(
                f"invalid dispatch_kind for {task_name}: {raw_dispatch_kind}"
            ) from exc
        blocked_by = _string_tuple(raw.get("blocked_by"))
        success_criteria = _string_tuple(raw.get("success_criteria"))
        worktree_group_raw = raw.get("worktree_group")
        worktree_group = _clean_task_name(str(worktree_group_raw)) if worktree_group_raw else None
        planning_phase_raw = raw.get("planning_phase")
        planning_phase = PlanningPhase(str(planning_phase_raw)) if planning_phase_raw else None
        reference_packs = tuple(
            ReferencePack(value) for value in _string_tuple(raw.get("reference_packs"))
        )
        tasks.append(
            PowWowTaskSpec(
                task_name=task_name,
                role=str(raw.get("role") or tier.value),
                description=str(raw.get("description") or "").strip(),
                success_criteria=success_criteria,
                judgment=JudgmentRole(name=str(raw.get("role") or tier.value), tier=tier),
                dispatch_kind=dispatch_kind,
                blocked_by=blocked_by,
                worktree_group=worktree_group,
                planning_phase=planning_phase,
                reference_packs=reference_packs,
            )
        )
    _validate_task_dag(tasks)
    try:
        validate_planning_visibility_contract(
            tasks,
            required=default_dispatch_kind is DispatchKind.CODE,
        )
    except PlanningContractError as exc:
        raise DecompositionError(str(exc)) from exc
    return tuple(tasks)


def parse_mini_gawd_from_planner_payload(payload: Mapping[str, Any]) -> MiniGawdDoc:
    raw = payload.get("mini_gawd")
    if not isinstance(raw, Mapping):
        raise DecompositionError("planner payload must contain a mini_gawd object")
    scope = raw.get("scope")
    if not isinstance(scope, Mapping):
        raise DecompositionError("mini_gawd.scope must be an object")
    core_design = raw.get("core_design")
    if not isinstance(core_design, Mapping):
        raise DecompositionError("mini_gawd.core_design must be an object")
    return MiniGawdDoc(
        project=_required_str(raw, "project"),
        status=str(raw.get("status") or "Scoped"),
        date=str(raw.get("date") or _today()),
        time_budget=_time_budget_tuple(raw.get("time_budget")),
        theory=_required_str(raw, "theory"),
        why=_required_str(raw, "why"),
        golden_flow=_string_tuple(raw.get("golden_flow")),
        in_scope=_string_tuple(scope.get("in")),
        non_goals=_string_tuple(scope.get("non_goals")),
        unit_of_work=_required_str(core_design, "unit_of_work"),
        lifecycle=_string_tuple(core_design.get("lifecycle")),
        data_model=_string_tuple(core_design.get("data_model")),
        failure_that_matters=_required_str(raw, "failure_that_matters"),
        verification=_string_tuple(raw.get("verification")),
        decisions=_decision_tuple(raw.get("decision_log")),
        deferred=_string_tuple(raw.get("deferred")),
        priority_order=tuple(
            str(item)
            for item in raw.get("priority_order")
            or (
                "correctness",
                "stability",
                "debuggability",
                "throughput",
                "latency",
            )
        ),
    )


# The stances a cast holds when the intent names none. Deliberately about how a
# question is approached rather than about a trade, because a default that
# guessed "marketing, design, engineering" would be wrong for every question that
# is not a product launch, and a wrong default is worse than a general one. A
# domain cast is the operator's to declare; this is what a bare `cast` intent
# gets.
#
# Every member sits on JUNIOR today, which means every member is gemma4, which
# means this cast currently measures one model's prior three times. That is the
# objection that parked the homogeneous junior swarm, and it is recorded here
# rather than hidden because the fix is not this module's to make.
#
# Two drafts own it. `Model residency as a schedulable resource` establishes that
# one heavy model is resident at a time (gemma4 at 5.6GB plus one of qwen3.8 at
# 20.6GB or glimmer at ~21GB, against 36GB), so a cast may hold at most one heavy
# stance before it starts paying a swap per member. `Midlevel tier and deferred
# frontier review` adds the seat those heavy models would occupy, since `DispatchTier`
# has only JUNIOR, SENIOR, and STAFF and the junior seat is defined as cheap and
# advisory.
#
# So the diverse default is one midlevel stance beside two junior ones, and it is
# unavailable until that tier exists. Until then this is a panel of stances
# rather than a panel of architectures, and it should be read as the weaker of
# the two.
DEFAULT_CAST: Final[tuple[CastMember, ...]] = (
    CastMember(
        name="advocate",
        stance=(
            "Argue the strongest version of the proposal and say what has to be "
            "true for it to work."
        ),
        tier=DispatchTier.JUNIOR,
    ),
    CastMember(
        name="skeptic",
        stance=(
            "Argue the strongest case against, naming the failure that would actually happen first."
        ),
        tier=DispatchTier.JUNIOR,
    ),
    CastMember(
        name="pragmatist",
        stance=(
            "Ignore whether it is a good idea and say what it would cost to do the "
            "smallest real version."
        ),
        tier=DispatchTier.JUNIOR,
    ),
)


def _cast_plan(prefix: str, prompt: str) -> tuple[PowWowTaskSpec, ...]:
    return build_cast_tasks(prefix=prefix, goal=prompt, members=DEFAULT_CAST)


def _code_plan(prefix: str, prompt: str) -> tuple[PowWowTaskSpec, ...]:
    worktree_group = f"{prefix}_code"
    senior_reading_task = f"{prefix}_senior_independent_reading"
    junior_plan_task = f"{prefix}_junior_verification_plan"
    implementation_task = f"{prefix}_senior_implementation"
    staff_reading_task = f"{prefix}_staff_independent_reading"
    return (
        _task(
            name=senior_reading_task,
            role="independent_reader",
            tier=DispatchTier.SENIOR,
            dispatch_kind=DispatchKind.ADVISORY,
            description=(
                "Read the raw saga contract and repository independently. Record source-anchored "
                "claims, affected seams, invariants, risks, uncertainties, and candidate "
                "verification oracles before any junior conclusion is model-visible."
            ),
            success_criteria=(
                "Raw contract claims have source anchors.",
                "Repository and test evidence pointers are explicit.",
                "No junior output was visible during this reading.",
            ),
            planning_phase=PlanningPhase.SENIOR_INDEPENDENT_READING,
        ),
        _task(
            name=staff_reading_task,
            role="independent_reviewer",
            tier=DispatchTier.STAFF,
            dispatch_kind=DispatchKind.ADVISORY,
            description=(
                "Independently read the raw saga contract and repository. Record the acceptance "
                "boundary, likely failure seams, and review oracles without receiving junior or "
                "senior conclusions."
            ),
            success_criteria=(
                "The raw contract and repository were read independently.",
                "Potential blocking concerns and review oracles are source-anchored.",
            ),
            planning_phase=PlanningPhase.STAFF_INDEPENDENT_READING,
        ),
        _task(
            name=junior_plan_task,
            role="verification_planner",
            tier=DispatchTier.JUNIOR,
            dispatch_kind=DispatchKind.ADVISORY,
            description=(
                "Generate non-exhaustive verification hypotheses: explicit claims, evidence "
                "pointers, suggested invariants and oracles, adversarial cases, affected seams, "
                "uncertainties, and disagreements. Never narrow the raw contract."
            ),
            success_criteria=(
                "Hypotheses and adversarial cases are explicitly non-exhaustive.",
                "Uncertainty and disagreements are preserved.",
                "No senior concern is deleted or reduced.",
            ),
            blocked_by=(senior_reading_task,),
            planning_phase=PlanningPhase.JUNIOR_VERIFICATION_PLAN,
        ),
        _task(
            name=implementation_task,
            role="implementer",
            tier=DispatchTier.SENIOR,
            dispatch_kind=DispatchKind.CODE,
            description=(
                "Implement the smallest safe change that satisfies the saga goal above. "
                "Own the final implementation and verification plan: reconcile the independent "
                "reading with the junior's non-exhaustive hypotheses, record independent "
                "additions, and do not let junior output narrow the raw contract. Do not merge "
                "or deploy."
            ),
            success_criteria=(
                "The change is scoped to the requested behavior.",
                "Relevant verification commands are run or clearly reported.",
                "No merge, deploy, purchase, or external communication is performed.",
            ),
            blocked_by=(senior_reading_task, junior_plan_task),
            worktree_group=worktree_group,
            purpose=TaskPurpose.IMPLEMENTATION,
            planning_phase=PlanningPhase.SENIOR_OWNED_PLAN,
        ),
        _task(
            name=f"{prefix}_staff_review",
            role="reviewer",
            tier=DispatchTier.STAFF,
            dispatch_kind=DispatchKind.CODE,
            description=(
                "Review the implementation worktree for correctness, missing tests, "
                "approval needs, and residual risks."
            ),
            success_criteria=(
                "A clear approve/block verdict is recorded.",
                "Verification and residual risks are explicit.",
                "The review does not mutate the worktree.",
            ),
            blocked_by=(staff_reading_task, implementation_task),
            worktree_group=worktree_group,
            purpose=TaskPurpose.REVIEW,
            planning_phase=PlanningPhase.STAFF_FINAL_REVIEW,
        ),
    )


def _senior_advisory_plan(prefix: str, prompt: str) -> tuple[PowWowTaskSpec, ...]:
    context_task = f"{prefix}_junior_context"
    return (
        _task(
            name=context_task,
            role="junior_context",
            tier=DispatchTier.JUNIOR,
            dispatch_kind=DispatchKind.ADVISORY,
            description=f"Extract facts, constraints, and open questions for: {prompt}",
            success_criteria=("Facts, constraints, and uncertainty are separated.",),
        ),
        _task(
            name=f"{prefix}_senior_synthesis",
            role="senior_synthesis",
            tier=DispatchTier.SENIOR,
            dispatch_kind=DispatchKind.ADVISORY,
            description=f"Synthesize an actionable answer for: {prompt}",
            success_criteria=("The answer is actionable and bounded by the evidence.",),
            blocked_by=(context_task,),
        ),
    )


def _staff_advisory_plan(prefix: str, prompt: str) -> tuple[PowWowTaskSpec, ...]:
    junior_task = f"{prefix}_junior_context"
    senior_task = f"{prefix}_senior_synthesis"
    return (
        _task(
            name=junior_task,
            role="junior_context",
            tier=DispatchTier.JUNIOR,
            dispatch_kind=DispatchKind.ADVISORY,
            description=f"Extract context and candidate options for: {prompt}",
            success_criteria=("Candidate options and uncertainty are explicit.",),
        ),
        _task(
            name=senior_task,
            role="senior_synthesis",
            tier=DispatchTier.SENIOR,
            dispatch_kind=DispatchKind.ADVISORY,
            description=f"Evaluate tradeoffs and prepare a recommendation for: {prompt}",
            success_criteria=("Tradeoffs and recommendation are explicit.",),
            blocked_by=(junior_task,),
        ),
        _task(
            name=f"{prefix}_staff_verdict",
            role="reviewer",
            tier=DispatchTier.STAFF,
            dispatch_kind=DispatchKind.ADVISORY,
            description=f"Give the final staff-level verdict for: {prompt}",
            success_criteria=("The final verdict is direct and names residual risk.",),
            blocked_by=(senior_task,),
        ),
    )


def _task(
    *,
    name: str,
    role: str,
    tier: DispatchTier,
    dispatch_kind: DispatchKind,
    description: str,
    success_criteria: tuple[str, ...],
    blocked_by: tuple[str, ...] = (),
    worktree_group: str | None = None,
    purpose: TaskPurpose | None = None,
    planning_phase: PlanningPhase | None = None,
    reference_packs: tuple[ReferencePack, ...] = (),
) -> PowWowTaskSpec:
    return PowWowTaskSpec(
        task_name=name,
        role=role,
        description=description,
        success_criteria=success_criteria,
        judgment=JudgmentRole(name=role, tier=tier),
        purpose=purpose,
        dispatch_kind=dispatch_kind,
        blocked_by=blocked_by,
        worktree_group=worktree_group,
        planning_phase=planning_phase,
        reference_packs=reference_packs,
    )


def _mini_gawd_for_plan(
    *,
    intent_id: str,
    kind: DispatchKind,
    prompt: str,
    target_project: LinkedProject,
    rationale: str,
    tasks: Sequence[PowWowTaskSpec],
) -> MiniGawdDoc:
    task_names = ", ".join(task.task_name for task in tasks)
    if kind is DispatchKind.CODE:
        time_budget = (
            TimeBudgetPhase(
                phase="scope",
                hours="0.25-0.5h",
                deliverable="mini-GAWD and task DAG recorded",
            ),
            TimeBudgetPhase(
                phase="implementation",
                hours="0.5-1.5h",
                deliverable="senior implementation task completed in an isolated worktree",
            ),
            TimeBudgetPhase(
                phase="review",
                hours="0.25-0.75h",
                deliverable="staff verdict and verification notes recorded",
            ),
        )
        in_scope = (
            "Bound the requested code change before execution.",
            "Run implementation in a code worktree through the tiered executor.",
            "Record review, verification, artifacts, and approval needs in the ledger.",
        )
        non_goals = (
            "No automatic merge, deploy, purchase, or external communication.",
            "No broad refactor outside the requested intent.",
            "No full 15-section GAWD document for this scoped dispatch.",
        )
        verification = (
            "Executor captures changed files and verification command output.",
            "Staff review produces an approve/block verdict.",
            "Dispatch intent resolves DONE or FAILED with ledger artifacts.",
        )
    else:
        time_budget = (
            TimeBudgetPhase(
                phase="scope",
                hours="0.1-0.25h",
                deliverable="mini-GAWD and advisory task DAG recorded",
            ),
            TimeBudgetPhase(
                phase="analysis",
                hours="0.25-0.75h",
                deliverable="bounded advisory answer or verdict recorded",
            ),
        )
        in_scope = (
            "Answer or evaluate the requested advisory intent.",
            "Use tiered tasks only where decomposition adds useful review or context.",
            "Record outputs and dependency context in the ledger.",
        )
        non_goals = (
            "No file edits for advisory tasks.",
            "No merge, deploy, purchase, or external communication.",
            "No full 15-section GAWD document for this scoped dispatch.",
        )
        verification = (
            "Advisory output is non-empty and captured as an artifact.",
            "Dispatch intent resolves DONE or FAILED with the result payload.",
        )
    return MiniGawdDoc(
        project=target_project.id,
        status="MVP",
        date=_today(),
        time_budget=time_budget,
        theory=(
            "Durable task-DAG dispatch: one operator intent becomes scoped design "
            "guardrails plus schedulable tiered work."
        ),
        why=(
            "Prevent intent drift and unbounded delegation while preserving fast execution "
            f"for: {prompt}"
        ),
        golden_flow=(
            "Claim one pending dispatch intent.",
            "Record mini-GAWD scope and a validated task DAG.",
            "Run tasks when blocked_by dependencies complete.",
            "Persist artifacts, verdicts, and terminal dispatch status.",
        ),
        in_scope=in_scope,
        non_goals=non_goals,
        unit_of_work=(
            f"Dispatch intent {intent_id}: one decomposition_plan.v1 containing mini-GAWD "
            f"and task specs [{task_names}]."
        ),
        lifecycle=(
            "dispatch intent: PENDING -> CLAIMED -> DONE|FAILED",
            "task: CLAIMED -> COMPLETED|FAILED with blocked dependents marked blocked",
            "pow-wow: created -> executor run -> COMPLETED|FAILED|BLOCKED",
        ),
        data_model=(
            "dispatch_intents stores queue state and terminal result.",
            "sagas, pow_wows, and saga_tasks store durable execution scope.",
            "task_artifacts store mini-GAWD, run captures, outputs, and review verdicts.",
        ),
        failure_that_matters=(
            "The dangerous failure is scope drift into irreversible action. Recovery is "
            "worktree isolation, no auto-merge, explicit approval gates, and FAILED/BLOCKED "
            "ledger state instead of silent continuation."
        ),
        verification=verification,
        decisions=(
            MiniGawdDecision(
                decision_id="D1",
                decision="Keep mini-GAWD as planner artifact, not an extra execution stage.",
                rationale="Adds scope guardrails without another agent hop or latency source.",
                date=_today(),
            ),
            MiniGawdDecision(
                decision_id="D2",
                decision="Use blocked_by as both schedule edge and dependency context.",
                rationale="Makes ordering and downstream prompt evidence durable.",
                date=_today(),
            ),
            MiniGawdDecision(
                decision_id="D3",
                decision="Defer the full 15-section GAWD structure.",
                rationale="The dispatch unit needs bounded correctness, not process weight.",
                date=_today(),
            ),
        ),
        deferred=(
            "Full GAWD document generation for large multi-session sagas.",
            "Operator-editable mini-GAWD approval before dispatch.",
            "Planner quality metrics across alternate decomposition strategies.",
        ),
    )


def _task_prefix(intent_id: str) -> str:
    cleaned = _clean_task_name(intent_id)[:8]
    return f"dispatch_{cleaned or 'intent'}"


def _clean_task_name(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", raw.strip().lower()).strip("_")
    return re.sub(r"_+", "_", cleaned)


def _string_tuple(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if not isinstance(raw, Sequence):
        raise DecompositionError(f"expected string list, got {type(raw).__name__}")
    return tuple(str(value) for value in raw if str(value).strip())


def _required_str(payload: Mapping[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise DecompositionError(f"missing required mini_gawd field: {key}")
    return value


def _time_budget_tuple(raw: Any) -> tuple[TimeBudgetPhase, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise DecompositionError("mini_gawd.time_budget must be an array")
    phases: list[TimeBudgetPhase] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise DecompositionError("mini_gawd.time_budget entries must be objects")
        phases.append(
            TimeBudgetPhase(
                phase=_required_str(item, "phase"),
                hours=_required_str(item, "hours"),
                deliverable=_required_str(item, "deliverable"),
            )
        )
    if not phases:
        raise DecompositionError("mini_gawd.time_budget must not be empty")
    return tuple(phases)


def _decision_tuple(raw: Any) -> tuple[MiniGawdDecision, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise DecompositionError("mini_gawd.decision_log must be an array")
    decisions: list[MiniGawdDecision] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise DecompositionError("mini_gawd.decision_log entries must be objects")
        decisions.append(
            MiniGawdDecision(
                decision_id=str(item.get("decision_id") or f"D{index}"),
                decision=_required_str(item, "decision"),
                rationale=_required_str(item, "rationale"),
                date=str(item.get("date") or _today()),
            )
        )
    if not decisions:
        raise DecompositionError("mini_gawd.decision_log must not be empty")
    return tuple(decisions)


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _validate_task_dag(tasks: Sequence[PowWowTaskSpec]) -> None:
    if not tasks:
        raise DecompositionError("decomposition must produce at least one task")
    names = [task.task_name for task in tasks]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise DecompositionError(f"duplicate task names: {', '.join(duplicates)}")
    by_name = {task.task_name: task for task in tasks}
    for task in tasks:
        if not task.description.strip():
            raise DecompositionError(f"task {task.task_name} is missing a description")
        missing = [name for name in task.blocked_by if name not in by_name]
        if missing:
            raise DecompositionError(
                f"task {task.task_name} references missing dependencies: {', '.join(missing)}"
            )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise DecompositionError(f"dependency cycle includes task {name}")
        visiting.add(name)
        for dep in by_name[name].blocked_by:
            visit(dep)
        visiting.remove(name)
        visited.add(name)

    for name in names:
        visit(name)
    if any(task.planning_phase is not None for task in tasks):
        try:
            validate_planning_visibility_contract(tasks, required=True)
        except PlanningContractError as exc:
            raise DecompositionError(str(exc)) from exc


def plan_to_jsonable_dict(plan: DecompositionPlan) -> dict[str, Any]:
    """Compatibility helper for callers that need pure built-in containers."""
    return plan.to_payload()
