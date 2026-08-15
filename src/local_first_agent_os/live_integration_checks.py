# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""DBOS workflow skeleton for end-to-end live integration checks.

The workflow is intentionally fail-closed. It names each durable boundary now,
but does not deploy, migrate, seed, rollback, or touch external systems until a
future implementation wires those steps behind explicit permissions.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from ._dbos_runtime import DBOS, SetWorkflowID, dbos_step, dbos_workflow
from .ids import sha256_text


class LiveIntegrationStep(StrEnum):
    STAGING_DEPLOYMENT = "staging_deployment"
    MIGRATION_SAFETY = "migration_safety"
    TEST_DATA_SEED = "test_data_seed"
    HEALTH_CHECKS = "health_checks"
    API_CHECKS = "api_checks"
    BROWSER_CHECKS = "browser_checks"
    LOG_AND_METRIC_CHECKS = "log_and_metric_checks"
    LEDGER_EVIDENCE = "ledger_evidence"
    ROLLBACK_OR_BLOCK = "rollback_or_block"


class LiveIntegrationStepStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


class LiveIntegrationOverallStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"


class LiveIntegrationCheckRequest(BaseModel):
    target_project: str = Field(min_length=1)
    environment: str = Field(default="staging", min_length=1)
    deployment_ref: str | None = None
    staging_url: str | None = None
    allow_staging_deploy: bool = False
    allow_migrations: bool = False
    allow_test_data_seed: bool = False
    allow_rollback: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class LiveIntegrationStepResult(BaseModel):
    step: LiveIntegrationStep
    status: LiveIntegrationStepStatus
    summary: str
    implementation_notes: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


class LiveIntegrationCheckReport(BaseModel):
    target_project: str
    environment: str
    overall_status: LiveIntegrationOverallStatus
    steps: list[LiveIntegrationStepResult]


def _request(payload: dict[str, Any]) -> LiveIntegrationCheckRequest:
    return LiveIntegrationCheckRequest.model_validate(payload)


def _result(
    *,
    step: LiveIntegrationStep,
    status: LiveIntegrationStepStatus,
    summary: str,
    implementation_notes: list[str],
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return LiveIntegrationStepResult(
        step=step,
        status=status,
        summary=summary,
        implementation_notes=implementation_notes,
        evidence=evidence or {},
    ).model_dump(mode="json")


def _blocked_permission(step: LiveIntegrationStep, permission: str) -> dict[str, Any]:
    return _result(
        step=step,
        status=LiveIntegrationStepStatus.BLOCKED,
        summary=f"{step.value} is blocked until `{permission}` is explicitly true.",
        implementation_notes=[
            "Keep this step fail-closed because it can mutate external state.",
            f"Implement the step only after the caller supplies `{permission}=true`.",
        ],
    )


def _not_implemented(
    step: LiveIntegrationStep,
    summary: str,
    implementation_notes: list[str],
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _result(
        step=step,
        status=LiveIntegrationStepStatus.NOT_IMPLEMENTED,
        summary=summary,
        implementation_notes=implementation_notes,
        evidence=evidence,
    )


@dbos_step()
def create_or_verify_staging_deployment(payload: dict[str, Any]) -> dict[str, Any]:
    return _create_or_verify_staging_deployment(_request(payload))


def _create_or_verify_staging_deployment(
    request: LiveIntegrationCheckRequest,
) -> dict[str, Any]:
    if not request.allow_staging_deploy:
        return _blocked_permission(
            LiveIntegrationStep.STAGING_DEPLOYMENT,
            "allow_staging_deploy",
        )
    return _not_implemented(
        LiveIntegrationStep.STAGING_DEPLOYMENT,
        "Create or locate a staging deployment for the requested ref.",
        [
            "Resolve `target_project` through `linked_projects.toml` or the approved GAWD target.",
            "Create an isolated staging deployment for the requested ref.",
            "Return the deployment URL, commit SHA, environment name, and deploy command output.",
        ],
        {"deployment_ref": request.deployment_ref, "staging_url": request.staging_url},
    )


@dbos_step()
def run_migration_safety_check(payload: dict[str, Any]) -> dict[str, Any]:
    return _run_migration_safety_check(_request(payload))


def _run_migration_safety_check(request: LiveIntegrationCheckRequest) -> dict[str, Any]:
    if not request.allow_migrations:
        return _blocked_permission(
            LiveIntegrationStep.MIGRATION_SAFETY,
            "allow_migrations",
        )
    return _not_implemented(
        LiveIntegrationStep.MIGRATION_SAFETY,
        "Run staging migrations with rollback-aware evidence.",
        [
            "Generate or capture the migration plan before applying it.",
            "Run migrations only against staging and require idempotent re-entry.",
            "Record schema diff, migration logs, backup handles, and destructive-change flags.",
        ],
    )


@dbos_step()
def seed_staging_test_data(payload: dict[str, Any]) -> dict[str, Any]:
    return _seed_staging_test_data(_request(payload))


def _seed_staging_test_data(request: LiveIntegrationCheckRequest) -> dict[str, Any]:
    if not request.allow_test_data_seed:
        return _blocked_permission(
            LiveIntegrationStep.TEST_DATA_SEED,
            "allow_test_data_seed",
        )
    return _not_implemented(
        LiveIntegrationStep.TEST_DATA_SEED,
        "Seed deterministic staging data for browser and API checks.",
        [
            "Use idempotent seed keys so DBOS retry never duplicates rows.",
            "Keep credentials, tokens, and PII out of artifacts.",
            "Return fixture IDs and cleanup handles.",
        ],
    )


@dbos_step()
def run_staging_health_checks(payload: dict[str, Any]) -> dict[str, Any]:
    return _run_staging_health_checks(_request(payload))


def _run_staging_health_checks(request: LiveIntegrationCheckRequest) -> dict[str, Any]:
    return _not_implemented(
        LiveIntegrationStep.HEALTH_CHECKS,
        "Run liveness, readiness, and dependency health checks against staging.",
        [
            "Check health endpoints, workers, queue drain status, and database reachability.",
            "Capture minimal failing snippets, response codes, and relevant request IDs.",
            "Fail if the staging URL is missing or mismatches the approved environment.",
        ],
        {"staging_url": request.staging_url},
    )


@dbos_step()
def run_staging_api_checks(payload: dict[str, Any]) -> dict[str, Any]:
    return _run_staging_api_checks(_request(payload))


def _run_staging_api_checks(request: LiveIntegrationCheckRequest) -> dict[str, Any]:
    return _not_implemented(
        LiveIntegrationStep.API_CHECKS,
        "Run API contract and critical-path checks against staging.",
        [
            "Exercise auth and anonymous critical paths with scoped test credentials.",
            "Validate response schema, persistence side effects, idempotency, and error handling.",
            "Store only redacted request/response evidence.",
        ],
        {"staging_url": request.staging_url},
    )


@dbos_step()
def run_staging_browser_checks(payload: dict[str, Any]) -> dict[str, Any]:
    return _run_staging_browser_checks(_request(payload))


def _run_staging_browser_checks(request: LiveIntegrationCheckRequest) -> dict[str, Any]:
    return _not_implemented(
        LiveIntegrationStep.BROWSER_CHECKS,
        "Run Playwright or equivalent browser checks against staging.",
        [
            "Cover the golden flow from the approved GAWD doc or task acceptance criteria.",
            "Capture screenshots, traces, and console/network failures as artifacts.",
            "Check responsive breakpoints when the feature has user-facing UI.",
        ],
        {"staging_url": request.staging_url},
    )


@dbos_step()
def collect_log_and_metric_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    return _collect_log_and_metric_evidence(_request(payload))


def _collect_log_and_metric_evidence(request: LiveIntegrationCheckRequest) -> dict[str, Any]:
    return _not_implemented(
        LiveIntegrationStep.LOG_AND_METRIC_CHECKS,
        "Collect logs, errors, and basic metrics for the staging run.",
        [
            "Query application logs, worker logs, error tracking, and relevant metrics windows.",
            "Summarize new errors, latency regressions, retry storms, and queue backlog.",
            "Attach links or artifact IDs rather than dumping full logs.",
        ],
        {"environment": request.environment},
    )


@dbos_step()
def write_live_check_evidence_to_ledger(payload: dict[str, Any]) -> dict[str, Any]:
    return _write_live_check_evidence_to_ledger(_request(payload))


def _write_live_check_evidence_to_ledger(request: LiveIntegrationCheckRequest) -> dict[str, Any]:
    return _not_implemented(
        LiveIntegrationStep.LEDGER_EVIDENCE,
        "Write the live-check report and evidence handles to the coordination ledger.",
        [
            "Attach this report to the saga, pow-wow, task, or approval request that triggered it.",
            "Persist artifact IDs for deploy logs, migration output, traces, and screenshots.",
            "Do not rely on chat transcript as the authoritative evidence store.",
        ],
        {"target_project": request.target_project},
    )


@dbos_step()
def rollback_or_block_on_failure(payload: dict[str, Any]) -> dict[str, Any]:
    return _rollback_or_block_on_failure(_request(payload))


def _rollback_or_block_on_failure(request: LiveIntegrationCheckRequest) -> dict[str, Any]:
    if not request.allow_rollback:
        return _blocked_permission(
            LiveIntegrationStep.ROLLBACK_OR_BLOCK,
            "allow_rollback",
        )
    return _not_implemented(
        LiveIntegrationStep.ROLLBACK_OR_BLOCK,
        "Rollback staging changes or emit a blocking approval gate on failure.",
        [
            "Rollback only within the approved staging environment.",
            "If rollback cannot be proven safe, create a blocking approval request instead.",
            "Record rollback commands, final health status, and remaining operator action.",
        ],
    )


def _overall_status(
    steps: list[LiveIntegrationStepResult],
) -> LiveIntegrationOverallStatus:
    statuses = {step.status for step in steps}
    if LiveIntegrationStepStatus.FAILED in statuses:
        return LiveIntegrationOverallStatus.FAILED
    if LiveIntegrationStepStatus.BLOCKED in statuses:
        return LiveIntegrationOverallStatus.BLOCKED
    if LiveIntegrationStepStatus.NOT_IMPLEMENTED in statuses:
        return LiveIntegrationOverallStatus.NOT_IMPLEMENTED
    return LiveIntegrationOverallStatus.PASSED


PureStep = Callable[[LiveIntegrationCheckRequest], dict[str, Any]]
DbosStep = Callable[[dict[str, Any]], dict[str, Any]]

_PURE_STEPS: tuple[PureStep, ...] = (
    _create_or_verify_staging_deployment,
    _run_migration_safety_check,
    _seed_staging_test_data,
    _run_staging_health_checks,
    _run_staging_api_checks,
    _run_staging_browser_checks,
    _collect_log_and_metric_evidence,
    _write_live_check_evidence_to_ledger,
    _rollback_or_block_on_failure,
)

_DBOS_STEPS: tuple[DbosStep, ...] = (
    create_or_verify_staging_deployment,
    run_migration_safety_check,
    seed_staging_test_data,
    run_staging_health_checks,
    run_staging_api_checks,
    run_staging_browser_checks,
    collect_log_and_metric_evidence,
    write_live_check_evidence_to_ledger,
    rollback_or_block_on_failure,
)


def _build_live_integration_check_report(
    payload: dict[str, Any],
    *,
    use_dbos_steps: bool,
) -> LiveIntegrationCheckReport:
    request = _request(payload)
    if use_dbos_steps:
        raw_steps = [step(payload) for step in _DBOS_STEPS]
    else:
        raw_steps = [step(request) for step in _PURE_STEPS]
    steps = [LiveIntegrationStepResult.model_validate(step) for step in raw_steps]
    return LiveIntegrationCheckReport(
        target_project=request.target_project,
        environment=request.environment,
        overall_status=_overall_status(steps),
        steps=steps,
    )


def build_live_integration_check_report(payload: dict[str, Any]) -> LiveIntegrationCheckReport:
    return _build_live_integration_check_report(payload, use_dbos_steps=False)


@dbos_workflow()
def live_integration_checks_workflow(payload: dict[str, Any]) -> dict[str, Any]:
    return _build_live_integration_check_report(
        payload,
        use_dbos_steps=True,
    ).model_dump(mode="json")


def run_live_integration_checks(
    request: LiveIntegrationCheckRequest,
) -> LiveIntegrationCheckReport:
    payload = request.model_dump(mode="json")
    if DBOS is not None and SetWorkflowID is not None:
        from .dbos_app import is_dbos_active, launch_dbos
        from .settings import get_settings

        settings = get_settings()
        if settings.use_dbos:
            launch_dbos()
            if is_dbos_active():
                workflow_id = f"live_integration_checks:{sha256_text(request.model_dump_json())}"
                with SetWorkflowID(workflow_id):
                    return LiveIntegrationCheckReport.model_validate(
                        live_integration_checks_workflow(payload)
                    )
    return build_live_integration_check_report(payload)


__all__ = [
    "LiveIntegrationCheckReport",
    "LiveIntegrationCheckRequest",
    "LiveIntegrationOverallStatus",
    "LiveIntegrationStep",
    "LiveIntegrationStepResult",
    "LiveIntegrationStepStatus",
    "build_live_integration_check_report",
    "live_integration_checks_workflow",
    "run_live_integration_checks",
]
