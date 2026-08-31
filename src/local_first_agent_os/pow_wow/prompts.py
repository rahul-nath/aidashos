# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Prompt-view construction for pow-wow agent tasks.

This module is the only place that turns durable task and artifact data into a
bounded model-facing view. The underlying ledger payloads are never truncated.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..constants import CLI_AGENT_RUN_ARTIFACT_TYPE, DELEGATED_TASK_RUN_ARTIFACT_TYPE
from ..coordination.contracts import DispatchKind
from ..engineering_doctrine import CURRENT_ENGINEERING_DOCTRINE
from ..marketing_site_doctrine import CURRENT_MARKETING_SITE_DOCTRINE
from ..vocabulary import DispatchTier
from .protocol import PlanningPhase, ReferencePack
from .repo_audit import AUDIT_EMISSION_INSTRUCTION
from .types import PowWowExecutionContext, PowWowTaskResult, PowWowTaskSpec
from .views import ViewCompactor, build_bounded_view_block

_DEPENDENCY_OUTPUT_CHAR_LIMIT = 2500
_DEPENDENCY_CONTEXT_CHAR_LIMIT = 12000

FRONTIER_CONTEXT_DISCIPLINE = """Context/token discipline:
- Start with a focused repo audit. Use file search before reading large files.
- Read only the files needed for the current phase.
- Do not paste long file contents into the response.
- Summarize findings with file paths and line references.
- Keep a compact running state: goals, decisions, files touched, commands run, blockers.
- Before broad exploration, state what you are looking for and why.
- Prefer small, verifiable increments over large speculative rewrites.
- When tests fail, report the smallest relevant error snippet, not the full log.
- If context is getting large, write a short checkpoint summary with:
  1. current objective,
  2. completed steps,
  3. key files,
  4. commands/results,
  5. next action.
- Do not re-read unchanged files unless needed.
- Do not re-litigate accepted architecture unless there is a concrete contradiction in the repo."""


SEATED_AGENT_PARALLELISM = """Parallelism is yours to decide:
- Exactly one agent is seated for your tier. That is a statement about quality,
  not about throughput: the bench declares which model is good enough to hold
  this seat, and two agents of a lower tier do not add up to one of a higher.
- If this task genuinely divides - independent changes over disjoint files, or
  several files to read that do not inform each other - spawn your own
  subagents and run those parts concurrently. You can see the file set and the
  staffing file cannot, so the decision belongs to you rather than to a
  capacity number written weeks ago.
- Do not split work whose parts share a file, or where one part's outcome
  changes what another should do. Sequential is correct there, and a merge
  conflict inside your own worktree costs more than the wall-clock saved.
- Subagents you spawn inherit the permissions this process was granted. They
  are a way to use your envelope in parallel, never a way to widen it: if an
  action is refused to you, it is refused to them."""


def _render_dependency_text_view(
    value: Any,
    *,
    source: str,
    limit: int = _DEPENDENCY_OUTPUT_CHAR_LIMIT,
) -> str:
    return build_bounded_view_block(
        source=source,
        content=str(value),
        char_limit=limit,
    ).render()


def _render_task_result_output_fragments(task_result: PowWowTaskResult) -> tuple[str, ...]:
    fragments: list[str] = []
    for artifact in task_result.artifacts:
        content = artifact.content
        if artifact.artifact_type == DELEGATED_TASK_RUN_ARTIFACT_TYPE:
            output = content.get("output")
            if output:
                source = f"{task_result.task_name}:delegated_task_run:output"
                fragments.append(
                    f"delegated_task_run output:\n"
                    f"{_render_dependency_text_view(output, source=source)}"
                )
            error = content.get("error")
            if error:
                source = f"{task_result.task_name}:delegated_task_run:error"
                fragments.append(
                    f"delegated_task_run error:\n"
                    f"{_render_dependency_text_view(error, source=source)}"
                )
            continue
        if artifact.artifact_type == CLI_AGENT_RUN_ARTIFACT_TYPE:
            output = content.get("verdict") or content.get("output")
            if output:
                source = f"{task_result.task_name}:cli_agent_run:output"
                fragments.append(
                    f"cli_agent_run output:\n{_render_dependency_text_view(output, source=source)}"
                )
            continue
        if artifact.artifact_type == "browser_acceptance":
            source = f"{task_result.task_name}:browser_acceptance:evidence"
            fragments.append(
                "browser_acceptance evidence:\n"
                f"{_render_dependency_text_view(content, source=source)}"
            )
    return tuple(fragments)


def render_dependency_context_block(
    dependency_results: Sequence[PowWowTaskResult],
    *,
    compactor: ViewCompactor | None = None,
) -> str:
    """Everything the finished dependencies said, bounded to one block.

    This is the one view in the prompt assembled from N upstream tasks, so it is
    the one whose overflow is systematically lopsided: the tail of the block is
    the most recent dependency, and truncating it hands the agent the earliest
    task's output in full and the one it most likely builds on not at all.
    ``compactor`` is what buys a summary of all of them instead, and stays
    optional so that this module still imports and renders with no model
    anywhere in reach.
    """

    if not dependency_results:
        return ""
    lines = ["Completed dependency outputs:"]
    for result in dependency_results:
        lines.extend(
            [
                f"## {result.task_name}",
                f"Status: {result.status}",
                f"Summary: {result.summary}",
            ]
        )
        if result.changed_files:
            lines.append(f"Changed files: {', '.join(result.changed_files)}")
        if result.risks:
            lines.append(f"Risks: {'; '.join(result.risks)}")
        lines.extend(_render_task_result_output_fragments(result))
    return build_bounded_view_block(
        source="pow_wow_dependency_context",
        content="\n".join(lines),
        char_limit=_DEPENDENCY_CONTEXT_CHAR_LIMIT,
        compactor=compactor,
    ).render()


def _uses_frontier_context_discipline(task: PowWowTaskSpec) -> bool:
    return task.judgment is not None and task.judgment.tier in {
        DispatchTier.SENIOR,
        DispatchTier.STAFF,
    }


def _uses_engineering_doctrine(task: PowWowTaskSpec) -> bool:
    return task.judgment is not None and task.judgment.tier in {
        DispatchTier.SENIOR,
        DispatchTier.STAFF,
    }


def build_agent_task_prompt(
    task: PowWowTaskSpec,
    context: PowWowExecutionContext,
    *,
    dependency_results: Sequence[PowWowTaskResult] = (),
    dependency_compactor: ViewCompactor | None = None,
    audit_context_block: str = "",
) -> str:
    # Ordered stable-prefix-first, and the ordering is load-bearing rather than
    # cosmetic. Prompt caching is keyed on a *prefix*: two dispatches share a
    # cache entry only for the bytes they agree on from the very start. Every
    # senior and staff dispatch opens with the same startup instruction and the
    # same doctrine contract, which is the largest block in the prompt and the
    # one most worth not paying for twice - and none of it could ever be shared,
    # because the role, the goal, and the target project were emitted first and
    # differ on every single dispatch. The prompt diverged at byte zero.
    #
    # So the blocks are laid out by decreasing universality: what every task
    # gets, then what every senior and staff task gets, then what only staff
    # gets, and so on. `_uses_engineering_doctrine` and
    # `_uses_frontier_context_discipline` are the same predicate, so those two
    # sit together ahead of the staff-only line; splitting them around it would
    # cut the shared senior/staff prefix down to the doctrine alone.
    #
    # The task itself, its criteria, and the constraints stay last. Recency is
    # what an instruction needs, and nothing about caching is worth trading it
    # for.
    lines = [
        "Startup:",
        (
            "If you have filesystem access and this repo contains "
            "skills/agent-startup/SKILL.md, read it before acting. Follow its architecture "
            "boundaries, subagent semantics, and validation expectations."
        ),
    ]
    if _uses_engineering_doctrine(task):
        lines.extend(("", CURRENT_ENGINEERING_DOCTRINE.render_prompt()))
    if _uses_frontier_context_discipline(task):
        lines.extend(("", FRONTIER_CONTEXT_DISCIPLINE))
        # Same predicate, emitted adjacently on purpose: both blocks are shared
        # by every senior and staff dispatch, so keeping them together keeps the
        # cacheable prefix one contiguous run.
        lines.extend(("", SEATED_AGENT_PARALLELISM))
    if (
        _uses_engineering_doctrine(task)
        and task.judgment
        and task.judgment.tier is DispatchTier.STAFF
    ):
        lines.extend(
            (
                "",
                (
                    "Staff doctrine enforcement: concrete violations of this contract may "
                    "BLOCK approval. Name the violated rule and the specific code, contract, "
                    "or invariant. Do not BLOCK on an unsupported style preference."
                ),
            )
        )
    if (
        task.judgment is not None
        and task.judgment.tier in {DispatchTier.SENIOR, DispatchTier.STAFF}
        and ReferencePack.MARKETING_SITE in task.reference_packs
    ):
        lines.extend(("", CURRENT_MARKETING_SITE_DOCTRINE.render_prompt()))
        if task.judgment.tier is DispatchTier.STAFF:
            lines.extend(
                (
                    "",
                    (
                        "Staff marketing-site enforcement: BLOCK unsupported business claims, "
                        "competitor leakage, missing source provenance, or missing rendered "
                        "desktop/mobile evidence. Do not approve from generated HTML alone."
                    ),
                )
            )
    if task.planning_phase is not None:
        lines.extend(("", _render_planning_phase_contract(task.planning_phase)))
    lines.extend(
        (
            "",
            f"You are the {task.role} for pow-wow task '{task.task_name}'.",
            f"Saga goal: {context.goal}",
            f"Target project: {context.target_project_id} at {context.target_project_path}",
        )
    )
    lines.extend(("Task:", task.description))
    if context.reuse_checkpoint_worktree and context.checkpoint_worktree_path:
        lines.append(
            "Interrupted-attempt recovery: continue the existing durable changes in this "
            "retained worktree. Inspect and finish them in place; do not recreate work that "
            "is already present."
        )
    if task.success_criteria:
        lines.append("Success criteria:")
        lines.extend(f"- {criterion}" for criterion in task.success_criteria)
    dependency_block = render_dependency_context_block(
        dependency_results,
        compactor=dependency_compactor,
    )
    if dependency_block:
        lines.append(dependency_block)
    # The predecessor's partitioned repository audit sits beside the dependency
    # context: both are host-supplied evidence about prior work, and both vary
    # per dispatch, so neither may precede the stable cacheable blocks above.
    # The caller decides whether a block exists at all; review tasks never get
    # one, because reviewer independence is a visibility rule, not a style.
    if audit_context_block:
        lines.append(audit_context_block)
    dispatch_kind = task.dispatch_kind or context.dispatch_kind
    if dispatch_kind is DispatchKind.CODE:
        constraints = (
            "Constraints: work only inside the assigned worktree; make the minimal necessary "
            "change. Do NOT merge, rebase, switch branches, push, or deploy. The control plane "
            "creates a branch checkpoint commit after required verification passes. Stop when "
            "the task is complete."
        )
    else:
        constraints = (
            "Constraints: advisory task only. Do not edit files, merge, push, deploy, purchase, "
            "or send external communications. Stop when the answer or verdict is complete."
        )
    lines.append(constraints)
    return "\n".join(lines)


def build_resumed_senior_implementation_prompt(
    task: PowWowTaskSpec,
    context: PowWowExecutionContext,
    *,
    dependency_results: Sequence[PowWowTaskResult] = (),
    dependency_compactor: ViewCompactor | None = None,
) -> str:
    """The bounded next turn for a senior reader thread becoming implementer."""

    if task.planning_phase is not PlanningPhase.SENIOR_OWNED_PLAN:
        raise ValueError("only senior_owned_plan may resume the senior reading thread")
    reading_results = tuple(
        result
        for result in dependency_results
        if any(
            artifact.schema_version == "planning_evidence.v1"
            and artifact.artifact_type == "senior_independent_reading"
            and artifact.persisted_artifact_id is not None
            for artifact in result.artifacts
        )
    )
    if len(reading_results) != 1:
        raise ValueError(
            "resumed senior implementation requires exactly one persisted "
            "senior_independent_reading artifact"
        )
    reading_result = reading_results[0]
    reading_artifact = next(
        artifact
        for artifact in reading_result.artifacts
        if artifact.schema_version == "planning_evidence.v1"
        and artifact.artifact_type == "senior_independent_reading"
        and artifact.persisted_artifact_id is not None
    )
    reading_output = reading_artifact.content.get("model_output")
    if not isinstance(reading_output, str) or not reading_output.strip():
        raise ValueError("persisted senior reading evidence requires non-empty model_output")
    lines = [
        "Continuation transition: senior_independent_reading -> senior_owned_plan.",
        (
            "Your independent repository reading and its reasoning are already in this Codex "
            "thread. Do not repeat that exploration. The persisted reading below remains typed, "
            "disputable evidence rather than an assumed premise. Re-check and revise its claims "
            "as needed while reconciling them with the new dependency evidence."
        ),
        (
            "The host has rebound this process to the assigned implementation worktree at "
            f"{context.target_project_path}. Treat that path and the current process sandbox as "
            "authoritative; do not edit the earlier read-only checkout."
        ),
        _render_planning_phase_contract(PlanningPhase.SENIOR_OWNED_PLAN),
        f"You are the {task.role} for pow-wow task '{task.task_name}'.",
        "The saga goal and engineering doctrine are unchanged from the prior turn.",
        "Task:",
        task.description,
        "Persisted senior independent-reading evidence (planning_evidence.v1):",
        _render_dependency_text_view(
            reading_output,
            source=(
                f"{reading_result.task_name}:planning_evidence.v1:"
                f"{reading_artifact.persisted_artifact_id}"
            ),
        ),
    ]
    if task.success_criteria:
        lines.append("Success criteria:")
        lines.extend(f"- {criterion}" for criterion in task.success_criteria)
    dependency_block = render_dependency_context_block(
        tuple(result for result in dependency_results if result is not reading_result),
        compactor=dependency_compactor,
    )
    if dependency_block:
        lines.append(dependency_block)
    lines.append(
        "Constraints: work only inside the assigned worktree; make the minimal necessary "
        "change. Do NOT merge, rebase, switch branches, push, or deploy. The control plane "
        "creates a branch checkpoint commit after required verification passes. Stop when "
        "the task is complete."
    )
    return "\n".join(lines)


def _render_planning_phase_contract(phase: PlanningPhase) -> str:
    common = (
        f"Planning visibility contract: {phase.value}. This phase is host-validated and its "
        "output is persisted as planning_evidence.v1 before any dependent model starts."
    )
    if phase is PlanningPhase.SENIOR_INDEPENDENT_READING:
        return (
            f"{common}\nRead the raw saga goal and repository independently. No junior conclusion "
            "is visible in this turn. Record source anchors, repository evidence, invariants, "
            "oracles, affected seams, risks, and uncertainty.\n"
            f"{AUDIT_EMISSION_INSTRUCTION}"
        )
    if phase is PlanningPhase.JUNIOR_VERIFICATION_PLAN:
        return (
            f"{common}\nYour output is non-exhaustive hypothesis generation only. It cannot define "
            "the complete problem boundary, delete a senior concern, decide promotion, or narrow "
            "the raw contract. Preserve uncertainty and disagreements."
        )
    if phase is PlanningPhase.SENIOR_OWNED_PLAN:
        return (
            f"{common}\nYou own the final implementation and verification plan. "
            "Treat junior output "
            "as non-exhaustive hypotheses, reconcile it with your persisted independent reading, "
            "and explicitly record your additions beyond it."
        )
    if phase is PlanningPhase.STAFF_INDEPENDENT_READING:
        return (
            f"{common}\nRead the raw saga goal and repository independently. No junior or senior "
            "conclusion is visible in this turn. Record the acceptance boundary and review "
            "oracles.\n"
            f"{AUDIT_EMISSION_INSTRUCTION}"
        )
    return (
        f"{common}\nYou retain final staff responsibility. Use your persisted independent reading, "
        "read the raw contract again as needed, and accept, reject, or extend the "
        "senior-owned plan."
    )


def build_assigned_worktree_context(
    context: PowWowExecutionContext,
    worktree_path: Path,
) -> PowWowExecutionContext:
    return replace(context, target_project_path=str(worktree_path))


def build_assigned_worktree_environment(context: PowWowExecutionContext) -> dict[str, str]:
    return {
        "LOCAL_AGENT_ASSIGNED_WORKTREE": context.target_project_path,
        "LOCAL_AGENT_CONTEXT_JSON": json.dumps(context.to_payload(), sort_keys=True),
    }


__all__ = [
    "FRONTIER_CONTEXT_DISCIPLINE",
    "SEATED_AGENT_PARALLELISM",
    "build_assigned_worktree_context",
    "build_assigned_worktree_environment",
    "build_agent_task_prompt",
    "build_resumed_senior_implementation_prompt",
    "render_dependency_context_block",
]
