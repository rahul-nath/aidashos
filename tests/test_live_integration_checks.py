# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from local_first_agent_os.live_integration_checks import (
    LiveIntegrationCheckRequest,
    LiveIntegrationOverallStatus,
    LiveIntegrationStep,
    LiveIntegrationStepStatus,
    build_live_integration_check_report,
)


def test_live_integration_checks_stub_is_fail_closed_without_permissions() -> None:
    report = build_live_integration_check_report({"target_project": "answers_bot"})

    assert report.target_project == "answers_bot"
    assert report.environment == "staging"
    assert report.overall_status == LiveIntegrationOverallStatus.BLOCKED
    assert {step.step for step in report.steps} == set(LiveIntegrationStep)

    blocked_steps = {
        step.step for step in report.steps if step.status == LiveIntegrationStepStatus.BLOCKED
    }
    assert blocked_steps == {
        LiveIntegrationStep.STAGING_DEPLOYMENT,
        LiveIntegrationStep.MIGRATION_SAFETY,
        LiveIntegrationStep.TEST_DATA_SEED,
        LiveIntegrationStep.ROLLBACK_OR_BLOCK,
    }


def test_live_integration_checks_stub_names_fill_in_work_when_authorized() -> None:
    request = LiveIntegrationCheckRequest(
        target_project="answers_bot",
        deployment_ref="abc123",
        staging_url="https://staging.example.test",
        allow_staging_deploy=True,
        allow_migrations=True,
        allow_test_data_seed=True,
        allow_rollback=True,
    )

    report = build_live_integration_check_report(request.model_dump(mode="json"))

    assert report.overall_status == LiveIntegrationOverallStatus.NOT_IMPLEMENTED
    assert {step.status for step in report.steps} == {LiveIntegrationStepStatus.NOT_IMPLEMENTED}
    deploy_step = next(
        step for step in report.steps if step.step == LiveIntegrationStep.STAGING_DEPLOYMENT
    )
    assert deploy_step.evidence["deployment_ref"] == "abc123"
    assert deploy_step.evidence["staging_url"] == "https://staging.example.test"
