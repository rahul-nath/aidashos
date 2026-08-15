# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json

import pytest

from local_first_agent_os.new_project_intake import (
    DurableWorkflowPlan,
    PermissionEnvelope,
    build_durable_workflow_plan,
    build_gawd_review_tasks,
    build_reviewable_gawd_draft,
    create_sparse_gawd_draft_file,
    merge_pow_wow_result_into_gawd_review_markdown,
    parse_durable_workflow_plan_payload,
    parse_sparse_gawd_draft,
    refine_durable_workflow_plan_from_run_result,
)
from local_first_agent_os.pow_wow import (
    PowWowArtifact,
    PowWowRunResult,
    PowWowRunStatus,
    PowWowTaskResult,
)
from local_first_agent_os.staffing import Tier


def test_sparse_gawd_draft_file_parses_and_finalizes(tmp_path) -> None:
    draft_file = create_sparse_gawd_draft_file(tmp_path)
    draft_file.path.write_text(
        """# THE GAWD DOC - Mini

**Draft ID:** test
**Project:** Ledger Intake | **Version:** v4-mini | **Status:** SPARSE_DRAFT
**Date:** 2026-07-08

## 1. Theory of the System

Durable intake contract over an event-driven agent swarm.

## 2. Why This Exists

Start projects from explicit scope instead of raw prompts.

## 3. Happy Path / Golden Flow

1. Draft the spec.
2. Finalize permissions.
3. Start approved work.

## 4. This Version - Scope & Non-Goals

**In scope.**
- Create the intake path.
- Run tests.

**Cut (non-goals).**
- No deploy.

## 5. Core Design

**Unit of work.** one project intake

**Lifecycle.**
- sparse draft -> finalized draft -> approved execution

**Data model.**
- text file plus ledger GAWD doc

## 6. The Failure That Matters Most

Agents deploy or merge without approval.

## 7. Verification

- Unit tests pass.

## 8. Decision Log

- D1 - Use file-first intake.

## 9. If I Had 2 More Weeks

- Add a TUI later.
""",
        encoding="utf-8",
    )

    draft = parse_sparse_gawd_draft(draft_file.path)
    finalized = build_reviewable_gawd_draft(draft)
    workflow_plan = build_durable_workflow_plan(finalized)
    tasks = build_gawd_review_tasks(draft)

    assert draft.project == "Ledger Intake"
    assert draft.goal == "Start projects from explicit scope instead of raw prompts."
    assert "No deploy." in draft.non_goals
    assert finalized.permission_envelope.schema_version == "permission_envelope.v1"
    assert "merge_to_main" in finalized.permission_envelope.denied_without_approval
    assert [milestone.happy_path_step for milestone in workflow_plan.milestones] == [
        "Draft the spec.",
        "Finalize permissions.",
        "Start approved work.",
    ]
    assert [step.step_id for step in workflow_plan.steps] == [
        "step_m01_draft_the_spec",
        "step_m02_finalize_permissions",
        "step_m03_start_approved_work",
    ]
    assert workflow_plan.steps[0].source_sections == (
        "Execution Milestones",
        "Happy Path / Golden Flow",
        "Core Design",
        "The Failure That Matters Most",
        "Verification",
        "Operational Contract",
        "Rollout / Migration / Rollback",
        "Risk Synthesis / Known Limitations",
        "Permission Envelope",
    )
    # The payload contract the ledger persists in `task_graph`: every step
    # carries the full durable-boundary field set.
    workflow_payload = workflow_plan.to_payload()
    expected_step_fields = {
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
    }
    assert expected_step_fields <= set(workflow_payload["steps"][0])
    assert [task.judgment.tier for task in tasks if task.judgment] == [
        Tier.JUNIOR,
        Tier.SENIOR,
        Tier.STAFF,
    ]
    assert tasks[1].blocked_by == (tasks[0].task_name,)
    assert json.loads(json.dumps(finalized.to_payload()))["schema_version"]


def test_sparse_gawd_template_placeholders_do_not_become_plan_data(tmp_path) -> None:
    draft_file = create_sparse_gawd_draft_file(tmp_path)

    draft = parse_sparse_gawd_draft(draft_file.path)
    workflow_plan = build_durable_workflow_plan(build_reviewable_gawd_draft(draft))

    assert draft.execution_milestones == ()
    assert draft.service_levels == ()
    assert draft.input_bounds == ()
    assert draft.interface_contracts == ()
    assert draft.idempotency_replay == ()
    assert draft.observability == ()
    assert draft.dependencies == ()
    assert draft.security_access == ()
    assert draft.backpressure_cost == ()
    assert draft.rollout_migration_rollback == ()
    assert draft.risk_synthesis == ()
    assert draft.known_limitations == ()
    assert [milestone.happy_path_step for milestone in workflow_plan.milestones] == [
        "Operator approves finalized GAWD doc.",
        "Saga executes approved task graph.",
        "Verification evidence is recorded.",
    ]


def test_full_gawd_expansion_drives_durable_workflow_scaffold(tmp_path) -> None:
    draft_file = create_sparse_gawd_draft_file(tmp_path)
    draft_file.path.write_text(
        """# THE GAWD DOC - Mini

**Project:** SaaS Pilot | **Version:** v4-mini | **Status:** SPARSE_DRAFT

## 1. Theory of the System

A milestone-gated SaaS build saga with durable verification.

## 2. Why This Exists

Build a small SaaS feature without losing approval and rollback boundaries.

## 3. Happy Path / Golden Flow

1. Approve the implementation contract.
2. Build the feature.
3. Verify in staging.

## 4. This Version - Scope & Non-Goals

**In scope.**
- Build one feature behind a flag.

**Cut (non-goals).**
- No production deploy.

## 5. Core Design

**Unit of work.** one approved milestone

**Lifecycle.**
- planned -> dispatched -> verified -> complete

**Data model.**
- milestone row plus evidence artifact

## 6. The Failure That Matters Most

Staging migration corrupts seeded test data.

## 7. Verification

- Unit tests pass.
- Staging smoke test passes.

## 8. Execution Milestones

- M1 - Scaffold flagged feature and tests.
- M2 - Run staging migration and smoke check.

## 9. Operational Contract

**Service levels.**
- Recovery from failed staging check under 15 minutes.

**Input bounds.**
- One linked repo and one feature flag per saga.

**Interface contracts.**
- Feature flag name remains additive and backward compatible.

**Idempotency / replay.**
- Milestone retry key is saga_id plus milestone_id.

**Observability.**
- Record test log and staging smoke result.

**Dependencies.**
- PostgreSQL staging database.

**Security / access.**
- Staging database credential is brokered and never written to ledger.

**Backpressure / cost.**
- At most one staging migration runs per target project.

## 10. Rollout / Migration / Rollback

- Staging deploy and migration require explicit approval.
- Rollback restores the pre-migration snapshot.

## 11. Risk Synthesis / Known Limitations

**Risk synthesis.**
- Migration safety is highest risk; mitigation confidence medium.

**Known limitations.**
- Production rollout is outside this version.

## 12. Decision Log

- D1 - Use a feature flag.

## 13. If I Had 2 More Weeks

- Add canary metrics.
""",
        encoding="utf-8",
    )

    draft = parse_sparse_gawd_draft(draft_file.path)
    workflow_plan = build_durable_workflow_plan(build_reviewable_gawd_draft(draft))

    assert draft.execution_milestones == (
        "M1 - Scaffold flagged feature and tests.",
        "M2 - Run staging migration and smoke check.",
    )
    assert draft.service_levels == ("Recovery from failed staging check under 15 minutes.",)
    assert draft.input_bounds == ("One linked repo and one feature flag per saga.",)
    assert draft.idempotency_replay == ("Milestone retry key is saga_id plus milestone_id.",)
    assert draft.dependencies == ("PostgreSQL staging database.",)
    assert [milestone.happy_path_step for milestone in workflow_plan.milestones] == [
        "M1 - Scaffold flagged feature and tests.",
        "M2 - Run staging migration and smoke check.",
    ]
    assert any(
        rule.source_section == "Operational Contract" for rule in workflow_plan.derivation_rules
    )
    assert any(
        rule.source_section == "Rollout / Migration / Rollback"
        for rule in workflow_plan.derivation_rules
    )
    assert any(
        rule.source_section == "Risk Synthesis / Known Limitations"
        for rule in workflow_plan.derivation_rules
    )
    second_step = workflow_plan.steps[1]
    assert second_step.approval_required is True
    assert "input bound: One linked repo and one feature flag per saga." in second_step.inputs
    assert "dependency: PostgreSQL staging database." in second_step.inputs
    assert (
        "security/access constraint: Staging database credential is brokered "
        "and never written to ledger."
    ) in second_step.side_effects
    assert (
        "observability: Record test log and staging smoke result." in second_step.evidence_to_record
    )
    assert (
        "service level: Recovery from failed staging check under 15 minutes."
        in second_step.evidence_to_record
    )
    assert "pre-migration snapshot" in second_step.compensation_or_rollback
    assert "saga_id plus milestone_id" in second_step.retry_policy


def test_saas_archetype_planner_feeds_workflow_plan_when_milestones_are_sparse(
    tmp_path,
) -> None:
    draft_file = create_sparse_gawd_draft_file(tmp_path)
    draft_file.path.write_text(
        """# THE GAWD DOC - Mini

**Project:** Agent CRM SaaS | **Version:** v4-mini | **Status:** SPARSE_DRAFT

## 1. Theory of the System

A B2B workflow SaaS with AI summaries, Stripe billing, OAuth integration sync,
analytics dashboard, staging deploy, and repo automation.

## 2. Why This Exists

Help teams route customer follow-up work through a durable workflow.

## 3. Happy Path / Golden Flow

1. User signs in.
2. User reviews a customer.
3. User approves a follow-up.

## 4. This Version - Scope & Non-Goals

**In scope.**
- Build the first approved workflow.

**Cut (non-goals).**
- No production deploy without approval.

## 5. Core Design

**Unit of work.** one customer follow-up workflow

**Lifecycle.**
- created -> reviewed -> approved -> completed

**Data model.**
- customers, followups, approvals

## 6. The Failure That Matters Most

AI summary sends an unapproved customer message.

## 7. Verification

- Unit tests pass.
- Workflow smoke test passes.
""",
        encoding="utf-8",
    )

    draft = parse_sparse_gawd_draft(draft_file.path)
    workflow_plan = build_durable_workflow_plan(build_reviewable_gawd_draft(draft))

    milestone_ids = {milestone.milestone_id for milestone in workflow_plan.milestones}
    assert "saas_m01_product_contract_scope_freeze" in milestone_ids
    assert "b2b_m01_tenant_roles_workflow_states" in milestone_ids
    assert "ai_m01_model_routing_eval_cost_review" in milestone_ids
    assert "commerce_m01_payment_ledger_refund_gates" in milestone_ids
    assert "integration_m01_oauth_webhooks_retries" in milestone_ids
    assert "deploy_m01_environment_release_rollback_gate" in milestone_ids
    assert any(
        rule.source_section == "SaaS Archetype Planner" for rule in workflow_plan.derivation_rules
    )
    ai_step = next(
        step
        for step in workflow_plan.steps
        if step.milestone_id == "ai_m01_model_routing_eval_cost_review"
    )
    assert ai_step.approval_required is True
    assert "SaaS Archetype Planner" in ai_step.source_sections
    assert "eval_result" in ai_step.evidence_to_record
    assert any(item.startswith("archetype entry criterion:") for item in ai_step.inputs)


def test_sparse_gawd_markdown_bold_is_not_treated_as_bullet(tmp_path) -> None:
    draft_file = create_sparse_gawd_draft_file(tmp_path)
    draft_file.path.write_text(
        """# THE GAWD DOC - Mini

**Project:** BestAnswers Bot | **Version:** v4-mini | **Status:** SPARSE_DRAFT

## 1. Theory of the System

**Archetype:** Crowd-sourced Knowledge Aggregator.
**Shape:** Dynamic consensus synthesis.

## 2. Why This Exists

Avoid rigid grading for subjective answers.

## 3. Happy Path / Golden Flow

1. **Creator** creates a quiz.
2. **Participants** answer and vote.

## 4. This Version - Scope & Non-Goals

**In scope.**
- Preserve markdown formatting.

**Cut (non-goals).**
- No deploy.

## 5. Core Design

**Unit of work.** one answer

**Lifecycle.**
- created -> finalized

**Data model.**
- answers table

## 6. The Failure That Matters Most

**Low-quality consensus.** Show raw evidence next to synthesis.

## 7. Verification

- Render finalized markdown.
""",
        encoding="utf-8",
    )

    finalized = build_reviewable_gawd_draft(parse_sparse_gawd_draft(draft_file.path))

    assert "**Archetype:** Crowd-sourced Knowledge Aggregator." in finalized.final_markdown
    assert "1. **Creator** creates a quiz." in finalized.final_markdown
    assert "**Low-quality consensus.**" in finalized.final_markdown
    assert "\n*Archetype:**" not in finalized.final_markdown


# What a staff model emits in its fenced ```toml block: a partial
# durable_workflow_plan.v1 refinement. Identity fields, milestones, and
# derivation rules are deliberately absent - the parser falls back to the
# scaffold for each - while the steps rename m01 and the envelope adds a
# denial, which is exactly the kind of correction a staff verdict makes.
# Literal rather than generated: the renderer that used to produce this
# text had no production caller and is gone. If the plan schema moves,
# parsing this fixture fails, the refinement falls back to the scaffold,
# and the `model_refined` assertion below fails loudly.
_STAFF_REFINED_PLAN_TOML = """\
schema_version = "durable_workflow_plan.v1"

[permission_envelope]
schema_version = "permission_envelope.v1"
autonomous_permissions = [
    "read_repo_context",
    "write_ledger_artifacts",
    "run_local_model_delegates",
    "prepare_isolated_worktrees",
    "request_operator_decisions",
]
denied_without_approval = [
    "merge_to_main",
    "deploy",
    "purchase_or_spend",
    "external_communications",
    "secret_or_credential_access",
    "destructive_file_operations",
    "external_ai_synthesis",
]
risks = ["Permission envelope is heuristic and must be operator-approved before execution."]

[[steps]]
step_id = "step_m01_draft_the_spec"
name = "Staff refined draft checkpoint"
milestone_id = "m01_draft_the_spec"
source_sections = ["Execution Milestones", "Happy Path / Golden Flow", "Core Design"]
durable_boundary_reason = "GAWD milestone checkpoint."
inputs = ["approved milestone input from happy path: Draft the spec."]
outputs = ["Durable evidence that milestone completed: Draft the spec."]
side_effects = ["write ledger artifacts"]
idempotency_key = "draft:m01_draft_the_spec"
retry_policy = "Retry only idempotent substeps."
timeout_policy = "Fail closed and record a blocking artifact."
compensation_or_rollback = "If compensation cannot be proven safe, create an approval gate."
approval_required = false
evidence_to_record = ["milestone result artifact for m01_draft_the_spec"]
derived_by = "senior_spec_completion_then_staff_final_verdict"

[[steps]]
step_id = "step_m02_run_staging_checks"
name = "Run staging checks."
milestone_id = "m02_run_staging_checks"
source_sections = ["Execution Milestones", "Happy Path / Golden Flow", "Core Design"]
durable_boundary_reason = "GAWD milestone checkpoint."
inputs = ["approved milestone input from happy path: Run staging checks."]
outputs = ["Durable evidence that milestone completed: Run staging checks."]
side_effects = ["write ledger artifacts"]
idempotency_key = "draft:m02_run_staging_checks"
retry_policy = "Retry only idempotent substeps."
timeout_policy = "Fail closed and record a blocking artifact."
compensation_or_rollback = "If compensation cannot be proven safe, create an approval gate."
approval_required = false
evidence_to_record = ["milestone result artifact for m02_run_staging_checks"]
derived_by = "senior_spec_completion_then_staff_final_verdict"
"""


def test_durable_workflow_plan_can_be_refined_from_staff_toml(tmp_path) -> None:
    draft_file = create_sparse_gawd_draft_file(tmp_path)
    draft_file.path.write_text(
        """# THE GAWD DOC - Mini

**Project:** Staff Plan | **Version:** v4-mini | **Status:** SPARSE_DRAFT

## 1. Theory of the System

Durable workflow from GAWD.

## 2. Why This Exists

Test staff refinement.

## 3. Happy Path / Golden Flow

1. Draft the spec.
2. Run staging checks.

## 4. This Version - Scope & Non-Goals

**In scope.**
- Refine workflow plan.

**Cut (non-goals).**
- No deploy.

## 5. Core Design

**Unit of work.** one workflow plan

**Lifecycle.**
- drafted -> approved

**Data model.**
- workflow TOML

## 6. The Failure That Matters Most

Generated code runs before approval.

## 7. Verification

- TOML parses.
""",
        encoding="utf-8",
    )
    scaffold = build_durable_workflow_plan(
        build_reviewable_gawd_draft(parse_sparse_gawd_draft(draft_file.path))
    )
    model_toml = _STAFF_REFINED_PLAN_TOML
    run_result = PowWowRunResult(
        executor="FakeExecutor",
        mode="cli",
        pow_wow_id="pow",
        target_project_id="target",
        target_project_path=str(tmp_path),
        status="COMPLETED",
        output_summary="staff approved",
        tasks=(
            PowWowTaskResult(
                task_name="staff_final_verdict",
                role="staff_final_verdict",
                status="completed",
                summary="approved",
                artifacts=(
                    PowWowArtifact(
                        artifact_type="cli_agent_run",
                        task_name="staff_final_verdict",
                        content={
                            "schema_version": "cli_agent_run.v1",
                            "output": f"APPROVE\n```toml\n{model_toml}\n```",
                        },
                    ),
                ),
            ),
        ),
    )

    refined, evidence = refine_durable_workflow_plan_from_run_result(scaffold, run_result)

    assert evidence["status"] == "model_refined"
    assert evidence["source_task_name"] == "staff_final_verdict"
    assert refined.steps[0].name == "Staff refined draft checkpoint"
    assert "external_ai_synthesis" in refined.permission_envelope.denied_without_approval


def test_durable_workflow_plan_uses_scaffold_when_model_output_is_invalid(tmp_path) -> None:
    scaffold = build_durable_workflow_plan(
        build_reviewable_gawd_draft(
            parse_sparse_gawd_draft(create_sparse_gawd_draft_file(tmp_path).path)
        )
    )
    run_result = PowWowRunResult(
        executor="FakeExecutor",
        mode="cli",
        pow_wow_id="pow",
        target_project_id="target",
        target_project_path=str(tmp_path),
        status="COMPLETED",
        output_summary="staff output invalid",
        tasks=(
            PowWowTaskResult(
                task_name="staff_final_verdict",
                role="staff_final_verdict",
                status="completed",
                summary="invalid",
                artifacts=(
                    PowWowArtifact(
                        artifact_type="cli_agent_run",
                        task_name="staff_final_verdict",
                        content={"schema_version": "cli_agent_run.v1", "output": "APPROVE"},
                    ),
                ),
            ),
        ),
    )

    refined, evidence = refine_durable_workflow_plan_from_run_result(scaffold, run_result)

    assert evidence["status"] == "scaffold_used"
    assert refined == scaffold


def test_build_gawd_review_tasks_embed_workflow_scaffold(tmp_path) -> None:
    draft_file = create_sparse_gawd_draft_file(tmp_path)
    draft_file.path.write_text(
        """# THE GAWD DOC - Mini

**Project:** Scaffold Prompt SaaS | **Version:** v4-mini | **Status:** SPARSE_DRAFT

## 1. Theory of the System

A B2B workflow SaaS with Stripe billing and a staging deploy.

## 2. Why This Exists

Prove the scaffold reaches the senior/staff prompts.

## 3. Happy Path / Golden Flow

1. User signs in.
2. User approves a follow-up.

## 4. This Version - Scope & Non-Goals

**In scope.**
- Build the first approved workflow.

**Cut (non-goals).**
- No production deploy without approval.

## 5. Core Design

**Unit of work.** one follow-up workflow

**Lifecycle.**
- created -> approved

**Data model.**
- followups

## 6. The Failure That Matters Most

Billing runs without approval.

## 7. Verification

- Unit tests pass.
""",
        encoding="utf-8",
    )
    draft = parse_sparse_gawd_draft(draft_file.path)
    scaffold = build_durable_workflow_plan(build_reviewable_gawd_draft(draft))

    tasks = build_gawd_review_tasks(draft, scaffold)

    senior = next(task for task in tasks if task.task_name == "senior_spec_completion")
    staff = next(task for task in tasks if task.task_name == "staff_final_verdict")
    for description in (senior.description, staff.description):
        assert "Deterministic scaffold" in description
        assert "saas_m01_product_contract_scope_freeze" in description
        assert "commerce_m01_payment_ledger_refund_gates" in description
        assert "[approval_required]" in description
    # Without a scaffold the prompts stay unchanged in shape.
    bare = build_gawd_review_tasks(draft)
    assert "Deterministic scaffold" not in bare[1].description


def test_plan_payload_rejects_steps_that_do_not_cover_milestones(tmp_path) -> None:
    scaffold = build_durable_workflow_plan(
        build_reviewable_gawd_draft(
            parse_sparse_gawd_draft(create_sparse_gawd_draft_file(tmp_path).path)
        )
    )
    # The scaffold payload as the ledger would hold it: the JSON round trip
    # turns asdict() tuples into the mutable lists a model-emitted plan has.
    payload = json.loads(json.dumps(scaffold.to_payload()))
    # An agent-refined plan that renames a step's milestone linkage leaves the
    # original milestone without entry/exit criteria, evidence, or approval
    # gates at persistence time. That plan must be rejected, not merged.
    payload["steps"][0]["milestone_id"] = "made_up_milestone"

    with pytest.raises(ValueError, match="do not cover milestones"):
        parse_durable_workflow_plan_payload(payload, fallback=scaffold)


def test_incoherent_model_plan_falls_back_to_scaffold(tmp_path) -> None:
    scaffold = build_durable_workflow_plan(
        build_reviewable_gawd_draft(
            parse_sparse_gawd_draft(create_sparse_gawd_draft_file(tmp_path).path)
        )
    )
    # The scaffold payload as the ledger would hold it: the JSON round trip
    # turns asdict() tuples into the mutable lists a model-emitted plan has.
    payload = json.loads(json.dumps(scaffold.to_payload()))
    payload["steps"] = [payload["steps"][0] | {"milestone_id": "made_up_milestone"}]
    model_output = "APPROVE\n```json\n" + json.dumps(payload) + "\n```"
    run_result = PowWowRunResult(
        executor="FakeExecutor",
        mode="cli",
        pow_wow_id="pow",
        target_project_id="target",
        target_project_path=str(tmp_path),
        status="COMPLETED",
        output_summary="staff emitted incoherent plan",
        tasks=(
            PowWowTaskResult(
                task_name="staff_final_verdict",
                role="staff_final_verdict",
                status="completed",
                summary="approved",
                artifacts=(
                    PowWowArtifact(
                        artifact_type="cli_agent_run",
                        task_name="staff_final_verdict",
                        content={
                            "schema_version": "cli_agent_run.v1",
                            "output": model_output,
                        },
                    ),
                ),
            ),
        ),
    )

    refined, evidence = refine_durable_workflow_plan_from_run_result(scaffold, run_result)

    assert evidence["status"] == "scaffold_used"
    assert refined == scaffold
    assert any("do not cover milestones" in error["error"] for error in evidence["parse_errors"])


def _build_reviewable_gawd_markdown(tmp_path) -> str:
    finalized = build_reviewable_gawd_draft(
        parse_sparse_gawd_draft(create_sparse_gawd_draft_file(tmp_path).path)
    )
    return finalized.final_markdown


def _build_gawd_review_run_result(
    tmp_path,
    *,
    status: PowWowRunStatus,
    senior_output: str | None = None,
    staff_output: str | None = None,
    failed_task: str | None = None,
    risks: tuple[str, ...] = (),
) -> PowWowRunResult:
    tasks = []
    for task_name, output in (
        ("senior_spec_completion", senior_output),
        ("staff_final_verdict", staff_output),
    ):
        artifacts = ()
        if output is not None:
            artifacts = (
                PowWowArtifact(
                    artifact_type="cli_agent_run",
                    task_name=task_name,
                    content={"schema_version": "cli_agent_run.v1", "output": output},
                ),
            )
        tasks.append(
            PowWowTaskResult(
                task_name=task_name,
                role=task_name,
                status="failed" if task_name == failed_task else "completed",
                summary=task_name,
                artifacts=artifacts,
            )
        )
    return PowWowRunResult(
        executor="FakeExecutor",
        mode="cli",
        pow_wow_id="pow",
        target_project_id="target",
        target_project_path=str(tmp_path),
        status=status,
        output_summary="finalization run",
        tasks=tuple(tasks),
        risks=risks,
    )


def test_apply_run_result_merges_model_output_on_success(tmp_path) -> None:
    markdown = _build_reviewable_gawd_markdown(tmp_path)
    senior = (
        "Expanded operational contract: recovery under 15 minutes.\n"
        '```toml\nschema_version = "durable_workflow_plan.v1"\n```'
    )
    staff = "APPROVE. Contract is coherent; deploy stays gated."
    run_result = _build_gawd_review_run_result(
        tmp_path, status="COMPLETED", senior_output=senior, staff_output=staff
    )

    merged, note = merge_pow_wow_result_into_gawd_review_markdown(markdown, run_result)

    assert note["status"] == "merged"
    assert "## Senior Spec Completion (Model Output)" in merged
    assert "Expanded operational contract" in merged
    # The raw TOML never belonged in the prose contract, and the sidecar it was
    # redirected to is gone: nothing ever read it back, and the milestones it
    # carried now render into this document's Execution Milestones section,
    # where `compile_design_doc` looks for them.
    assert "schema_version = " not in merged
    assert "(durable workflow plan rendered into the Execution Milestones section)" in merged
    # The hardcoded template verdict is replaced by the staff model's verdict.
    assert "Ready for operator review; execution remains blocked" not in merged
    assert "APPROVE. Contract is coherent" in merged
    assert merged.index("## Senior Spec Completion") < merged.index("## Permission Envelope")


def test_apply_run_result_marks_failed_finalization(tmp_path) -> None:
    markdown = _build_reviewable_gawd_markdown(tmp_path)
    run_result = _build_gawd_review_run_result(
        tmp_path,
        status="FAILED",
        failed_task="senior_spec_completion",
        risks=("Junior delegate failed: model not loaded",),
    )

    merged, note = merge_pow_wow_result_into_gawd_review_markdown(markdown, run_result)

    assert note["status"] == "finalization_failed"
    assert "**Status:** FINALIZATION_FAILED" in merged
    assert "**Status:** FINALIZED_DRAFT" not in merged
    assert "FINALIZATION_FAILED. Do not approve this draft." in merged
    assert "senior_spec_completion: failed" in merged
    assert "model not loaded" in merged
    assert "Ready for operator review" not in merged


def test_apply_run_result_keeps_template_when_no_model_output(tmp_path) -> None:
    markdown = _build_reviewable_gawd_markdown(tmp_path)
    run_result = _build_gawd_review_run_result(tmp_path, status="DRY_RUN_COMPLETED")

    merged, note = merge_pow_wow_result_into_gawd_review_markdown(markdown, run_result)

    assert note["status"] == "no_model_output"
    assert merged == markdown


def test_build_gawd_review_tasks_embed_draft_content(tmp_path) -> None:
    draft = parse_sparse_gawd_draft(create_sparse_gawd_draft_file(tmp_path).path)
    finalized = build_reviewable_gawd_draft(draft)

    tasks = build_gawd_review_tasks(draft, draft_markdown=finalized.final_markdown)

    for task in tasks:
        assert "Sparse GAWD draft content (source:" in task.description
        assert "## 1. Theory of the System" in task.description
    bare = build_gawd_review_tasks(draft)
    assert "Sparse GAWD draft content" not in bare[0].description


def test_finalized_intake_milestones_are_readable_by_the_compiler(tmp_path) -> None:
    """The two lanes meet, which is the whole point of rendering these.

    Intake derived milestones and rendered them to a TOML sidecar nothing read,
    beside a document that compiled with `no_milestones`. Every GAWD doc that
    ever reached the cockpit had been hand-authored past this step. This asserts
    the round trip: what intake produces is what `compile_design_doc` parses.

    `outputs` is deliberately absent from the assertion. Those entries are free
    text describing deliverables, and `Artifacts:` takes a closed vocabulary, so
    the renderer derives the kind rather than copying the prose.
    """

    from local_first_agent_os.new_project_intake import (
        render_execution_milestones_markdown,
        replace_execution_milestones_section,
    )
    from local_first_agent_os.work_units.design_doc import parse_design_doc

    draft = parse_sparse_gawd_draft(create_sparse_gawd_draft_file(tmp_path).path)
    finalized = build_reviewable_gawd_draft(draft)
    plan = build_durable_workflow_plan(finalized)

    document = replace_execution_milestones_section(
        finalized.final_markdown,
        render_execution_milestones_markdown(plan),
    )
    parsed = parse_design_doc(document, design_doc_id="intake_roundtrip")

    assert len(parsed.milestone_candidates) == len(plan.steps)


def test_a_scaffold_that_gates_every_step_still_describes_work(tmp_path) -> None:
    """An approval flag is a gate on the work, not a milestone instead of it.

    The scaffold marks every step `approval_required`. Reading that as "this step
    is a review" rendered a plan of operator reviews and no implementation - a
    document that compiles and describes nothing being built.
    """

    from local_first_agent_os.new_project_intake import render_execution_milestones_markdown

    draft = parse_sparse_gawd_draft(create_sparse_gawd_draft_file(tmp_path).path)
    plan = build_durable_workflow_plan(build_reviewable_gawd_draft(draft))

    rendered = render_execution_milestones_markdown(plan)

    assert "Phase: IMPLEMENT" in rendered
    assert "Executor: review.operator" not in rendered
    assert "Artifacts: operator_approval" not in rendered


def test_a_refined_step_carries_its_own_phase_into_the_milestone() -> None:
    """The phase is the model's decision, recorded, not the renderer's guess.

    A rendered phase decides which artifact the milestone must produce, so
    inferring one from step text would put structure in an operator's plan that
    nobody chose and fail its evidence gate when the guess was wrong. Asking for
    it makes it data: visible in the finalized document, before anything runs.
    """

    from local_first_agent_os.new_project_intake import (
        _parse_durable_workflow_step,
        render_execution_milestones_markdown,
    )

    base = {
        "step_id": "step_m01",
        "name": "Prove the ACL denies",
        "milestone_id": "m01",
        "source_sections": [],
        "durable_boundary_reason": "runs the suite",
        "inputs": [],
        "outputs": [],
        "side_effects": [],
        "idempotency_key": "k",
        "retry_policy": "none",
        "timeout_policy": "none",
        "compensation_or_rollback": "none",
        "approval_required": False,
        "evidence_to_record": ["the suite passes"],
        "derived_by": "senior",
        "phase": "VERIFY",
    }
    step = _parse_durable_workflow_step(base)
    plan = DurableWorkflowPlan(
        draft_id="draft",
        project="Test",
        source_draft_path="draft.txt",
        contract_path="configs/durable_workflow_plan.toml",
        milestones=(),
        derivation_rules=(),
        steps=(step,),
        permission_envelope=PermissionEnvelope(
            autonomous_permissions=(),
            requested_permissions=(),
            denied_without_approval=(),
            risks=(),
        ),
        approval_boundary="operator approval",
        code_generation_policy="worktree only",
    )

    rendered = render_execution_milestones_markdown(plan)

    assert step.phase == "VERIFY"
    assert "Phase: VERIFY" in rendered
    assert "Executor: verify.tests" in rendered
    assert "Artifacts: test_result" in rendered


def test_a_phase_that_does_not_exist_is_refused_not_coerced() -> None:
    """Coercing it would hide the mistake until a later milestone failed.

    The phase decides the required artifact, so silently turning an unknown one
    into IMPLEMENT would produce a plan that compiles and then fails its evidence
    gate with nothing pointing back at the step that was wrong.
    """

    from local_first_agent_os.new_project_intake import _parse_durable_workflow_step

    payload = {
        "step_id": "step_m01",
        "name": "Do a thing",
        "milestone_id": "m01",
        "source_sections": [],
        "durable_boundary_reason": "",
        "inputs": [],
        "outputs": [],
        "side_effects": [],
        "idempotency_key": "k",
        "retry_policy": "none",
        "timeout_policy": "none",
        "compensation_or_rollback": "none",
        "approval_required": False,
        "evidence_to_record": [],
        "derived_by": "senior",
        "phase": "SHIPPING",
    }

    with pytest.raises(ValueError, match="SHIPPING"):
        _parse_durable_workflow_step(payload)
