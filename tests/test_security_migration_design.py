# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The security CGD must carry its authority contract into the compiled plan."""

import re
from pathlib import Path

import pytest
from work_unit_support import register_document_target

from local_first_agent_os.work_units.compiler import CompiledPlanOutcome, compile_design_doc
from local_first_agent_os.work_units.design_doc import parse_design_doc
from local_first_agent_os.work_units.lifecycle import LifecyclePhase

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/security_boundary_migration_gawd.md"
pytestmark = pytest.mark.skipif(
    not SOURCE.exists(), reason="security migration CGD is owned by the private repository"
)


@pytest.fixture
def compiled(tmp_path: Path) -> CompiledPlanOutcome:
    register_document_target(SOURCE.read_text(), tmp_path / "security-target")
    parsed = parse_design_doc(
        SOURCE.read_text(), design_doc_id=SOURCE.stem, source_path=str(SOURCE)
    )
    assert parsed.permission_envelope is not None
    outcome = compile_design_doc(parsed, design_doc_revision_id="security-design-test")
    assert isinstance(outcome, CompiledPlanOutcome), outcome.diagnostics
    assert outcome.validation_status == "VALID"
    assert outcome.runnable
    assert not outcome.execution_blockers
    return outcome


def test_security_graph_has_one_review_after_both_verification_gates(
    compiled: CompiledPlanOutcome,
) -> None:
    plan = compiled.plan
    assert [item.stable_key for item in plan.milestones] == list("abcdefghijklmnop")
    assert plan.milestone("a").phase == LifecyclePhase.PLAN
    assert plan.milestone("m").phase == LifecyclePhase.VERIFY
    assert plan.milestone("n").phase == LifecyclePhase.VERIFY
    assert plan.milestone("o").phase == LifecyclePhase.REVIEW
    assert plan.milestone("p").phase == LifecyclePhase.DELIVER
    assert plan.milestone("o").approval_policy.to_payload()["required"] is True


def test_security_policy_is_explicit_and_denies_external_authority(
    compiled: CompiledPlanOutcome,
) -> None:
    policy = compiled.plan.permission_policy
    assert policy is not None
    assert set(policy.denied_capabilities) == {
        "network_access",
        "access_credentials",
        "destructive_file_operations",
        "external_communications",
        "merge_to_main",
        "publish_deployment",
        "spend_money",
    }
    assert {"run_command", "write_repository"} <= set(policy.capability_ceiling)
    for milestone in compiled.plan.milestones:
        assert not set(milestone.tool_policy.permitted_tools) & set(policy.denied_capabilities)


def test_security_context_reaches_milestone_execution(compiled: CompiledPlanOutcome) -> None:
    context = compiled.plan.document_context
    assert {item.split(":", 1)[0] for item in context.requirements} == {
        f"R{index:02d}" for index in range(1, 14)
    }
    assert len(context.constraints) >= 10
    assert context.acceptance_criteria
    assert context.non_goals
    assert context.rollout
    assert "1200-second" in context.render()
    assert "stored permission ceiling" in context.render()


def test_security_reconciliation_and_local_links_are_complete() -> None:
    source = SOURCE.read_text()
    assert set(re.findall(r"^\| (C\d\d):", source, re.MULTILINE)) == {
        f"C{index:02d}" for index in range(1, 16)
    }
    assert set(re.findall(r"^\| (S\d\d) \|", source, re.MULTILINE)) == {
        f"S{index:02d}" for index in range(1, 19)
    }
    for target in re.findall(r"\]\(([^)]+)\)", source):
        if not target.startswith(("https://", "#")):
            assert (SOURCE.parent / target.split("#", 1)[0]).is_file(), target
    for stem in ("claims_become_enforcement_gawd", "privileged_capability_broker_design"):
        assert not (ROOT / "docs" / f"{stem}.md").exists()
        reference = ROOT / "docs/designs" / f"{stem}.md"
        assert "supersedes this document's execution milestones" in reference.read_text()
    assert (ROOT / "docs/ndf_semantic_provenance_and_sink_enforcement_gawd.md").is_file()


def test_ndf_compiled_constraints_preserve_the_shared_authority_port(tmp_path: Path) -> None:
    source = ROOT / "docs/ndf_semantic_provenance_and_sink_enforcement_gawd.md"
    register_document_target(source.read_text(), tmp_path / "ndf-target")
    parsed = parse_design_doc(source.read_text(), design_doc_id=source.stem)
    outcome = compile_design_doc(parsed, design_doc_revision_id="ndf-port-test")
    assert isinstance(outcome, CompiledPlanOutcome)
    assert outcome.runnable
    assert "security_boundary_migration_gawd" in outcome.plan.document_context.render()
    assert [item.stable_key for item in outcome.plan.milestones] == list("abcdefghijklmno")
