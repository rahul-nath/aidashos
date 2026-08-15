# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic archetype planners for GAWD intake.

Archetype planners are compile-time scaffolds. They do not own runtime state;
their output is folded into ``DurableWorkflowPlan`` and later persisted through
the existing saga milestone ledger.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ArchetypeMilestoneTemplate:
    milestone_id: str
    name: str
    description: str
    include_when: tuple[str, ...]
    entry_criteria: tuple[str, ...]
    exit_criteria: tuple[str, ...]
    required_evidence: tuple[str, ...]
    approval_required: bool
    full_gawd_sources: tuple[str, ...]


@dataclass(frozen=True)
class ArchetypeOverlay:
    overlay_id: str
    name: str
    triggers: tuple[str, ...]
    milestones: tuple[ArchetypeMilestoneTemplate, ...]


@dataclass(frozen=True)
class ArchetypePlan:
    archetype: str
    confidence: str
    applied_overlays: tuple[str, ...]
    milestones: tuple[ArchetypeMilestoneTemplate, ...]
    blocked_questions: tuple[str, ...]


def plan_saas_archetype(draft: Any) -> ArchetypePlan | None:
    """Return a SaaS execution scaffold when the draft is SaaS-shaped.

    The planner is intentionally conservative: it only adds reusable execution
    structure and approval gates. Senior/staff refinement still owns the final
    milestone plan before operator approval.
    """

    text = _draft_search_text(draft)
    if not text:
        return None

    matched_base = _matched_keywords(text, _BASE_SAAS_TRIGGERS)
    matched_overlays = tuple(
        overlay for overlay in _SAAS_OVERLAYS if _matched_keywords(text, overlay.triggers)
    )
    if not matched_base and not _has_saas_defining_overlay(matched_overlays):
        return None

    milestones = _dedupe_milestones(
        (
            *_BASE_SAAS_MILESTONES,
            *(milestone for overlay in matched_overlays for milestone in overlay.milestones),
        )
    )
    confidence = "high" if matched_base and matched_overlays else "medium"
    blocked_questions = _blocked_questions(text, matched_overlays)
    return ArchetypePlan(
        archetype="saas",
        confidence=confidence,
        applied_overlays=tuple(overlay.overlay_id for overlay in matched_overlays),
        milestones=milestones,
        blocked_questions=blocked_questions,
    )


def _template(
    milestone_id: str,
    name: str,
    description: str,
    *,
    include_when: tuple[str, ...],
    entry_criteria: tuple[str, ...],
    exit_criteria: tuple[str, ...],
    required_evidence: tuple[str, ...],
    approval_required: bool = False,
    full_gawd_sources: tuple[str, ...],
) -> ArchetypeMilestoneTemplate:
    return ArchetypeMilestoneTemplate(
        milestone_id=milestone_id,
        name=name,
        description=description,
        include_when=include_when,
        entry_criteria=entry_criteria,
        exit_criteria=exit_criteria,
        required_evidence=required_evidence,
        approval_required=approval_required,
        full_gawd_sources=full_gawd_sources,
    )


_BASE_SAAS_TRIGGERS = (
    "saas",
    "web app",
    "app users",
    "user account",
    "multi tenant",
    "multi-tenant",
    "subscription",
    "stripe",
    "marketplace",
    "dashboard",
    "crm",
    "customer portal",
    "productivity app",
    "staging deploy",
    "production deploy",
)

_BASE_SAAS_MILESTONES = (
    _template(
        "saas_m01_product_contract_scope_freeze",
        "Product contract and scope freeze",
        "Freeze users, core workflow, non-goals, success criteria, and approval gates.",
        include_when=("Any SaaS-shaped project without explicit execution milestones.",),
        entry_criteria=("Sparse or finalized GAWD draft exists.",),
        exit_criteria=("Scope, user roles, non-goals, and success criteria are explicit.",),
        required_evidence=("finalized_gawd_doc", "permission_envelope", "workflow_plan"),
        full_gawd_sources=(
            "This Version - Scope & Non-Goals",
            "Risk Synthesis / Known Limitations",
            "Permission Envelope",
        ),
    ),
    _template(
        "saas_m02_app_auth_data_scaffold",
        "App/auth/data scaffold",
        "Create the app skeleton, authentication boundary, roles, and initial data model.",
        include_when=("Any user-facing or team-facing SaaS project.",),
        entry_criteria=("Product contract milestone is complete.",),
        exit_criteria=("Auth boundary, roles, and durable entities are represented.",),
        required_evidence=("schema_or_model_diff", "auth_boundary_notes", "test_log"),
        full_gawd_sources=("Core Design", "Security / Access", "Data Model"),
    ),
    _template(
        "saas_m03_core_domain_workflow",
        "Core domain workflow",
        "Implement the smallest end-to-end domain workflow behind the approved scope.",
        include_when=("Any SaaS project with a user-visible workflow.",),
        entry_criteria=("App/auth/data scaffold is available.",),
        exit_criteria=("Primary workflow can run end to end with seeded data.",),
        required_evidence=("workflow_demo_notes", "test_log", "artifact_summary"),
        full_gawd_sources=("Happy Path / Golden Flow", "Core Design", "Verification"),
    ),
    _template(
        "saas_m04_persistence_migration_safety",
        "Persistence and migration safety",
        "Define migration plan, rollback or forward-fix behavior, and data validation.",
        include_when=("Any SaaS project with persistent data.",),
        entry_criteria=("Data entities and migration need are explicit.",),
        exit_criteria=("Migration can be tested safely before production use.",),
        required_evidence=("migration_status", "rollback_or_forward_fix_plan", "test_log"),
        approval_required=True,
        full_gawd_sources=("Data Model", "Rollout / Migration / Rollback", "Verification"),
    ),
    _template(
        "saas_m05_verification_fixtures",
        "Verification fixtures and smoke proof",
        "Create seeded data, unit/integration coverage, and the smoke path evidence.",
        include_when=("Any SaaS project that will be executed by agents.",),
        entry_criteria=("Core workflow and data model are available.",),
        exit_criteria=("Tests and smoke proof cover the approved happy path.",),
        required_evidence=("test_log", "seed_data_notes", "smoke_result"),
        full_gawd_sources=("Verification", "Happy Path / Golden Flow"),
    ),
    _template(
        "saas_m06_staging_live_checks",
        "Staging and live integration checks",
        "Run staging or realistic integration checks and record pass/fail evidence.",
        include_when=("Any SaaS project that mentions staging, deploy, or external services.",),
        entry_criteria=("Verification fixtures exist and target environment is approved.",),
        exit_criteria=("Live check evidence is recorded or the milestone blocks safely.",),
        required_evidence=("live_check_result", "health_check_result", "log_excerpt"),
        approval_required=True,
        full_gawd_sources=("Verification", "Dependency Map", "Rollout / Migration / Rollback"),
    ),
    _template(
        "saas_m07_release_approval_packet",
        "Release and merge approval packet",
        "Prepare diff summary, risk notes, rollback plan, and operator approval request.",
        include_when=("Any SaaS project with code changes.",),
        entry_criteria=("Verification evidence exists.",),
        exit_criteria=("Operator has a concise approval packet for merge or release.",),
        required_evidence=("diff_summary", "risk_summary", "approval_request"),
        approval_required=True,
        full_gawd_sources=("Decision Log", "Risk Synthesis", "Rollout / Migration / Rollback"),
    ),
    _template(
        "saas_m08_observability_support_handoff",
        "Observability and support handoff",
        "Record inspection hooks, known failure queries, and support/operator notes.",
        include_when=("Any SaaS project expected to keep running after the build.",),
        entry_criteria=("Release packet or final verification exists.",),
        exit_criteria=("Operator can inspect health, evidence, and known limitations.",),
        required_evidence=("operator_handoff", "known_limitations", "inspection_commands"),
        full_gawd_sources=("Observability", "Known Limitations", "Organizational Context"),
    ),
)

_SAAS_OVERLAYS = (
    ArchetypeOverlay(
        overlay_id="internal_ops_tool",
        name="Internal Ops Tool",
        triggers=("internal", "admin", "ops", "backoffice", "dashboard"),
        milestones=(
            _template(
                "ops_m01_admin_roles_audit",
                "Admin roles and audit trail",
                "Define internal roles, privileged actions, and audit evidence.",
                include_when=("Draft is an internal ops/admin/dashboard tool.",),
                entry_criteria=("Internal operators and privileged actions are known.",),
                exit_criteria=("Roles, audit events, and admin boundaries are explicit.",),
                required_evidence=("role_matrix", "audit_event_list", "admin_access_notes"),
                approval_required=True,
                full_gawd_sources=("Security / Access", "Observability"),
            ),
        ),
    ),
    ArchetypeOverlay(
        overlay_id="b2b_workflow_saas",
        name="B2B Workflow SaaS",
        triggers=("b2b", "crm", "customer workflow", "organization", "tenant"),
        milestones=(
            _template(
                "b2b_m01_tenant_roles_workflow_states",
                "Tenant roles and workflow states",
                "Define tenant boundaries, team roles, workflow states, and auditability.",
                include_when=("Draft is team-facing, B2B, CRM, or workflow software.",),
                entry_criteria=("Core workflow and actor types are named.",),
                exit_criteria=("Tenant isolation and workflow transitions are explicit.",),
                required_evidence=("tenant_model_notes", "workflow_state_table", "audit_notes"),
                approval_required=True,
                full_gawd_sources=("Units of Work", "Lifecycle", "Security / Access"),
            ),
        ),
    ),
    ArchetypeOverlay(
        overlay_id="self_serve_productivity_saas",
        name="Self-Serve Productivity SaaS",
        triggers=("self serve", "self-serve", "productivity", "onboarding", "settings"),
        milestones=(
            _template(
                "productivity_m01_onboarding_export_support",
                "Onboarding, settings, and export/support hooks",
                "Define user onboarding, settings, data export, and support surfaces.",
                include_when=("Draft is a self-serve productivity tool.",),
                entry_criteria=("User account and core workflow are known.",),
                exit_criteria=("Onboarding and user-owned data controls are explicit.",),
                required_evidence=("onboarding_flow_notes", "settings_notes", "export_policy"),
                full_gawd_sources=("Outputs", "Security / Access", "Known Limitations"),
            ),
        ),
    ),
    ArchetypeOverlay(
        overlay_id="marketplace_lead_platform",
        name="Marketplace / Lead Platform",
        triggers=(
            "marketplace",
            "lead platform",
            "vendor",
            "buyer",
            "influencer",
            "two sided",
        ),
        milestones=(
            _template(
                "marketplace_m01_roles_moderation_messaging",
                "Two-sided roles, moderation, and messaging gates",
                (
                    "Define participant roles, queue ownership, moderation, and "
                    "outbound messaging gates."
                ),
                include_when=("Draft matches a marketplace, lead queue, or outreach platform.",),
                entry_criteria=("Supply-side and demand-side actors are named.",),
                exit_criteria=("Moderation, spam, and external communication gates are explicit.",),
                required_evidence=("actor_matrix", "moderation_policy", "messaging_approval_gate"),
                approval_required=True,
                full_gawd_sources=(
                    "Interface Contracts",
                    "Security / Access",
                    "Permission Envelope",
                ),
            ),
        ),
    ),
    ArchetypeOverlay(
        overlay_id="data_analytics_saas",
        name="Data / Analytics SaaS",
        triggers=("analytics", "reporting", "report", "metric", "bi", "ranking"),
        milestones=(
            _template(
                "analytics_m01_ingestion_freshness_correctness",
                "Ingestion, freshness, and metric correctness",
                "Define data ingestion, freshness targets, backfills, and metric validation.",
                include_when=("Draft is an analytics, BI, ranking, or reporting tool.",),
                entry_criteria=("Source data and target metrics are named.",),
                exit_criteria=("Freshness, lineage, and correctness checks are explicit.",),
                required_evidence=("data_lineage_notes", "freshness_check", "metric_test_log"),
                full_gawd_sources=("Data Model", "SLA / SLO Targets", "Verification"),
            ),
        ),
    ),
    ArchetypeOverlay(
        overlay_id="ai_workflow_saas",
        name="AI Workflow SaaS",
        triggers=("ai", "ai agent", "llm", "copilot", "document processor", "ai summary"),
        milestones=(
            _template(
                "ai_m01_model_routing_eval_cost_review",
                "Model routing, evals, cost caps, and human review",
                (
                    "Define model choices, evaluation evidence, hallucination "
                    "controls, and cost gates."
                ),
                include_when=(
                    "Draft includes AI, agents, LLMs, copilots, or document processing.",
                ),
                entry_criteria=("AI task boundary and failure mode are named.",),
                exit_criteria=("Eval, replay, human review, and cost controls are explicit.",),
                required_evidence=("eval_result", "model_routing_notes", "cost_cap_notes"),
                approval_required=True,
                full_gawd_sources=("Verification", "Backpressure / Cost", "Failure Semantics"),
            ),
        ),
    ),
    ArchetypeOverlay(
        overlay_id="integration_automation_saas",
        name="Integration / Automation SaaS",
        triggers=("integration", "oauth", "webhook", "sync", "api sync", "zapier"),
        milestones=(
            _template(
                "integration_m01_oauth_webhooks_retries",
                "OAuth, webhooks, idempotency, and retries",
                "Define secret brokerage, webhook semantics, rate limits, and retry safety.",
                include_when=("Draft integrates with external APIs or automation platforms.",),
                entry_criteria=("External systems and auth method are named.",),
                exit_criteria=(
                    "OAuth, webhook, idempotency, and rate-limit behavior is explicit.",
                ),
                required_evidence=("secret_request", "webhook_contract", "retry_policy_notes"),
                approval_required=True,
                full_gawd_sources=("Dependency Map", "Security / Access", "Idempotency and Replay"),
            ),
        ),
    ),
    ArchetypeOverlay(
        overlay_id="developer_tool_infra_saas",
        name="Developer Tool / Infra SaaS",
        triggers=("developer tool", "dev tool", "infra", "ci", "repo", "code agent", "cli"),
        milestones=(
            _template(
                "devtool_m01_repo_permissions_sandbox_artifacts",
                "Repo permissions, sandboxing, and artifact contracts",
                (
                    "Define repo access, sandbox boundaries, logs/artifacts, and "
                    "dangerous-action gates."
                ),
                include_when=("Draft is a developer tool, infra tool, CI tool, or code agent.",),
                entry_criteria=("Target repo/action surface is named.",),
                exit_criteria=(
                    "Repo permissions, sandbox behavior, and artifact contracts are explicit.",
                ),
                required_evidence=("sandbox_policy", "artifact_contract", "dangerous_action_gates"),
                approval_required=True,
                full_gawd_sources=("Security / Access", "Interface Contracts", "Observability"),
            ),
        ),
    ),
    ArchetypeOverlay(
        overlay_id="commerce_billing_saas",
        name="Commerce / Billing SaaS",
        triggers=("billing", "stripe", "payment", "subscription", "invoice", "refund"),
        milestones=(
            _template(
                "commerce_m01_payment_ledger_refund_gates",
                "Payment, billing ledger, and refund gates",
                "Define payment provider boundaries, ledger correctness, refunds, and spend gates.",
                include_when=(
                    "Draft mentions billing, payments, subscriptions, invoices, or refunds.",
                ),
                entry_criteria=("Billing action and provider boundary are named.",),
                exit_criteria=(
                    "Payment actions are approval-bound and ledger correctness is testable.",
                ),
                required_evidence=(
                    "billing_contract",
                    "payment_test_mode_evidence",
                    "refund_policy",
                ),
                approval_required=True,
                full_gawd_sources=("Security / Access", "Verification", "Permission Envelope"),
            ),
        ),
    ),
    ArchetypeOverlay(
        overlay_id="regulated_sensitive_data_saas",
        name="Regulated / Sensitive Data SaaS",
        triggers=(
            "regulated",
            "health",
            "medical",
            "finance",
            "legal",
            "education",
            "pii",
            "hipaa",
        ),
        milestones=(
            _template(
                "sensitive_m01_pii_retention_audit_review",
                "PII, retention, audit, and compliance review",
                (
                    "Define sensitive data handling, retention, audit logging, "
                    "and compliance review gates."
                ),
                include_when=("Draft touches regulated or sensitive user data.",),
                entry_criteria=("Sensitive data categories are named.",),
                exit_criteria=(
                    "PII redaction, retention, access, and audit behavior are explicit.",
                ),
                required_evidence=(
                    "sensitive_data_inventory",
                    "retention_policy",
                    "audit_log_notes",
                ),
                approval_required=True,
                full_gawd_sources=("Security / Access", "Data Model", "Known Limitations"),
            ),
        ),
    ),
    ArchetypeOverlay(
        overlay_id="deploy_overlay",
        name="Deploy / Release Overlay",
        triggers=("deploy", "release", "production", "staging", "canary", "rollback"),
        milestones=(
            _template(
                "deploy_m01_environment_release_rollback_gate",
                "Environment, release, and rollback gate",
                "Define staging target, release gate, rollback trigger, and post-release evidence.",
                include_when=("Draft mentions deploy, release, staging, production, or rollback.",),
                entry_criteria=("Target environment and release intent are named.",),
                exit_criteria=("Release, validation, and rollback gates are explicit.",),
                required_evidence=("environment_notes", "release_gate", "rollback_plan"),
                approval_required=True,
                full_gawd_sources=(
                    "Rollout / Migration / Rollback",
                    "Verification",
                    "Observability",
                ),
            ),
        ),
    ),
)


def _draft_search_text(draft: Any) -> str:
    parts: list[str] = []
    for name in (
        "project",
        "goal",
        "theory",
        "why",
        "failure_that_matters",
        "unit_of_work",
    ):
        value = getattr(draft, name, "")
        if isinstance(value, str):
            parts.append(value)
    for name in (
        "golden_flow",
        "in_scope",
        "non_goals",
        "verification",
        "dependencies",
        "security_access",
        "backpressure_cost",
        "rollout_migration_rollback",
        "risk_synthesis",
        "known_limitations",
    ):
        value = getattr(draft, name, ())
        if isinstance(value, Iterable) and not isinstance(value, str):
            parts.extend(str(item) for item in value)
    return " ".join(parts).lower()


def _matched_keywords(text: str, keywords: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(keyword for keyword in keywords if _keyword_in_text(text, keyword))


def _keyword_in_text(text: str, keyword: str) -> bool:
    if re.fullmatch(r"[a-z0-9]+", keyword):
        return re.search(rf"\b{re.escape(keyword)}\b", text) is not None
    return keyword in text


def _has_saas_defining_overlay(overlays: tuple[ArchetypeOverlay, ...]) -> bool:
    return any(overlay.overlay_id != "deploy_overlay" for overlay in overlays)


def _dedupe_milestones(
    milestones: Iterable[ArchetypeMilestoneTemplate],
) -> tuple[ArchetypeMilestoneTemplate, ...]:
    seen: set[str] = set()
    deduped: list[ArchetypeMilestoneTemplate] = []
    for milestone in milestones:
        if milestone.milestone_id in seen:
            continue
        seen.add(milestone.milestone_id)
        deduped.append(milestone)
    return tuple(deduped)


def _blocked_questions(
    text: str,
    overlays: tuple[ArchetypeOverlay, ...],
) -> tuple[str, ...]:
    questions: list[str] = []
    if ("deploy" in text or "release" in text or "production" in text) and "staging" not in text:
        questions.append("Which staging environment should prove release readiness?")
    if (
        any(overlay.overlay_id == "commerce_billing_saas" for overlay in overlays)
        and "test mode" not in text
        and "sandbox" not in text
    ):
        questions.append("Should billing/payment work be restricted to provider test mode?")
    if any(overlay.overlay_id == "regulated_sensitive_data_saas" for overlay in overlays):
        questions.append("Which sensitive data classes are in scope and what retention applies?")
    return tuple(questions)
