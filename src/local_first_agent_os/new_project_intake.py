# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""File-first GAWD intake for starting new build sagas.

This module keeps the intake contract as data:

* a human-editable sparse mini-GAWD draft file;
* a parsed sparse draft;
* a permission envelope;
* a finalized draft artifact ready for operator approval.

The workflow layer owns ledger writes and executor selection. This module owns
only the local file contract and deterministic finalization shape.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from .archetype_planners import ArchetypeMilestoneTemplate, plan_saas_archetype
from .constants import CLI_AGENT_RUN_ARTIFACT_TYPE, DELEGATED_TASK_RUN_ARTIFACT_TYPE
from .pow_wow import PowWowRunResult, PowWowTaskSpec
from .staffing import JudgmentRole, Tier

SCHEMA_VERSION_SPARSE_GAWD_DRAFT = "sparse_gawd_draft.v1"
SCHEMA_VERSION_PERMISSION_ENVELOPE = "permission_envelope.v1"
SCHEMA_VERSION_FINALIZED_GAWD_DRAFT = "finalized_gawd_draft.v1"
SCHEMA_VERSION_DURABLE_WORKFLOW_PLAN = "durable_workflow_plan.v1"

DEFAULT_GAWD_DRAFT_DIR = Path("docs/gawd_drafts")
DURABLE_WORKFLOW_PLAN_CONTRACT_PATH = Path("configs/durable_workflow_plan.toml")


@dataclass(frozen=True)
class SparseGawdDraft:
    draft_id: str
    source_path: str
    project: str
    goal: str
    raw_text: str
    theory: str = ""
    why: str = ""
    golden_flow: tuple[str, ...] = ()
    in_scope: tuple[str, ...] = ()
    non_goals: tuple[str, ...] = ()
    unit_of_work: str = ""
    lifecycle: tuple[str, ...] = ()
    data_model: tuple[str, ...] = ()
    failure_that_matters: str = ""
    verification: tuple[str, ...] = ()
    execution_milestones: tuple[str, ...] = ()
    service_levels: tuple[str, ...] = ()
    input_bounds: tuple[str, ...] = ()
    interface_contracts: tuple[str, ...] = ()
    idempotency_replay: tuple[str, ...] = ()
    observability: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    security_access: tuple[str, ...] = ()
    backpressure_cost: tuple[str, ...] = ()
    rollout_migration_rollback: tuple[str, ...] = ()
    risk_synthesis: tuple[str, ...] = ()
    known_limitations: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()
    unresolved_questions: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION_SPARSE_GAWD_DRAFT

    def constraints(self) -> tuple[str, ...]:
        values = [
            "Priority order: correctness > stability > debuggability > throughput > latency",
            *(f"In scope: {item}" for item in self.in_scope),
            *(f"Non-goal: {item}" for item in self.non_goals),
        ]
        if self.failure_that_matters:
            values.append(f"Failure to guard: {self.failure_that_matters}")
        return tuple(values)

    def success_criteria(self) -> tuple[str, ...]:
        values = list(self.verification)
        if not values:
            values.extend(self.golden_flow)
        if not values:
            values.append("Finalized GAWD draft and permission envelope are recorded.")
        return tuple(values)

    def acceptance_criteria(self) -> tuple[str, ...]:
        values = list(self.golden_flow)
        values.extend(self.verification)
        return tuple(dict.fromkeys(values)) or self.success_criteria()

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PermissionRequest:
    permission: str
    reason: str
    required_before_execution: bool = True

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PermissionEnvelope:
    autonomous_permissions: tuple[str, ...]
    requested_permissions: tuple[PermissionRequest, ...]
    denied_without_approval: tuple[str, ...]
    risks: tuple[str, ...]
    schema_version: str = SCHEMA_VERSION_PERMISSION_ENVELOPE

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["requested_permissions"] = [
            permission.to_payload() for permission in self.requested_permissions
        ]
        return payload


@dataclass(frozen=True)
class FinalizedGawdDraft:
    draft: SparseGawdDraft
    permission_envelope: PermissionEnvelope
    final_markdown: str
    schema_version: str = SCHEMA_VERSION_FINALIZED_GAWD_DRAFT

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "draft": self.draft.to_payload(),
            "permission_envelope": self.permission_envelope.to_payload(),
            "final_markdown": self.final_markdown,
        }


@dataclass(frozen=True)
class DurableWorkflowMilestone:
    milestone_id: str
    name: str
    happy_path_step: str
    source_index: int

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DurableWorkflowDerivationRule:
    source_section: str
    owner: str
    produces: tuple[str, ...]
    instruction: str

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DurableWorkflowStep:
    step_id: str
    name: str
    milestone_id: str
    source_sections: tuple[str, ...]
    durable_boundary_reason: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    side_effects: tuple[str, ...]
    idempotency_key: str
    retry_policy: str
    timeout_policy: str
    compensation_or_rollback: str
    approval_required: bool
    evidence_to_record: tuple[str, ...]
    derived_by: str
    # Which lifecycle phase this step is. `IMPLEMENT` is the default because it
    # is what a scaffolded step is until something with the document in hand says
    # otherwise, and because the phase decides which artifact the milestone must
    # produce - so a wrong one fails a milestone rather than mislabelling it.
    phase: str = "IMPLEMENT"

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DurableWorkflowPlan:
    draft_id: str
    project: str
    source_draft_path: str
    contract_path: str
    milestones: tuple[DurableWorkflowMilestone, ...]
    derivation_rules: tuple[DurableWorkflowDerivationRule, ...]
    steps: tuple[DurableWorkflowStep, ...]
    permission_envelope: PermissionEnvelope
    approval_boundary: str
    code_generation_policy: str
    schema_version: str = SCHEMA_VERSION_DURABLE_WORKFLOW_PLAN

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "draft_id": self.draft_id,
            "project": self.project,
            "source_draft_path": self.source_draft_path,
            "contract_path": self.contract_path,
            "milestones": [milestone.to_payload() for milestone in self.milestones],
            "derivation_rules": [rule.to_payload() for rule in self.derivation_rules],
            "steps": [step.to_payload() for step in self.steps],
            "permission_envelope": self.permission_envelope.to_payload(),
            "approval_boundary": self.approval_boundary,
            "code_generation_policy": self.code_generation_policy,
        }


@dataclass(frozen=True)
class DraftFile:
    draft_id: str
    path: Path
    next_command: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft_id,
            "path": str(self.path),
            "next_command": self.next_command,
        }


def create_sparse_gawd_draft_file(
    repo_root: Path,
    *,
    created_at: datetime | None = None,
) -> DraftFile:
    created_at = created_at or datetime.now(UTC)
    draft_id = uuid4().hex
    draft_dir = repo_root / DEFAULT_GAWD_DRAFT_DIR
    draft_dir.mkdir(parents=True, exist_ok=True)
    path = draft_dir / f"gawd_doc_{draft_id}.txt"
    path.write_text(_sparse_gawd_template(draft_id, created_at), encoding="utf-8")
    return DraftFile(
        draft_id=draft_id,
        path=path,
        next_command=f"pi /start /new-project {path}",
    )


def parse_sparse_gawd_draft(path: Path) -> SparseGawdDraft:
    source_path = path.expanduser().resolve()
    raw_text = source_path.read_text(encoding="utf-8")
    sections = _sections(raw_text)
    project = _project_name(raw_text) or _clean_title(source_path.stem)
    why = _section_body(sections, "why this exists")
    theory = _section_body(sections, "theory of the system")
    core_design = _section_body(sections, "core design")
    scope = _section_body(sections, "this version scope non goals")
    operational_contract = _section_body(sections, "operational contract")
    risk = _section_body(sections, "risk synthesis known limitations")
    golden_flow = _section_lines(sections, "happy path golden flow")
    in_scope = _label_lines(scope, "in scope")
    non_goals = _label_lines(scope, "cut") or _label_lines(scope, "non goals")
    verification = _section_lines(sections, "verification")
    missing_sections = _missing_sections(sections)
    goal = _first_meaningful_line(why) or _first_meaningful_line(theory) or project

    return SparseGawdDraft(
        draft_id=_parse_draft_id_from_path(source_path),
        source_path=str(source_path),
        project=project,
        goal=goal,
        raw_text=raw_text,
        theory=_plain_text(theory),
        why=_plain_text(why),
        golden_flow=tuple(golden_flow),
        in_scope=tuple(in_scope),
        non_goals=tuple(non_goals),
        unit_of_work=_label_text(core_design, "unit of work"),
        lifecycle=tuple(_label_lines(core_design, "lifecycle")),
        data_model=tuple(_label_lines(core_design, "data model")),
        failure_that_matters=_plain_text(_section_body(sections, "the failure that matters most")),
        verification=tuple(verification),
        execution_milestones=tuple(_section_lines(sections, "execution milestones")),
        service_levels=tuple(_label_lines(operational_contract, "service levels")),
        input_bounds=tuple(_label_lines(operational_contract, "input bounds")),
        interface_contracts=tuple(_label_lines(operational_contract, "interface contracts")),
        idempotency_replay=tuple(_label_lines(operational_contract, "idempotency replay")),
        observability=tuple(_label_lines(operational_contract, "observability")),
        dependencies=tuple(_label_lines(operational_contract, "dependencies")),
        security_access=tuple(_label_lines(operational_contract, "security access")),
        backpressure_cost=tuple(_label_lines(operational_contract, "backpressure cost")),
        rollout_migration_rollback=tuple(_section_lines(sections, "rollout migration rollback")),
        risk_synthesis=tuple(_label_lines(risk, "risk synthesis")),
        known_limitations=tuple(_label_lines(risk, "known limitations")),
        decisions=tuple(_section_lines(sections, "decision log")),
        deferred=tuple(_section_lines(sections, "if i had 2 more weeks")),
        unresolved_questions=tuple(missing_sections),
    )


def build_reviewable_gawd_draft(draft: SparseGawdDraft) -> FinalizedGawdDraft:
    envelope = permission_envelope_for_draft(draft)
    final_markdown = render_gawd_review_markdown(draft, envelope)
    return FinalizedGawdDraft(
        draft=draft,
        permission_envelope=envelope,
        final_markdown=final_markdown,
    )


def build_durable_workflow_plan(
    finalized: FinalizedGawdDraft,
    *,
    contract_path: Path = DURABLE_WORKFLOW_PLAN_CONTRACT_PATH,
) -> DurableWorkflowPlan:
    draft = finalized.draft
    archetype_plan = plan_saas_archetype(draft)
    archetype_templates: tuple[ArchetypeMilestoneTemplate, ...] = ()
    if draft.execution_milestones:
        milestone_source = draft.execution_milestones
        milestones = tuple(
            DurableWorkflowMilestone(
                milestone_id=f"m{index:02d}_{_slugify(step)}",
                name=_clean_title(step),
                happy_path_step=step,
                source_index=index,
            )
            for index, step in enumerate(milestone_source, start=1)
        )
    elif archetype_plan is not None and archetype_plan.milestones:
        archetype_templates = archetype_plan.milestones
        milestones = tuple(
            DurableWorkflowMilestone(
                milestone_id=template.milestone_id,
                name=template.name,
                happy_path_step=template.description,
                source_index=index,
            )
            for index, template in enumerate(archetype_templates, start=1)
        )
    else:
        milestone_source = draft.golden_flow or (
            "Operator approves finalized GAWD doc.",
            "Saga executes approved task graph.",
            "Verification evidence is recorded.",
        )
        milestones = tuple(
            DurableWorkflowMilestone(
                milestone_id=f"m{index:02d}_{_slugify(step)}",
                name=_clean_title(step),
                happy_path_step=step,
                source_index=index,
            )
            for index, step in enumerate(milestone_source, start=1)
        )
    archetype_templates_by_id = {
        template.milestone_id: template for template in archetype_templates
    }
    derivation_rules = (
        *_archetype_derivation_rules(archetype_plan),
        DurableWorkflowDerivationRule(
            source_section="Execution Milestones + SaaS Archetype Planner + Happy Path",
            owner="planner_then_senior_or_staff",
            produces=("milestones",),
            instruction=(
                "Prefer explicit Execution Milestones. If absent and the draft is "
                "SaaS-shaped, use deterministic archetype templates. Otherwise "
                "create ordered milestones from the Happy Path without inventing "
                "extra product scope."
            ),
        ),
        DurableWorkflowDerivationRule(
            source_section="Core Design",
            owner="senior_or_staff",
            produces=("inputs", "outputs", "side_effects", "state_transitions"),
            instruction=(
                "Derive concrete workflow contracts from unit of work, lifecycle, and data model."
            ),
        ),
        DurableWorkflowDerivationRule(
            source_section="Operational Contract",
            owner="senior_or_staff",
            produces=(
                "service_levels",
                "input_bounds",
                "interface_contracts",
                "idempotency_replay",
                "observability",
                "dependencies",
                "security_access",
                "backpressure_cost",
            ),
            instruction=(
                "Expand sparse operator notes into concrete bounds, contracts, "
                "observability, dependency, security, and cost controls that affect "
                "durable workflow boundaries."
            ),
        ),
        DurableWorkflowDerivationRule(
            source_section="The Failure That Matters Most",
            owner="operator_then_senior_or_staff",
            produces=("retry_policy", "timeout_policy", "compensation_or_rollback"),
            instruction=(
                "Formalize the operator's failure mode into retry, timeout, rollback, "
                "or fail-closed behavior."
            ),
        ),
        DurableWorkflowDerivationRule(
            source_section="Verification",
            owner="senior_or_staff",
            produces=("evidence_to_record", "final_smoke_checks"),
            instruction="Turn verification bullets into evidence artifacts and smoke checks.",
        ),
        DurableWorkflowDerivationRule(
            source_section="Rollout / Migration / Rollback",
            owner="senior_or_staff",
            produces=("approval_required", "migration_gates", "rollback_plan"),
            instruction=(
                "Turn rollout, migration, and rollback notes into explicit gates. "
                "Merge, deploy, destructive migration, and data rollback remain "
                "approval-bound unless the operator explicitly says otherwise."
            ),
        ),
        DurableWorkflowDerivationRule(
            source_section="Risk Synthesis / Known Limitations",
            owner="staff",
            produces=("block_conditions", "risk_gates", "revisit_triggers"),
            instruction=(
                "Promote top risks and stated limitations into milestone block "
                "conditions, evidence requirements, or deferred work."
            ),
        ),
        DurableWorkflowDerivationRule(
            source_section="Permission Envelope",
            owner="senior_or_staff",
            produces=("approval_required", "allowed_capabilities", "denied_without_approval"),
            instruction=(
                "Decide which steps can run autonomously and which require explicit "
                "operator approval."
            ),
        ),
    )
    approval_keywords = (
        "merge",
        "deploy",
        "production",
        "release",
        "migration",
        "secret",
        "credential",
        "email",
        "slack",
        "send",
        "purchase",
        "paid",
        "stripe",
        "billing",
    )
    steps = tuple(
        DurableWorkflowStep(
            step_id=f"step_{milestone.milestone_id}",
            name=milestone.name,
            milestone_id=milestone.milestone_id,
            source_sections=_workflow_step_source_sections(
                archetype_templates_by_id.get(milestone.milestone_id)
            ),
            durable_boundary_reason=(
                "GAWD milestone checkpoint. Senior/staff must refine whether this is "
                "a DBOS workflow, DBOS step, approval gate, or grouped subflow using "
                "the full-GAWD operational concerns."
            ),
            inputs=_workflow_step_inputs(
                draft,
                milestone,
                archetype_templates_by_id.get(milestone.milestone_id),
            ),
            outputs=_workflow_step_outputs(
                milestone,
                archetype_templates_by_id.get(milestone.milestone_id),
            ),
            side_effects=_workflow_step_side_effects(
                draft,
                milestone,
                archetype_templates_by_id.get(milestone.milestone_id),
            ),
            idempotency_key=f"{draft.draft_id}:{milestone.milestone_id}",
            retry_policy=_workflow_retry_policy(draft),
            timeout_policy=_workflow_timeout_policy(draft),
            compensation_or_rollback=_workflow_compensation_policy(draft),
            approval_required=_workflow_step_approval_required(
                draft,
                milestone,
                archetype_templates_by_id.get(milestone.milestone_id),
                approval_keywords,
            ),
            evidence_to_record=_workflow_evidence(
                draft,
                milestone,
                archetype_templates_by_id.get(milestone.milestone_id),
            ),
            derived_by="senior_spec_completion_then_staff_final_verdict",
        )
        for milestone in milestones
    )
    return DurableWorkflowPlan(
        draft_id=draft.draft_id,
        project=draft.project,
        source_draft_path=draft.source_path,
        contract_path=str(contract_path),
        milestones=milestones,
        derivation_rules=derivation_rules,
        steps=steps,
        permission_envelope=finalized.permission_envelope,
        approval_boundary=(
            "Operator must approve the finalized GAWD doc, permission envelope, and "
            "durable workflow plan before any generated DBOS workflow code executes."
        ),
        code_generation_policy=(
            "Generate DBOS workflow code only in an isolated worktree or PR with tests. "
            "Do not hot-load generated workflow code into the resident daemon."
        ),
    )


def refine_durable_workflow_plan_from_run_result(
    scaffold: DurableWorkflowPlan,
    run_result: PowWowRunResult,
) -> tuple[DurableWorkflowPlan, dict[str, Any]]:
    """Use senior/staff model output when it contains a valid workflow plan.

    The executor captures model output as artifacts. This parser deliberately
    accepts only a fenced TOML/JSON plan or a whole-output TOML/JSON plan with
    the durable workflow schema. Invalid or absent output keeps the deterministic
    scaffold as the authoritative plan and records why.
    """

    candidates: list[tuple[str, str, str]] = []
    for task in run_result.tasks:
        if task.task_name not in {"senior_spec_completion", "staff_final_verdict"}:
            continue
        for artifact in task.artifacts:
            if artifact.artifact_type != CLI_AGENT_RUN_ARTIFACT_TYPE:
                continue
            output = artifact.content.get("output") or artifact.content.get("verdict")
            if isinstance(output, str) and output.strip():
                candidates.append((task.task_name, artifact.artifact_type, output))

    parse_errors: list[dict[str, str]] = []
    # Prefer staff verdict corrections over senior drafts.
    for task_name, artifact_type, output in sorted(
        candidates,
        key=lambda item: 0 if item[0] == "staff_final_verdict" else 1,
    ):
        for language, raw_payload in _structured_payload_candidates(output):
            try:
                payload = _load_structured_payload(language, raw_payload)
                refined = parse_durable_workflow_plan_payload(payload, fallback=scaffold)
            except (TypeError, ValueError, tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
                parse_errors.append(
                    {
                        "task_name": task_name,
                        "artifact_type": artifact_type,
                        "language": language,
                        "error": str(exc),
                    }
                )
                continue
            return refined, {
                "schema_version": "durable_workflow_plan_model_refinement.v1",
                "status": "model_refined",
                "source_task_name": task_name,
                "source_artifact_type": artifact_type,
                "language": language,
                "parse_errors": parse_errors,
            }

    return scaffold, {
        "schema_version": "durable_workflow_plan_model_refinement.v1",
        "status": "scaffold_used",
        "reason": "No valid senior/staff durable workflow plan was found in model output.",
        "candidate_count": len(candidates),
        "parse_errors": parse_errors,
    }


def parse_durable_workflow_plan_payload(
    payload: Mapping[str, Any],
    *,
    fallback: DurableWorkflowPlan,
) -> DurableWorkflowPlan:
    schema_version = str(payload.get("schema_version") or "")
    if schema_version != SCHEMA_VERSION_DURABLE_WORKFLOW_PLAN:
        raise ValueError(
            "durable workflow plan payload must set "
            f"schema_version={SCHEMA_VERSION_DURABLE_WORKFLOW_PLAN!r}"
        )
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("durable workflow plan payload must contain non-empty steps")
    steps = tuple(_parse_durable_workflow_step(step) for step in raw_steps)
    raw_milestones = payload.get("milestones")
    milestones = (
        tuple(_parse_durable_workflow_milestone(item) for item in raw_milestones)
        if isinstance(raw_milestones, list) and raw_milestones
        else fallback.milestones
    )
    # A milestone without a matching step would later persist with no entry/exit
    # criteria, evidence, or approval gate, so reject the incoherent plan and
    # let the caller fall back to the deterministic scaffold.
    step_milestone_ids = {step.milestone_id for step in steps}
    uncovered = [
        milestone.milestone_id
        for milestone in milestones
        if milestone.milestone_id not in step_milestone_ids
    ]
    if uncovered:
        raise ValueError(
            "durable workflow plan steps do not cover milestones: " + ", ".join(uncovered)
        )
    raw_rules = payload.get("derivation_rules")
    derivation_rules = (
        tuple(_parse_durable_workflow_derivation_rule(item) for item in raw_rules)
        if isinstance(raw_rules, list) and raw_rules
        else fallback.derivation_rules
    )
    return DurableWorkflowPlan(
        draft_id=str(payload.get("draft_id") or fallback.draft_id),
        project=str(payload.get("project") or fallback.project),
        source_draft_path=str(payload.get("source_draft_path") or fallback.source_draft_path),
        contract_path=str(payload.get("contract_path") or fallback.contract_path),
        milestones=milestones,
        derivation_rules=derivation_rules,
        steps=steps,
        permission_envelope=_parse_permission_envelope(
            payload.get("permission_envelope"),
            fallback=fallback.permission_envelope,
        ),
        approval_boundary=str(payload.get("approval_boundary") or fallback.approval_boundary),
        code_generation_policy=str(
            payload.get("code_generation_policy") or fallback.code_generation_policy
        ),
    )


def write_gawd_review_files(finalized: FinalizedGawdDraft) -> tuple[Path, Path]:
    source_path = Path(finalized.draft.source_path)
    # `.md`, because the field being written is called `final_markdown` and is
    # markdown. The `.txt` this used to write was an extension that misdescribed
    # its own contents, and it mattered beyond tidiness: the file an operator is
    # asked to approve is the file the next step compiles, and a reader deciding
    # whether intake produces something runnable should not have to open it to
    # find out what it is.
    finalized_path = source_path.with_name(f"{source_path.stem}.finalized.md")
    permissions_path = source_path.with_name(f"{source_path.stem}.permissions.json")
    finalized_path.write_text(finalized.final_markdown, encoding="utf-8")
    permissions_path.write_text(
        json.dumps(finalized.permission_envelope.to_payload(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return finalized_path, permissions_path


_EXECUTOR_FOR_PHASE: Final = {
    "PLAN": "plan.implementation",
    "IMPLEMENT": "implement.code_change",
    "VERIFY": "verify.tests",
    "REVIEW": "review.operator",
    "DELIVER": "deliver.artifact",
}

_ARTIFACT_FOR_PHASE: Final = {
    "PLAN": "implementation_plan",
    "IMPLEMENT": "source_patch",
    "VERIFY": "test_result",
    "REVIEW": "operator_approval",
    "DELIVER": "delivery_record",
}


def replace_execution_milestones_section(markdown: str, rendered: str) -> str:
    """Put the rendered milestones under section 8, replacing what was there.

    The sparse section holds the operator's own bullets or the template's
    guidance, and both are prose the compiler cannot read. This is the one place
    the derived milestones belong, so the document an operator approves is the
    document the next step compiles rather than a summary of it.
    """

    heading = "## 8. Execution Milestones"
    start = markdown.find(heading)
    if start == -1 or not rendered.strip():
        return markdown
    body_start = start + len(heading)
    next_heading = markdown.find("\n## ", body_start)
    tail = markdown[next_heading:] if next_heading != -1 else "\n"
    return f"{markdown[:body_start]}\n\n{rendered}\n{tail}"


def render_execution_milestones_markdown(plan: DurableWorkflowPlan) -> str:
    """The plan's steps as milestone blocks `compile_design_doc` can read.

    Intake already derives everything a milestone needs and then renders it
    somewhere the compiler never looks. `evidence_to_record` is a list of
    checkable outcomes, which is what an `Acceptance:` line is; `approval_required`
    is what makes a milestone an operator gate. The result was a finalized
    document that compiled with `no_milestones`, so every document that ever
    reached the cockpit had been hand-authored past this step.

    `outputs` is deliberately not used for `Artifacts:`. Those entries are free
    text describing deliverables - "per-service pages", "tel links" - while the
    compiler takes a closed vocabulary of artifact kinds. Copying one into the
    other would produce a document that parses and then fails its evidence gate,
    which is worse than one that refuses to compile.

    Every step is `IMPLEMENT`, and `approval_required` becomes a gate on that
    milestone rather than a milestone of its own. Reading it as "this step is a
    review" looked right and was not: the scaffold marks every step
    `approval_required`, so that mapping produced a plan of six operator reviews
    and no implementation - a document that compiles and describes nothing being
    built. The steps are the work; the flag says a person signs off on the work.

    Phases beyond `IMPLEMENT` are not inferred. The steps carry no phase, and
    guessing which one is really a plan or a verification would put structure in
    the operator's plan that the operator did not choose. An operator who wants
    those writes them, in the shape the template now shows.
    """

    blocks: list[str] = []
    for index, step in enumerate(plan.steps):
        phase = step.phase
        lines = [f"### Milestone {index}: {step.name}", "", f"Phase: {phase}"]
        if index:
            lines.append(f"Depends on: {index - 1}")
        lines.append(f"Executor: {_EXECUTOR_FOR_PHASE[phase]}")
        if step.approval_required or phase == "REVIEW":
            lines.append("Approval: required")
        lines.append(f"Description: {step.durable_boundary_reason or step.name}")
        lines.extend(f"Acceptance: {evidence}" for evidence in step.evidence_to_record)
        lines.append(f"Artifacts: {_ARTIFACT_FOR_PHASE[phase]}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


_STAFF_VERDICT_PLACEHOLDER = (
    "## Staff Verdict\n"
    "FINALIZED_DRAFT. Ready for operator review; execution remains blocked until approval."
)
_TOML_FENCE_RE = re.compile(r"```toml\n.*?```", re.DOTALL)


def _model_output_for_task(run_result: PowWowRunResult, task_name: str) -> str | None:
    for task in run_result.tasks:
        if task.task_name != task_name:
            continue
        for artifact in task.artifacts:
            if artifact.artifact_type not in {
                CLI_AGENT_RUN_ARTIFACT_TYPE,
                DELEGATED_TASK_RUN_ARTIFACT_TYPE,
            }:
                continue
            output = artifact.content.get("output") or artifact.content.get("verdict")
            if isinstance(output, str) and output.strip():
                return output.strip()
    return None


def merge_pow_wow_result_into_gawd_review_markdown(
    final_markdown: str,
    run_result: PowWowRunResult,
) -> tuple[str, dict[str, Any]]:
    """Fold the finalization pow-wow outcome into the operator-facing sidecar.

    The deterministic template is written before the pow-wow runs (crash safe);
    this pass rewrites it afterwards so the operator reviews what actually
    happened. A failed run must not present itself as ready for approval, and a
    successful run must show the senior's expanded contract and the staff
    model's real verdict instead of template placeholder text.
    """

    note: dict[str, Any] = {
        "schema_version": "finalized_gawd_model_merge.v1",
        "run_status": run_result.status,
    }
    if run_result.status not in {"COMPLETED", "DRY_RUN_COMPLETED"}:
        failed_tasks = [
            f"{task.task_name}: {task.status}"
            for task in run_result.tasks
            if task.status != "completed"
        ]
        detail_lines = [
            "## Staff Verdict",
            "FINALIZATION_FAILED. Do not approve this draft.",
            f"The finalization pow-wow ended with status {run_result.status}.",
            *(f"- {item}" for item in failed_tasks),
            *(f"- risk: {risk}" for risk in run_result.risks[:5]),
            "Fix the cause and re-run: pi /start /new-project <draft_path>",
        ]
        markdown = final_markdown.replace(
            "**Status:** FINALIZED_DRAFT", "**Status:** FINALIZATION_FAILED", 1
        ).replace(_STAFF_VERDICT_PLACEHOLDER, "\n".join(detail_lines), 1)
        note["status"] = "finalization_failed"
        note["failed_tasks"] = failed_tasks
        return markdown, note

    senior_text = _model_output_for_task(run_result, "senior_spec_completion")
    staff_text = _model_output_for_task(run_result, "staff_final_verdict")
    note["status"] = "merged"
    note["senior_output_present"] = senior_text is not None
    note["staff_output_present"] = staff_text is not None
    markdown = final_markdown
    if senior_text is not None:
        senior_body = _TOML_FENCE_RE.sub(
            "(durable workflow plan rendered into the Execution Milestones section)",
            senior_text,
        ).strip()
        senior_section = f"## Senior Spec Completion (Model Output)\n{senior_body}\n\n"
        markdown = markdown.replace(
            "## Permission Envelope", f"{senior_section}## Permission Envelope", 1
        )
    if staff_text is not None:
        staff_body = _TOML_FENCE_RE.sub(
            "(durable workflow plan rendered into the Execution Milestones section)",
            staff_text,
        ).strip()
        markdown = markdown.replace(
            _STAFF_VERDICT_PLACEHOLDER,
            f"## Staff Verdict\n{staff_body}",
            1,
        )
    if senior_text is None and staff_text is None:
        note["status"] = "no_model_output"
    return markdown, note


def _scaffold_prompt_block(plan: DurableWorkflowPlan) -> str:
    """Render the deterministic workflow scaffold for agent prompts.

    Senior/staff must see the candidate milestones (including approval gates and
    required evidence) to be able to refine or block them. A compact rendering
    keeps the prompt bounded while preserving the judgment-relevant fields.
    """

    steps_by_milestone = {step.milestone_id: step for step in plan.steps}
    lines = [
        "Deterministic scaffold (candidate milestones; refine or block, do not "
        "silently drop approval gates or required evidence):",
    ]
    for milestone in plan.milestones:
        step = steps_by_milestone.get(milestone.milestone_id)
        gate = " [approval_required]" if step is not None and step.approval_required else ""
        evidence = (
            f" evidence: {', '.join(step.evidence_to_record)}"
            if step is not None and step.evidence_to_record
            else ""
        )
        lines.append(f"- {milestone.milestone_id}: {milestone.name}{gate}{evidence}")
    return "\n".join(lines)


def build_gawd_review_tasks(
    draft: SparseGawdDraft,
    workflow_plan_scaffold: DurableWorkflowPlan | None = None,
    draft_markdown: str | None = None,
) -> tuple[PowWowTaskSpec, ...]:
    scan_task = "junior_permissions_scan"
    completion_task = "senior_spec_completion"
    verdict_task = "staff_final_verdict"
    scaffold_block = (
        f"\n\n{_scaffold_prompt_block(workflow_plan_scaffold)}"
        if workflow_plan_scaffold is not None
        else ""
    )
    # The agents run headless with only their prompt; a draft they cannot see
    # is a draft they cannot expand or judge, so embed the (bounded) content.
    draft_block = (
        f"\n\nSparse GAWD draft content (source: {draft.source_path}):\n{draft_markdown[:12000]}"
        if draft_markdown
        else ""
    )
    return (
        PowWowTaskSpec(
            task_name=scan_task,
            role="junior_permissions_scan",
            judgment=JudgmentRole(name="junior_permissions_scan", tier=Tier.JUNIOR),
            dispatch_kind="advisory",
            description=(
                "Read the sparse GAWD draft and identify permissions, autonomy limits, "
                "external dependencies, secret/access needs, spend risks, and "
                f"irreversible gates for project {draft.project}."
                f"{draft_block}"
            ),
            success_criteria=(
                "Permission needs are explicit.",
                "Irreversible actions are separated from safe autonomous work.",
                "Dependency, secret, deploy, spend, and communication risks are named.",
            ),
        ),
        PowWowTaskSpec(
            task_name=completion_task,
            role="senior_spec_completion",
            judgment=JudgmentRole(name="senior_spec_completion", tier=Tier.SENIOR),
            dispatch_kind="advisory",
            blocked_by=(scan_task,),
            description=(
                "Turn the sparse Mini-GAWD draft into a complete scoped build "
                "contract by expanding the missing full-GAWD concerns that affect "
                "execution: service levels, input bounds, interface contracts, "
                "idempotency/replay, observability, dependencies, security/access, "
                "backpressure/cost, rollout/migration/rollback, risk synthesis, "
                "and known limitations. Preserve operator intent and do not invent "
                "new product scope. Emit a fenced ```toml block for "
                "durable_workflow_plan.v1 using configs/durable_workflow_plan.toml; "
                "prefer explicit Execution Milestones when present, otherwise refine "
                "the deterministic scaffold milestones below when provided, otherwise "
                "derive milestones from Happy Path, then refine each [[steps]] entry "
                "from the full-GAWD concerns. Every milestone must keep a matching "
                "[[steps]] entry. Give every step a `phase` of PLAN, IMPLEMENT, "
                "VERIFY, REVIEW or DELIVER. The phase decides what the milestone "
                "must produce as evidence - PLAN an implementation plan, IMPLEMENT a "
                "source patch, VERIFY a test result, REVIEW an operator approval, "
                "DELIVER a delivery record - so give a step a phase whose evidence "
                "its own work actually produces. A step that writes no code is not "
                "IMPLEMENT, and a step that runs no tests is not VERIFY."
                f"{scaffold_block}"
                f"{draft_block}"
            ),
            success_criteria=(
                "Scope, non-goals, lifecycle, and verification are coherent.",
                "Full-GAWD operational gaps are either filled or explicitly blocked.",
                "A durable_workflow_plan.v1 TOML block is emitted or gaps are explicit.",
                "Open questions remain explicit instead of hidden.",
            ),
        ),
        PowWowTaskSpec(
            task_name=verdict_task,
            role="staff_final_verdict",
            judgment=JudgmentRole(name="staff_final_verdict", tier=Tier.STAFF),
            dispatch_kind="advisory",
            blocked_by=(completion_task,),
            description=(
                "Review the finalized GAWD draft, permission envelope, and durable "
                "workflow plan. Decide whether they are ready for operator approval "
                "before execution. Block if milestone boundaries ignore service "
                "levels, bounds, interfaces, idempotency/replay, observability, "
                "dependencies, security/access, cost/backpressure, rollout/rollback, "
                "or top risks. If approving with corrections, emit the corrected "
                "durable_workflow_plan.v1 as a fenced ```toml block. If blocking, "
                "start the verdict with BLOCK and name the missing fields. If a "
                "deterministic scaffold is provided below, dropping one of its "
                "approval gates requires an explicit justification in the verdict."
                f"{scaffold_block}"
                f"{draft_block}"
            ),
            success_criteria=(
                "Verdict names approve/block conditions.",
                "Permission envelope and durable workflow plan reflect full-GAWD concerns.",
                "Execution remains blocked until the operator approves the finalized draft.",
            ),
        ),
    )


def task_graph_payload(
    finalized: FinalizedGawdDraft,
    *,
    tasks: tuple[PowWowTaskSpec, ...],
    durable_workflow_plan: DurableWorkflowPlan | None = None,
    finalized_path: Path | None = None,
    permissions_path: Path | None = None,
    target_project_id: str | None = None,
    target_project_scaffold: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "new_project_task_graph.v1",
        "source_draft_path": finalized.draft.source_path,
        "finalization_tasks": [task.to_payload() for task in tasks],
        "permission_envelope": finalized.permission_envelope.to_payload(),
    }
    if durable_workflow_plan is not None:
        payload["durable_workflow_plan"] = durable_workflow_plan.to_payload()
    if finalized_path is not None:
        payload["finalized_path"] = str(finalized_path)
    if permissions_path is not None:
        payload["permissions_path"] = str(permissions_path)
    if target_project_id is not None:
        payload["target_project_id"] = target_project_id
    if target_project_scaffold is not None:
        payload["target_project_scaffold"] = dict(target_project_scaffold)
    return payload


def permission_envelope_for_draft(draft: SparseGawdDraft) -> PermissionEnvelope:
    raw = draft.raw_text.lower()
    requested: list[PermissionRequest] = [
        PermissionRequest(
            permission="code_worktree_write",
            reason="Implementation agents need isolated write access to complete build tasks.",
        ),
        PermissionRequest(
            permission="test_command_execution",
            reason="Agents need to run verification commands from the approved GAWD doc.",
        ),
    ]
    if _mentions_any(raw, ("install", "package", "dependency", "npm", "pnpm", "pip", "uv ")):
        requested.append(
            PermissionRequest(
                permission="dependency_install",
                reason="Draft mentions package/dependency work.",
            )
        )
    if _mentions_any(raw, ("http", "api", "web", "download", "github", "network")):
        requested.append(
            PermissionRequest(
                permission="network_access",
                reason="Draft appears to require external lookup or networked services.",
            )
        )
    if _mentions_any(raw, ("deploy", "production", "release", "ship")):
        requested.append(
            PermissionRequest(
                permission="deploy",
                reason="Draft mentions deployment or release activity.",
            )
        )
    if _mentions_any(raw, ("email", "slack", "message customer", "post ", "send ")):
        requested.append(
            PermissionRequest(
                permission="external_communications",
                reason="Draft may involve outbound communication.",
            )
        )
    if _mentions_any(raw, ("buy", "purchase", "paid", "stripe", "billing", "credit card")):
        requested.append(
            PermissionRequest(
                permission="spend_money",
                reason="Draft may involve paid services or purchases.",
            )
        )

    denied = (
        "merge_to_main",
        "deploy",
        "purchase_or_spend",
        "external_communications",
        "secret_or_credential_access",
        "destructive_file_operations",
    )
    risks = tuple(
        item
        for item in (
            "Sparse draft still has unresolved sections; keep execution blocked until reviewed."
            if draft.unresolved_questions
            else "",
            "Permission envelope is heuristic and must be operator-approved before execution.",
            "Finalized draft preserves non-goals to reduce intent drift.",
        )
        if item
    )
    return PermissionEnvelope(
        autonomous_permissions=(
            "read_repo_context",
            "write_ledger_artifacts",
            "run_local_model_delegates",
            "prepare_isolated_worktrees",
            "request_operator_decisions",
        ),
        requested_permissions=tuple(requested),
        denied_without_approval=denied,
        risks=risks,
    )


def render_gawd_review_markdown(draft: SparseGawdDraft, envelope: PermissionEnvelope) -> str:
    today = datetime.now(UTC).date().isoformat()
    parts = [
        "# THE GAWD DOC - Mini",
        "",
        f"**Project:** {draft.project} | **Version:** v4-mini | "
        f"**Status:** FINALIZED_DRAFT | **Date:** {today}",
        "",
        # Declared unambiguously rather than left to the banner above, which is a
        # display line and parses as a pipe-separated blob.
        f"Target project: {draft.project}",
        "",
        "## 1. Theory of the System",
        draft.theory or "A scoped build saga driven by a durable GAWD intake contract.",
        "",
        "## 2. Why This Exists",
        draft.why or draft.goal,
        "",
        "## 3. Happy Path / Golden Flow",
        _numbered(
            draft.golden_flow
            or (
                "Operator approves finalized GAWD doc.",
                "Saga executes approved task graph.",
            )
        ),
        "",
        "## 4. This Version - Scope & Non-Goals",
        "**In scope.**",
        _bullets(draft.in_scope or ("Finalize the project contract before execution.",)),
        "",
        "**Cut (non-goals).**",
        _bullets(draft.non_goals or ("No execution before operator approval.",)),
        "",
        "## 5. Core Design",
        (
            f"**Unit of work.** "
            f"{draft.unit_of_work or 'One approved saga from one finalized GAWD doc.'}"
        ),
        "",
        "**Lifecycle.**",
        _bullets(
            draft.lifecycle
            or ("sparse draft -> finalized draft -> approved GAWD -> saga execution",)
        ),
        "",
        "**Data model.**",
        _bullets(
            draft.data_model
            or (
                "Text draft remains human-editable working memory.",
                "Ledger GAWD doc is durable truth after ingestion.",
                "Pow-wow artifacts record finalization and permission decisions.",
            )
        ),
        "",
        "## 6. The Failure That Matters Most",
        draft.failure_that_matters
        or (
            "Intent drift into unapproved work. Recovery is explicit non-goals, "
            "permission gates, and no execution before approval."
        ),
        "",
        "## 7. Verification",
        _bullets(draft.verification or ("Finalized draft and permission envelope are persisted.",)),
        "",
        "## 8. Execution Milestones",
        _milestone_blocks(
            draft.execution_milestones
            or (
                "Senior/staff must derive durable milestones from Happy Path, Core "
                "Design, failure semantics, verification, and rollout constraints.",
            )
        ),
        "",
        "## 9. Operational Contract",
        "**Service levels.**",
        _bullets(draft.service_levels or ("Senior/staff must define measurable targets.",)),
        "",
        "**Input bounds.**",
        _bullets(draft.input_bounds or ("Senior/staff must define size, rate, and time bounds.",)),
        "",
        "**Interface contracts.**",
        _bullets(
            draft.interface_contracts
            or ("Senior/staff must define changed interfaces and compatibility promises.",)
        ),
        "",
        "**Idempotency / replay.**",
        _bullets(
            draft.idempotency_replay
            or ("Senior/staff must define retry keys, dedupe rules, and replay safety.",)
        ),
        "",
        "**Observability.**",
        _bullets(
            draft.observability
            or ("Senior/staff must define evidence, logs, metrics, and inspection hooks.",)
        ),
        "",
        "**Dependencies.**",
        _bullets(
            draft.dependencies or ("Senior/staff must list external systems and failure behavior.",)
        ),
        "",
        "**Security / access.**",
        _bullets(
            draft.security_access
            or ("Senior/staff must define secret, permission, and data-access boundaries.",)
        ),
        "",
        "**Backpressure / cost.**",
        _bullets(
            draft.backpressure_cost
            or ("Senior/staff must define concurrency, queue, and spend controls.",)
        ),
        "",
        "## 10. Rollout / Migration / Rollback",
        _bullets(
            draft.rollout_migration_rollback
            or (
                "Senior/staff must define rollout gates, migration safety, and "
                "rollback or compensation behavior.",
            )
        ),
        "",
        "## 11. Risk Synthesis / Known Limitations",
        "**Risk synthesis.**",
        _bullets(
            draft.risk_synthesis
            or ("Senior/staff must rank the top risks and mitigation confidence.",)
        ),
        "",
        "**Known limitations.**",
        _bullets(
            draft.known_limitations
            or ("Senior/staff must state when this design stops being sufficient.",)
        ),
        "",
        "## 12. Decision Log",
        _bullets(
            draft.decisions
            or (
                "D1 - Use a file-first GAWD draft as the intake contract.",
                "D2 - Keep execution blocked until operator approval.",
            )
        ),
        "",
        "## 13. If I Had 2 More Weeks",
        _bullets(draft.deferred or ("Richer TUI form for drafting the same file contract.",)),
        "",
        "## Permission Envelope",
        "Autonomous permissions:",
        _bullets(envelope.autonomous_permissions),
        "",
        "Requested permissions:",
        _bullets(
            tuple(
                f"{request.permission}: {request.reason}"
                for request in envelope.requested_permissions
            )
        ),
        "",
        "Denied without explicit approval:",
        _bullets(envelope.denied_without_approval),
        "",
        "Risks:",
        _bullets(envelope.risks),
        "",
        "## Staff Verdict",
        "FINALIZED_DRAFT. Ready for operator review; execution remains blocked until approval.",
        "",
    ]
    return "\n".join(parts)


def _sparse_gawd_template(draft_id: str, created_at: datetime) -> str:
    return f"""# THE GAWD DOC - Mini

Target project: _REGISTERED PROJECT ID_

**Draft ID:** {draft_id}
**Project:** _PROJECT NAME_ | **Version:** v4-mini | **Status:** SPARSE_DRAFT
**Date:** {created_at.date().isoformat()}

<!-- Replace _REGISTERED_PROJECT_ID_ above with a project id from the registry,
     which `local-agent projects` lists. It is the only line here the compiler
     refuses to guess: a draft without it used to compile against the
     project-center default and dispatch a frontier agent at the wrong
     repository, so it is now an execution blocker. The `**Project:**` banner
     below it is a human title and is not read as an id. -->


**Priority order:** correctness > stability > debuggability > throughput > latency

## Time Budget

| Phase | Hours | Deliverable |
|---|---|---|
| scope | 0.5-1h | finalized GAWD doc and permission envelope |
| build | 1-3h | first working implementation |
| verify | 0.5-1h | proof and approval notes |

## 1. Theory of the System

Write the archetype and one-sentence computational shape.

## 2. Why This Exists

Write the concrete pain this removes.

## 3. Happy Path / Golden Flow

1. Write the starting state.
2. Write the main agent/build step.
3. Write the done state.

## 4. This Version - Scope & Non-Goals

**In scope.**
- Write what is included.

**Cut (non-goals).**
- Write what is deliberately excluded.

## 5. Core Design

**Unit of work.** Write the unit of work.

**Lifecycle.**
- created -> processing -> done | failed

**Data model.**
- Write the durable entities and storage.

## 6. The Failure That Matters Most

Write the one failure that would actually hurt this build.

## 7. Verification

- Write the smoke proof that should pass.

## 8. Execution Milestones

Leave this sparse if you want the senior tier to derive milestones from the
Happy Path. Whoever fills it in copies the shape below, once per milestone.

`compile_design_doc` finds milestones by their `### Milestone N:` heading and
nothing else, so a bullet list here is invisible to it and the document compiles
with `no_milestones`. The example is fenced so it stays an example: an unfenced
one would be read as a real milestone and this section would ship plan data
nobody wrote.

```markdown
### Milestone 0: Write the title as an outcome, not a task

Phase: PLAN
Depends on: none
Description: What this milestone settles, and why it precedes the next one.
Acceptance: one checkable sentence per line, each line starting with Acceptance:
Acceptance: written as observable outcomes rather than steps taken
Artifacts: implementation_plan
```

`Phase:` is required. `Executor:` is required wherever the phase default is
wrong: `REVIEW` defaults to `review.agent`, which cannot produce an
`operator_approval`, so an operator gate needs `Executor: review.operator` and
`Approval: required`.

## 9. Operational Contract

**Service levels.**
- Write latency, success, recovery, or correctness targets if known.

**Input bounds.**
- Write size, rate, count, time, or concurrency limits if known.

**Interface contracts.**
- Write APIs, events, file formats, or compatibility promises if known.

**Idempotency / replay.**
- Write retry keys, dedupe rules, replay scope, or variance notes if known.

**Observability.**
- Write logs, metrics, inspection commands, or evidence artifacts if known.

**Dependencies.**
- Write network, database, model, service, or human dependencies if known.

**Security / access.**
- Write secrets, credentials, roles, data sensitivity, or tenant boundaries if known.

**Backpressure / cost.**
- Write capacity, queueing, budget, spend, or degradation rules if known.

## 10. Rollout / Migration / Rollback

- Write deploy, migration, rollback, or manual gate notes if known.

## 11. Risk Synthesis / Known Limitations

**Risk synthesis.**
- Write the top risk, mitigation, and confidence if known.

**Known limitations.**
- Write where this design stops being sufficient.

## 12. Decision Log

- D1 - Write the first decision and rationale.

## 13. If I Had 2 More Weeks

- Write deferred work here.
"""


def render_sparse_gawd_draft(
    *,
    draft_id: str,
    created_at: datetime,
    project: str,
    section_bodies: Mapping[str, str],
) -> str:
    """Render accepted intake text into the existing sparse GAWD shape.

    Keys in ``section_bodies`` are normalized through the same heading matcher
    used by the parser. Unspecified sections retain their sparse placeholders.
    """

    rendered = _sparse_gawd_template(draft_id, created_at)
    safe_project = " ".join(project.split()).strip() or "PROJECT NAME"
    rendered = rendered.replace(
        "**Project:** _PROJECT NAME_",
        f"**Project:** {safe_project}",
    )
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", rendered, flags=re.MULTILINE))
    replacements: list[tuple[int, int, str]] = []
    for index, match in enumerate(matches):
        heading = _normalize_heading(match.group(1))
        body = next(
            (
                value.strip()
                for key, value in section_bodies.items()
                if _heading_matches(heading, _normalize_heading(key)) and value.strip()
            ),
            None,
        )
        if body is None:
            continue
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(rendered)
        replacements.append((body_start, body_end, f"\n\n{body}\n\n"))
    for start, end, replacement in reversed(replacements):
        rendered = rendered[:start] + replacement + rendered[end:]
    return rendered.rstrip() + "\n"


def _parse_draft_id_from_path(path: Path) -> str:
    match = re.search(r"gawd_doc_([A-Za-z0-9_-]+)", path.stem)
    return match.group(1) if match else uuid4().hex


def _sections(text: str) -> dict[str, str]:
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", text, flags=re.MULTILINE))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[_normalize_heading(match.group(1))] = text[start:end].strip()
    return sections


def _normalize_heading(heading: str) -> str:
    cleaned = re.sub(r"^\d+\.\s*", "", heading.strip().lower())
    cleaned = cleaned.replace("&", "and")
    return re.sub(r"[^a-z0-9]+", " ", cleaned).strip()


def _section_body(sections: dict[str, str], name: str) -> str:
    normalized = _normalize_heading(name)
    for heading, body in sections.items():
        if _heading_matches(heading, normalized):
            return body
    return ""


def _heading_matches(actual: str, expected: str) -> bool:
    actual_without_and = re.sub(r"\band\b", "", actual)
    expected_without_and = re.sub(r"\band\b", "", expected)
    actual_key = re.sub(r"\s+", " ", actual_without_and).strip()
    expected_key = re.sub(r"\s+", " ", expected_without_and).strip()
    return expected_key in actual_key or actual_key in expected_key


def _section_lines(sections: dict[str, str], name: str) -> list[str]:
    return _meaningful_lines(_section_body(sections, name))


def _project_name(text: str) -> str:
    match = re.search(r"\*\*Project:\*\*\s*([^|\n·]+)", text)
    if not match:
        return ""
    value = match.group(1).strip().strip("_ ")
    if not value or value.upper() == "PROJECT NAME":
        return ""
    return value


def _label_text(body: str, label: str) -> str:
    pattern = re.compile(rf"\*\*{re.escape(label)}\.\*\*\s*(.+)", flags=re.IGNORECASE)
    for line in body.splitlines():
        match = pattern.search(line.strip())
        if match:
            return _clean_line(match.group(1))
    return ""


def _label_lines(body: str, label: str) -> list[str]:
    normalized_label = _normalize_heading(label)
    lines = body.splitlines()
    selected: list[str] = []
    active = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("**") and stripped.endswith("**"):
            active = normalized_label in _normalize_heading(stripped.strip("* "))
            continue
        if stripped.startswith("**"):
            active = normalized_label in _normalize_heading(stripped.strip("* "))
            text = re.sub(r"^\*\*.+?\*\*\s*", "", stripped)
            if active and text:
                selected.append(_clean_line(text))
            continue
        if active:
            if stripped.startswith("##"):
                break
            cleaned = _clean_line(stripped)
            if cleaned:
                selected.append(cleaned)
    return selected


def _meaningful_lines(body: str) -> list[str]:
    """The lines of a section that carry operator intent.

    Fenced blocks are skipped whole. A fence in a sparse draft is showing the
    reader a shape - the milestone form the compiler reads, a config snippet, a
    command - and an example that became plan data would put words in the
    operator's mouth that the operator never wrote.

    Skipping them structurally rather than naming them in the placeholder set
    below is what keeps that set from having to grow by one entry per line of
    every example the template ever shows.
    """

    selected: list[str] = []
    fenced = False
    for line in body.splitlines():
        if line.strip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if cleaned := _clean_line(line):
            selected.append(cleaned)
    return selected


def _plain_text(body: str) -> str:
    return " ".join(_meaningful_lines(body))


def _first_meaningful_line(body: str) -> str:
    lines = _meaningful_lines(body)
    return lines[0] if lines else ""


def _clean_line(line: str) -> str:
    cleaned = line.strip()
    cleaned = re.sub(r"^\d+\.\s*", "", cleaned)
    cleaned = re.sub(r"^[-*]\s+", "", cleaned)
    cleaned = cleaned.strip("| ")
    if not cleaned:
        return ""
    placeholders = {
        "write the archetype and one-sentence computational shape.",
        "write the concrete pain this removes.",
        "write the starting state.",
        "write the main agent/build step.",
        "write the done state.",
        "write what is included.",
        "write what is deliberately excluded.",
        "write the unit of work.",
        "write the durable entities and storage.",
        "write the one failure that would actually hurt this build.",
        "write the smoke proof that should pass.",
        "leave this sparse if you want staff to derive milestones from the happy path.",
        "m1 - write a milestone, exit evidence, and approval gate if known.",
        "leave this sparse if you want the senior tier to derive milestones from the",
        "happy path. whoever fills it in copies the shape below, once per milestone.",
        "`compile_design_doc` finds milestones by their `### milestone n:` heading and",
        "nothing else, so a bullet list here is invisible to it and the document compiles",
        "with `no_milestones`. the example is fenced so it stays an example: an unfenced",
        "one would be read as a real milestone and this section would ship plan data",
        "nobody wrote.",
        "`phase:` is required. `executor:` is required wherever the phase default is",
        "wrong: `review` defaults to `review.agent`, which cannot produce an",
        "`operator_approval`, so an operator gate needs `executor: review.operator` and",
        "`approval: required`.",
        "write latency, success, recovery, or correctness targets if known.",
        "write size, rate, count, time, or concurrency limits if known.",
        "write apis, events, file formats, or compatibility promises if known.",
        "write retry keys, dedupe rules, replay scope, or variance notes if known.",
        "write logs, metrics, inspection commands, or evidence artifacts if known.",
        "write network, database, model, service, or human dependencies if known.",
        "write secrets, credentials, roles, data sensitivity, or tenant boundaries if known.",
        "write capacity, queueing, budget, spend, or degradation rules if known.",
        "write deploy, migration, rollback, or manual gate notes if known.",
        "write the top risk, mitigation, and confidence if known.",
        "write where this design stops being sufficient.",
        "d1 - write the first decision and rationale.",
        "write deferred work here.",
    }
    if cleaned.lower() in placeholders:
        return ""
    if re.fullmatch(r"_.*_", cleaned):
        return ""
    return cleaned


def _missing_sections(sections: dict[str, str]) -> list[str]:
    required = (
        "theory of the system",
        "why this exists",
        "happy path golden flow",
        "this version scope non goals",
        "core design",
        "the failure that matters most",
        "verification",
    )
    missing = []
    for section in required:
        if not _section_lines(sections, section):
            missing.append(section)
    return missing


def _mentions_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _archetype_derivation_rules(archetype_plan: Any) -> tuple[DurableWorkflowDerivationRule, ...]:
    if archetype_plan is None:
        return ()
    overlays = ", ".join(archetype_plan.applied_overlays) or "base_saas"
    questions = "; ".join(archetype_plan.blocked_questions) or "none"
    return (
        DurableWorkflowDerivationRule(
            source_section="SaaS Archetype Planner",
            owner="deterministic_planner_then_staff",
            produces=("candidate_milestones", "approval_gates", "required_evidence"),
            instruction=(
                "Use the deterministic SaaS scaffold as candidate execution structure. "
                f"Applied overlays: {overlays}. Planner confidence: "
                f"{archetype_plan.confidence}. Blocked questions: {questions}."
            ),
        ),
    )


def _archetype_source_sections(
    template: ArchetypeMilestoneTemplate | None,
) -> tuple[str, ...]:
    if template is None:
        return ()
    return ("SaaS Archetype Planner", *template.full_gawd_sources)


def _workflow_step_source_sections(
    template: ArchetypeMilestoneTemplate | None,
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                "Execution Milestones",
                "Happy Path / Golden Flow",
                "Core Design",
                "The Failure That Matters Most",
                "Verification",
                "Operational Contract",
                "Rollout / Migration / Rollback",
                "Risk Synthesis / Known Limitations",
                "Permission Envelope",
                *_archetype_source_sections(template),
            )
        )
    )


def _workflow_step_approval_required(
    draft: SparseGawdDraft,
    milestone: DurableWorkflowMilestone,
    template: ArchetypeMilestoneTemplate | None,
    approval_keywords: tuple[str, ...],
) -> bool:
    return bool(template and template.approval_required) or _mentions_any(
        _approval_scan_text(draft, milestone),
        approval_keywords,
    )


def _workflow_step_inputs(
    draft: SparseGawdDraft,
    milestone: DurableWorkflowMilestone,
    template: ArchetypeMilestoneTemplate | None = None,
) -> tuple[str, ...]:
    values = [
        f"approved milestone input from happy path: {milestone.happy_path_step}",
        f"unit of work: {draft.unit_of_work or 'senior/staff must define'}",
    ]
    if template is not None:
        values.extend(f"archetype entry criterion: {item}" for item in template.entry_criteria)
        values.extend(f"archetype include condition: {item}" for item in template.include_when)
    values.extend(f"data model: {item}" for item in draft.data_model)
    values.extend(f"input bound: {item}" for item in draft.input_bounds)
    values.extend(f"interface contract: {item}" for item in draft.interface_contracts)
    values.extend(f"dependency: {item}" for item in draft.dependencies)
    return tuple(dict.fromkeys(values))


def _workflow_step_outputs(
    milestone: DurableWorkflowMilestone,
    template: ArchetypeMilestoneTemplate | None = None,
) -> tuple[str, ...]:
    outputs = [f"Durable evidence that milestone completed: {milestone.happy_path_step}"]
    if template is not None:
        outputs.extend(f"archetype exit criterion: {item}" for item in template.exit_criteria)
    return tuple(dict.fromkeys(outputs))


def _workflow_step_side_effects(
    draft: SparseGawdDraft,
    milestone: DurableWorkflowMilestone,
    template: ArchetypeMilestoneTemplate | None = None,
) -> tuple[str, ...]:
    step_text = milestone.happy_path_step.lower()
    effects = ["write ledger artifacts"]
    if _mentions_any(step_text, ("code", "build", "implement", "file", "repo")):
        effects.append("may write isolated worktree files after approval")
    if _mentions_any(step_text, ("database", "migration", "schema", "data")):
        effects.append("may mutate staging database after approval")
    if _mentions_any(step_text, ("deploy", "release", "production", "ship")):
        effects.append("may deploy only behind explicit approval gate")
    if _mentions_any(step_text, ("email", "slack", "send", "post")):
        effects.append("may send external communication only behind explicit approval gate")
    for item in draft.dependencies:
        effects.append(f"uses dependency: {item}")
    for item in draft.security_access:
        effects.append(f"security/access constraint: {item}")
    for item in draft.backpressure_cost:
        effects.append(f"backpressure/cost constraint: {item}")
    if template is not None and template.approval_required:
        effects.append("archetype template marks this milestone approval-bound")
    if not draft.data_model:
        effects.append("senior/staff must refine side effects from Core Design")
    return tuple(dict.fromkeys(effects))


def _workflow_retry_policy(draft: SparseGawdDraft) -> str:
    failure = draft.failure_that_matters.strip()
    idempotency = "; ".join(draft.idempotency_replay)
    if idempotency and failure:
        return (
            "Retry only idempotent substeps. Use the idempotency/replay contract: "
            f"{idempotency}. On recurrence of the stated failure - {failure} - "
            "record evidence and block for senior/staff review."
        )
    if idempotency:
        return (
            "Retry only idempotent substeps. Use the idempotency/replay contract: "
            f"{idempotency}. If retry safety is unclear, record failure evidence "
            "and block for review."
        )
    if not failure:
        return (
            "Senior/staff must define retry policy. Default is no blind retry; "
            "record failure evidence and block for review."
        )
    return (
        "Retry only idempotent substeps. On recurrence of the stated failure - "
        f"{failure} - record evidence and block for senior/staff review."
    )


def _workflow_timeout_policy(draft: SparseGawdDraft) -> str:
    controls = "; ".join((*draft.service_levels, *draft.input_bounds, *draft.dependencies))
    if controls:
        return (
            "Senior/staff must derive timeout from service levels, input bounds, "
            f"and dependencies: {controls}. Default is fail closed and record a "
            "blocking artifact."
        )
    return (
        "Senior/staff must derive timeout from time budget and expected external "
        "systems; default is fail closed and record a blocking artifact."
    )


def _workflow_compensation_policy(draft: SparseGawdDraft) -> str:
    failure = draft.failure_that_matters.strip()
    rollout = "; ".join(draft.rollout_migration_rollback)
    if rollout and failure:
        return (
            "Use the rollout/migration/rollback contract for compensation: "
            f"{rollout}. The stated failure remains the recovery driver: {failure}. "
            "If compensation cannot be proven safe, create an approval gate."
        )
    if rollout:
        return (
            "Use the rollout/migration/rollback contract for compensation: "
            f"{rollout}. If compensation cannot be proven safe, create an "
            "approval gate."
        )
    if not failure:
        return (
            "Senior/staff must define rollback or compensation. Default is fail "
            "closed before irreversible actions."
        )
    return (
        "Use the failure section as the recovery driver: "
        f"{failure}. If compensation cannot be proven safe, create an approval gate."
    )


def _workflow_evidence(
    draft: SparseGawdDraft,
    milestone: DurableWorkflowMilestone,
    template: ArchetypeMilestoneTemplate | None = None,
) -> tuple[str, ...]:
    evidence = [
        f"milestone result artifact for {milestone.milestone_id}",
        "DBOS workflow or step status",
        "coordination ledger artifact id",
    ]
    if template is not None:
        evidence.extend(template.required_evidence)
    evidence.extend(f"verification: {item}" for item in draft.verification)
    evidence.extend(f"observability: {item}" for item in draft.observability)
    evidence.extend(f"service level: {item}" for item in draft.service_levels)
    evidence.extend(f"risk gate: {item}" for item in draft.risk_synthesis)
    return tuple(dict.fromkeys(evidence))


def _approval_scan_text(
    draft: SparseGawdDraft,
    milestone: DurableWorkflowMilestone,
) -> str:
    parts = (
        milestone.happy_path_step,
        *draft.security_access,
        *draft.rollout_migration_rollback,
        *draft.backpressure_cost,
        *draft.risk_synthesis,
        *draft.known_limitations,
    )
    return " ".join(parts).lower()


_REQUIRED_DURABLE_STEP_FIELDS = frozenset(
    (
        "step_id",
        "name",
        "source_sections",
        "durable_boundary_reason",
        "inputs",
        "outputs",
        "side_effects",
        "idempotency_key",
        "retry_policy",
        "timeout_policy",
        "compensation_or_rollback",
        "approval_required",
        "evidence_to_record",
    )
)


def _structured_payload_candidates(text: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    for match in re.finditer(
        r"```(?P<language>toml|json)?\s*(?P<body>.*?)```",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        language = (match.group("language") or "toml").lower()
        body = match.group("body").strip()
        if body:
            candidates.append((language, body))
    stripped = text.strip()
    if stripped:
        if stripped.startswith("{"):
            candidates.append(("json", stripped))
        else:
            candidates.append(("toml", stripped))
    return candidates


def _load_structured_payload(language: str, raw_payload: str) -> Mapping[str, Any]:
    payload = json.loads(raw_payload) if language == "json" else tomllib.loads(raw_payload)
    if not isinstance(payload, Mapping):
        raise TypeError("durable workflow plan output must decode to a mapping")
    return payload


def _parse_durable_workflow_milestone(
    payload: Mapping[str, Any],
) -> DurableWorkflowMilestone:
    return DurableWorkflowMilestone(
        milestone_id=str(payload.get("milestone_id") or ""),
        name=str(payload.get("name") or ""),
        happy_path_step=str(payload.get("happy_path_step") or ""),
        source_index=int(payload.get("source_index") or 0),
    )


def _parse_durable_workflow_derivation_rule(
    payload: Mapping[str, Any],
) -> DurableWorkflowDerivationRule:
    return DurableWorkflowDerivationRule(
        source_section=str(payload.get("source_section") or ""),
        owner=str(payload.get("owner") or ""),
        produces=_string_tuple(payload.get("produces")),
        instruction=str(payload.get("instruction") or ""),
    )


def _parse_step_phase(value: object) -> str:
    """The phase a refined step claims, refused if it is not one that exists.

    Refused rather than coerced. A phase decides the artifact the milestone must
    produce, so quietly turning an unrecognised one into `IMPLEMENT` would hide a
    model's mistake inside a plan that then fails its evidence gate three
    milestones later, with nothing pointing back to here.
    """

    if value is None or not str(value).strip():
        return "IMPLEMENT"
    phase = str(value).strip().upper()
    if phase not in _ARTIFACT_FOR_PHASE:
        raise ValueError(
            f"durable workflow step declares phase {phase!r}, which is not one of "
            f"{sorted(_ARTIFACT_FOR_PHASE)}"
        )
    return phase


def _parse_durable_workflow_step(payload: Mapping[str, Any]) -> DurableWorkflowStep:
    missing = sorted(_REQUIRED_DURABLE_STEP_FIELDS - set(payload))
    if missing:
        raise ValueError(f"durable workflow step is missing required fields: {missing}")
    step_id = str(payload.get("step_id") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not step_id or not name:
        raise ValueError("durable workflow step requires non-empty step_id and name")
    return DurableWorkflowStep(
        step_id=step_id,
        name=name,
        milestone_id=str(payload.get("milestone_id") or step_id),
        phase=_parse_step_phase(payload.get("phase")),
        source_sections=_string_tuple(payload.get("source_sections")),
        durable_boundary_reason=str(payload.get("durable_boundary_reason") or ""),
        inputs=_string_tuple(payload.get("inputs")),
        outputs=_string_tuple(payload.get("outputs")),
        side_effects=_string_tuple(payload.get("side_effects")),
        idempotency_key=str(payload.get("idempotency_key") or ""),
        retry_policy=str(payload.get("retry_policy") or ""),
        timeout_policy=str(payload.get("timeout_policy") or ""),
        compensation_or_rollback=str(payload.get("compensation_or_rollback") or ""),
        approval_required=bool(payload.get("approval_required")),
        evidence_to_record=_string_tuple(payload.get("evidence_to_record")),
        derived_by=str(payload.get("derived_by") or "senior_or_staff_model"),
    )


def _parse_permission_envelope(
    payload: Any,
    *,
    fallback: PermissionEnvelope,
) -> PermissionEnvelope:
    if not isinstance(payload, Mapping):
        return fallback
    raw_requests = payload.get("requested_permissions")
    requested = (
        tuple(_parse_permission_request(item) for item in raw_requests)
        if isinstance(raw_requests, list)
        else fallback.requested_permissions
    )
    return PermissionEnvelope(
        autonomous_permissions=_string_tuple(
            payload.get("autonomous_permissions") or fallback.autonomous_permissions
        ),
        requested_permissions=requested,
        denied_without_approval=_string_tuple(
            payload.get("denied_without_approval") or fallback.denied_without_approval
        ),
        risks=_string_tuple(payload.get("risks") or fallback.risks),
        schema_version=str(payload.get("schema_version") or fallback.schema_version),
    )


def _parse_permission_request(payload: Any) -> PermissionRequest:
    if not isinstance(payload, Mapping):
        return PermissionRequest(permission=str(payload), reason="model supplied permission")
    return PermissionRequest(
        permission=str(payload.get("permission") or ""),
        reason=str(payload.get("reason") or ""),
        required_before_execution=bool(payload.get("required_before_execution", True)),
    )


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(str(item) for item in value)
    if value is None:
        return ()
    return (str(value),)


def _clean_title(value: str) -> str:
    cleaned = re.sub(r"[_-]+", " ", value).strip()
    return cleaned[:1].upper() + cleaned[1:] if cleaned else "New Project"


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug[:48] or "milestone"


def _bullets(items: tuple[str, ...]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _numbered(items: tuple[str, ...]) -> str:
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, start=1))


def _milestone_blocks(items: tuple[str, ...]) -> str:
    """Render the author's milestones as blocks the WorkUnit compiler can execute.

    An interview produces a list of things to build. A compiled plan is a graph
    across a fixed lifecycle, and its policy refuses work that no plan precedes
    and work no verification covers. Prose alone can never satisfy that, so a
    finalized draft used to be unexecutable no matter how well it was written.

    The spine around the author's items is a default, and the difference between
    a default and an invention is where it appears. This one is written into the
    document a human reads and approves before anything runs, so an operator who
    disagrees can edit or delete it. That is the opposite of the compiler quietly
    synthesizing structure at compile time, which is how this codebase has
    repeatedly ended up executing decisions nobody made.

    Phases are declared rather than inferred, so compiling stays deterministic
    and offline: `--classify-phases` is for documents that did not say, and this
    one says.

    The author's text is never truncated. It is the heading, the description, and
    the acceptance criterion, because a milestone list item is a claim about what
    will be true, and shortening it would decide which half mattered.
    """

    def block(key: int, title: str, lines: list[str]) -> list[str]:
        return [f"### Milestone {key}: {title}", ""] + lines + [""]

    out = block(
        1,
        "Plan the implementation",
        [
            "Phase: PLAN",
            (
                "Description: Turn the accepted contract into an ordered implementation "
                "plan, naming what each milestone below changes and where."
            ),
            ("Acceptance: an implementation plan names what each execution milestone changes"),
            "Artifacts: implementation_plan",
        ],
    )

    implement_keys: list[str] = []
    key = 1
    for item in items:
        key += 1
        implement_keys.append(str(key))
        text = " ".join(item.split())
        out += block(
            key,
            text,
            [
                "Phase: IMPLEMENT",
                "Depends on: 1",
                f"Description: {text}",
                f"Acceptance: {text}",
                "Artifacts: source_patch",
            ],
        )

    verify_key = key + 1
    out += block(
        verify_key,
        "Verify the delivered work",
        [
            "Phase: VERIFY",
            f"Depends on: {', '.join(implement_keys)}",
            (
                "Description: Run the verification named in section 7 against everything "
                "the milestones above changed."
            ),
            "Acceptance: the verification in section 7 passes against the changed system",
            "Artifacts: test_result",
        ],
    )

    # A DELIVER milestone is what gives the WorkUnit's terminal gate anything to
    # check. `required_final_artifacts` unions document-level artifacts with
    # DELIVER-phase ones and nothing else, so a plan without one ends by
    # subtracting an empty set and cannot fail however the run went.
    out += block(
        verify_key + 1,
        "Record what was delivered",
        [
            "Phase: DELIVER",
            f"Depends on: {verify_key}",
            (
                "Description: Record what shipped, what it does not yet cover, and how to "
                "operate it."
            ),
            "Acceptance: a delivery record names what shipped and what remains open",
            "Artifacts: delivery_record",
        ],
    )
    return "\n".join(out).rstrip()
