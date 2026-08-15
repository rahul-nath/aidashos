# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The deep-think GAWD doc, proven compilable and driven to SUCCEEDED.

`docs/deep_think_buys_a_second_pass_gawd.md` is a candidate WorkUnit subject, so
the properties a run depends on are pinned here rather than discovered at
dispatch time: the document compiles runnable with no execution blockers, it
targets this repository by name, exactly one milestone gates on an operator, and
the whole plan drives to ``SUCCEEDED`` through the same engine the rest of the
suite trusts. A compile regression caught here costs a recompile; caught at
dispatch time it costs an agent hour and a milestone attempt.

The drive uses the simulated runtime deliberately. The real bench would dispatch
frontier agents at IMPLEMENT milestones whose ``source_patch`` evidence a mock
turn cannot honestly produce, which is the same reason the golden-path document
exists apart from the acceptance one.
"""

from __future__ import annotations

from pathlib import Path

from work_unit_support import install_simulated_engine, settle_operator_decisions, start_inline

from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units import service
from local_first_agent_os.work_units.lifecycle import (
    LifecyclePhase,
    MilestoneExecutionStatus,
    WorkUnitStatus,
)

DOC_PATH = Path(__file__).resolve().parents[1] / "docs" / "deep_think_buys_a_second_pass_gawd.md"
MILESTONE_KEYS = ("0", "1", "2", "3", "4", "5")
REVIEW_MILESTONE = "4"


def _compile(design_doc_id: str) -> service.CompileResult:
    result = service.compile_design_doc_text(
        DOC_PATH.read_text(encoding="utf-8"), design_doc_id=design_doc_id
    )
    assert result.runnable is True, result.diagnostics
    assert result.compiled_plan_revision_id is not None
    return result


def test_the_deep_think_document_compiles_to_a_runnable_plan(work_unit_ledger: Path) -> None:
    """VALID, runnable, and blocker-free, through the same service the CLI uses."""

    _compile("deep_think")


def test_the_deep_think_document_targets_this_repository(work_unit_ledger: Path) -> None:
    """The banner's project id must resolve to a registered project, not a blocker.

    ``_resolve_target_project`` turns an unregistered name into an execution
    blocker, so this pin is what keeps a rename of the project id from silently
    unrunning the document.
    """

    result = _compile("deep_think_target")
    assert result.compiled_plan_revision_id is not None
    plan = repo.get_compiled_plan_revision(result.compiled_plan_revision_id).plan

    assert plan.target_project_id == "local_first_agent_os"


def test_every_implement_milestone_demands_a_real_patch(work_unit_ledger: Path) -> None:
    """The inverse of the golden-path evidence rule, on purpose.

    This document exists to be executed by the real bench, so its IMPLEMENT
    milestones must demand ``source_patch``: an implementation milestone that a
    bounded advisory turn could satisfy would let a run succeed without the
    feature existing.
    """

    result = _compile("deep_think_evidence")
    assert result.compiled_plan_revision_id is not None
    plan = repo.get_compiled_plan_revision(result.compiled_plan_revision_id).plan

    implement = [
        milestone
        for milestone in plan.ordered_milestones()
        if milestone.phase is LifecyclePhase.IMPLEMENT
    ]
    assert {milestone.stable_key for milestone in implement} == {"1", "2"}
    for milestone in implement:
        assert "source_patch" in milestone.required_artifacts


def test_the_deep_think_document_gates_on_exactly_one_operator(work_unit_ledger: Path) -> None:
    """One review gate, at the review milestone, and nowhere else."""

    result = _compile("deep_think_gate")
    assert result.compiled_plan_revision_id is not None
    plan = repo.get_compiled_plan_revision(result.compiled_plan_revision_id).plan

    gated = [
        milestone.stable_key
        for milestone in plan.ordered_milestones()
        if milestone.approval_policy.required
    ]
    assert gated == [REVIEW_MILESTONE]


def test_the_document_names_the_feature_flag_it_is_gated_by() -> None:
    """The flag is the design's kill switch, so the document must name it exactly.

    Prose like "behind a flag" cannot be implemented without a decision the plan
    was supposed to have made; the field name and its environment spelling are
    the decision.
    """

    text = DOC_PATH.read_text(encoding="utf-8")
    assert "deep_think_second_pass" in text
    assert "LOCAL_AGENT_DEEP_THINK_SECOND_PASS" in text
    assert "feature_flag" in text


def test_a_deep_think_work_unit_runs_end_to_end(work_unit_ledger: Path) -> None:
    """Compile, start, approve the one gate, and reach SUCCEEDED on every milestone."""

    result = _compile("deep_think_run")
    assert result.compiled_plan_revision_id is not None
    install_simulated_engine()

    started = start_inline(result.compiled_plan_revision_id)
    work_unit_id = str(started["work_unit_id"])
    settle_operator_decisions(work_unit_id)

    unit = repo.get_work_unit(work_unit_id)
    assert unit.status is WorkUnitStatus.SUCCEEDED
    statuses = {
        milestone.stable_key: milestone.status
        for milestone in repo.list_milestone_executions(work_unit_id)
    }
    assert statuses == dict.fromkeys(MILESTONE_KEYS, MilestoneExecutionStatus.SUCCEEDED)
