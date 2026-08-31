# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from pathlib import Path

import pytest
from pydantic import ValidationError

from local_first_agent_os.browser_acceptance import (
    DEFAULT_RESPONSIVE_VIEWPORTS,
    BrowserAcceptanceRequest,
    BrowserViewport,
)
from local_first_agent_os.coordination import DispatchKind
from local_first_agent_os.decomposition import RuleBasedDecompositionPlanner
from local_first_agent_os.marketing_site_doctrine import CURRENT_MARKETING_SITE_DOCTRINE
from local_first_agent_os.pow_wow.prompts import build_agent_task_prompt
from local_first_agent_os.pow_wow.protocol import ReferencePack, TaskPurpose
from local_first_agent_os.pow_wow.types import PowWowExecutionContext, PowWowTaskSpec
from local_first_agent_os.project_access import AccessMode, ProjectAccessPolicy
from local_first_agent_os.project_center import LinkedProject, load_project_center
from local_first_agent_os.settings import Settings
from local_first_agent_os.staffing import JudgmentRole
from local_first_agent_os.vocabulary import DispatchTier


def _context() -> PowWowExecutionContext:
    return PowWowExecutionContext(
        saga_id="saga-site",
        goal="Build a source-grounded pest-control landing page",
        directive="/pow-wow",
        target_project_id="pest_site_factory",
        target_project_path="/tmp/pest",
        target_project_kind="client_site_factory",
        target_project_status="active_product_repo",
        target_project_read_only=False,
    )


@pytest.mark.parametrize("tier", [DispatchTier.SENIOR, DispatchTier.STAFF])
def test_marketing_site_doctrine_is_cross_harness_prompt_contract(tier: DispatchTier) -> None:
    task = PowWowTaskSpec(
        task_name=f"{tier.value}_site",
        role="reviewer" if tier is DispatchTier.STAFF else "implementer",
        description="Implement or review the site",
        judgment=JudgmentRole(name=tier.value, tier=tier),
        dispatch_kind=DispatchKind.CODE,
        reference_packs=(ReferencePack.MARKETING_SITE,),
    )

    prompt = build_agent_task_prompt(task, _context())

    assert CURRENT_MARKETING_SITE_DOCTRINE.render_prompt() in prompt
    if tier is DispatchTier.STAFF:
        assert "BLOCK unsupported business claims" in prompt


def test_marketing_site_doctrine_is_not_in_unrelated_task() -> None:
    task = PowWowTaskSpec(
        task_name="senior_backend",
        role="implementer",
        description="Change a backend contract",
        judgment=JudgmentRole(name="implementer", tier=DispatchTier.SENIOR),
        dispatch_kind=DispatchKind.CODE,
    )

    assert "Marketing-site doctrine contract" not in build_agent_task_prompt(task, _context())


def test_project_reference_pack_is_attached_to_every_planned_task() -> None:
    project = LinkedProject(
        id="pest_site_factory",
        kind="client_site_factory",
        path=Path("/tmp/pest"),
        status="active_product_repo",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="Pest factory",
        reference_packs=[ReferencePack.MARKETING_SITE],
    )

    plan = RuleBasedDecompositionPlanner().plan(
        intent_id="intent-site",
        tier=DispatchTier.SENIOR,
        kind=DispatchKind.CODE,
        prompt="Improve the generated homepage",
        target_project=project,
        intent={},
    )

    assert plan.tasks
    assert all(ReferencePack.MARKETING_SITE in task.reference_packs for task in plan.tasks)
    assert all(task.to_payload()["reference_packs"] == ["marketing_site"] for task in plan.tasks)
    implementation = next(task for task in plan.tasks if task.purpose is TaskPurpose.IMPLEMENTATION)
    browser = next(task for task in plan.tasks if task.purpose is TaskPurpose.BROWSER_ACCEPTANCE)
    review = next(task for task in plan.tasks if task.purpose is TaskPurpose.REVIEW)
    assert browser.blocked_by == (implementation.task_name,)
    assert browser.task_name in review.blocked_by
    assert implementation.task_name not in review.blocked_by


def test_browser_acceptance_request_requires_bounded_viewports_and_paths() -> None:
    request = BrowserAcceptanceRequest(
        target_url="http://127.0.0.1:3000/",
        viewports=DEFAULT_RESPONSIVE_VIEWPORTS,
        required_paths=("/", "/contact/"),
    )

    assert request.target_url == "http://127.0.0.1:3000"
    assert [viewport.name for viewport in request.viewports] == ["mobile", "desktop"]

    with pytest.raises(ValidationError, match="viewport names must be unique"):
        BrowserAcceptanceRequest(
            target_url="http://127.0.0.1:3000",
            viewports=(
                BrowserViewport(name="mobile", width=375, height=812),
                BrowserViewport(name="mobile", width=390, height=844),
            ),
        )

    with pytest.raises(ValidationError, match="must start with /"):
        BrowserAcceptanceRequest(
            target_url="http://127.0.0.1:3000",
            viewports=DEFAULT_RESPONSIVE_VIEWPORTS,
            required_paths=("contact",),
        )


def test_every_declared_browser_acceptance_profile_is_locally_bounded() -> None:
    # Which projects a machine registers is operator state, so naming one here would
    # assert somebody's configuration rather than this system's contract, and would
    # fail on every checkout but the author's. The contract is that a profile, once
    # declared, keeps browser acceptance on the loopback interface and bounded.
    center = load_project_center(Settings(config_dir=Path("configs")))
    profiles = [
        (project.id, project.browser_acceptance)
        for project in center.projects
        if project.browser_acceptance is not None
    ]

    assert profiles, "the shipped registry should declare at least one profile to exercise"
    for project_id, profile in profiles:
        assert profile is not None
        assert profile.required_paths, f"{project_id} declares no path to capture"
        assert profile.viewports, f"{project_id} declares no viewport to capture at"
        assert set(profile.allowed_hosts) <= {"127.0.0.1", "localhost"}, (
            f"{project_id} allows a non-loopback host"
        )
        assert profile.target_url_template.startswith("http://127.0.0.1"), (
            f"{project_id} points browser acceptance off the loopback interface"
        )
