# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from local_first_agent_os.archetype_planners import plan_saas_archetype
from local_first_agent_os.new_project_intake import (
    create_sparse_gawd_draft_file,
    parse_sparse_gawd_draft,
)


def test_saas_archetype_planner_returns_none_for_non_saas_draft(tmp_path) -> None:
    draft_file = create_sparse_gawd_draft_file(tmp_path)
    draft_file.path.write_text(
        """# THE GAWD DOC - Mini

**Project:** Ledger Cleanup | **Version:** v4-mini | **Status:** SPARSE_DRAFT

## 1. Theory of the System

A local maintenance script.

## 2. Why This Exists

Clean up old test artifacts.

## 3. Happy Path / Golden Flow

1. Scan files.
2. Remove expired files.
3. Record the result.
""",
        encoding="utf-8",
    )

    assert plan_saas_archetype(parse_sparse_gawd_draft(draft_file.path)) is None


def test_saas_archetype_planner_applies_all_table_overlays(tmp_path) -> None:
    draft_file = create_sparse_gawd_draft_file(tmp_path)
    draft_file.path.write_text(
        """# THE GAWD DOC - Mini

**Project:** Omni SaaS | **Version:** v4-mini | **Status:** SPARSE_DRAFT

## 1. Theory of the System

A SaaS web app for internal ops, B2B workflow approvals, self-serve productivity,
marketplace leads, analytics reporting, AI agents, OAuth integration sync,
developer repo automation, Stripe billing, and legal PII workflows.

## 2. Why This Exists

Coordinate customer work without losing auditability.

## 3. Happy Path / Golden Flow

1. User signs in.
2. User runs the workflow.
3. Admin reviews analytics.

## 10. Rollout / Migration / Rollback

- Staging deploy then production release with rollback.
""",
        encoding="utf-8",
    )

    plan = plan_saas_archetype(parse_sparse_gawd_draft(draft_file.path))

    assert plan is not None
    assert plan.archetype == "saas"
    assert set(plan.applied_overlays) >= {
        "internal_ops_tool",
        "b2b_workflow_saas",
        "self_serve_productivity_saas",
        "marketplace_lead_platform",
        "data_analytics_saas",
        "ai_workflow_saas",
        "integration_automation_saas",
        "developer_tool_infra_saas",
        "commerce_billing_saas",
        "regulated_sensitive_data_saas",
        "deploy_overlay",
    }
    milestone_ids = {milestone.milestone_id for milestone in plan.milestones}
    assert "saas_m01_product_contract_scope_freeze" in milestone_ids
    assert "ops_m01_admin_roles_audit" in milestone_ids
    assert "b2b_m01_tenant_roles_workflow_states" in milestone_ids
    assert "productivity_m01_onboarding_export_support" in milestone_ids
    assert "marketplace_m01_roles_moderation_messaging" in milestone_ids
    assert "analytics_m01_ingestion_freshness_correctness" in milestone_ids
    assert "ai_m01_model_routing_eval_cost_review" in milestone_ids
    assert "integration_m01_oauth_webhooks_retries" in milestone_ids
    assert "devtool_m01_repo_permissions_sandbox_artifacts" in milestone_ids
    assert "commerce_m01_payment_ledger_refund_gates" in milestone_ids
    assert "sensitive_m01_pii_retention_audit_review" in milestone_ids
    assert "deploy_m01_environment_release_rollback_gate" in milestone_ids
    assert any(milestone.approval_required for milestone in plan.milestones)
