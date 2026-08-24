# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Compilation is where document authority stops.

These tests pin the refusals as tightly as the successes. A plan that compiles is
a plan the runtime has agreed to execute, so every rule the compiler declines to
enforce becomes a rule nothing enforces.
"""

from __future__ import annotations

from work_unit_support import ACCEPTANCE_DESIGN_DOC

from local_first_agent_os.work_units.compiler import (
    CompilationRejected,
    CompiledPlanOutcome,
    ValidationStatus,
    compile_design_doc,
)
from local_first_agent_os.work_units.design_doc import (
    PhaseInference,
    apply_phase_inference,
    parse_design_doc,
)
from local_first_agent_os.work_units.executors import ExecutorKind
from local_first_agent_os.work_units.lifecycle import LifecyclePhase
from local_first_agent_os.work_units.plan import (
    CompiledWorkPlan,
    PlanIntegrityError,
    RequiredDelivery,
)


def _compile(document: str) -> CompiledPlanOutcome | CompilationRejected:
    parsed = parse_design_doc(document, design_doc_id="doc")
    return compile_design_doc(parsed, design_doc_revision_id="ddr_test")


def _codes(outcome: CompilationRejected) -> set[str]:
    return {item.code for item in outcome.errors}


def test_every_milestone_lands_in_exactly_one_phase() -> None:
    outcome = _compile(ACCEPTANCE_DESIGN_DOC)
    assert isinstance(outcome, CompiledPlanOutcome)

    phases = {item.stable_key: item.phase for item in outcome.plan.milestones}
    assert phases == {
        "a": LifecyclePhase.PLAN,
        "b": LifecyclePhase.IMPLEMENT,
        "c": LifecyclePhase.IMPLEMENT,
        "d": LifecyclePhase.VERIFY,
        "e": LifecyclePhase.REVIEW,
        "f": LifecyclePhase.DELIVER,
    }
    # One membership per milestone: the per-phase lists partition the plan.
    listed = [
        item.stable_key
        for phase in outcome.plan.lifecycle.ordered_phases
        for item in outcome.plan.milestones_in_phase(phase)
    ]
    assert sorted(listed) == sorted(phases)


def test_a_milestone_with_no_phase_is_rejected_rather_than_placed() -> None:
    document = ACCEPTANCE_DESIGN_DOC.replace("Phase: VERIFY\n", "")

    outcome = _compile(document)

    assert isinstance(outcome, CompilationRejected)
    assert "missing_phase" in _codes(outcome)


def test_same_phase_dependencies_are_accepted() -> None:
    document = ACCEPTANCE_DESIGN_DOC.replace(
        "Phase: IMPLEMENT\nDepends on: A\nAcceptance: the writer lands",
        "Phase: IMPLEMENT\nDepends on: A, B\nAcceptance: the writer lands",
    )

    outcome = _compile(document)

    assert isinstance(outcome, CompiledPlanOutcome)
    assert outcome.plan.milestone("c").dependencies == ("a", "b")


def test_a_dependency_on_a_later_phase_is_rejected() -> None:
    document = ACCEPTANCE_DESIGN_DOC.replace(
        "Phase: PLAN\nAcceptance: a written implementation plan exists",
        "Phase: PLAN\nDepends on: B\nAcceptance: a written implementation plan exists",
    )

    outcome = _compile(document)

    assert isinstance(outcome, CompilationRejected)
    assert "future_phase_dependency" in _codes(outcome)


def test_a_dependency_cycle_is_rejected_with_the_cycle_named() -> None:
    document = ACCEPTANCE_DESIGN_DOC.replace(
        "Phase: IMPLEMENT\nDepends on: A\nAcceptance: the reader lands",
        "Phase: IMPLEMENT\nDepends on: A, C\nAcceptance: the reader lands",
    ).replace(
        "Phase: IMPLEMENT\nDepends on: A\nAcceptance: the writer lands",
        "Phase: IMPLEMENT\nDepends on: A, B\nAcceptance: the writer lands",
    )

    outcome = _compile(document)

    assert isinstance(outcome, CompilationRejected)
    cycle = next(item for item in outcome.errors if item.code == "dependency_cycle")
    assert "->" in cycle.message


def test_self_dependency_is_rejected() -> None:
    document = ACCEPTANCE_DESIGN_DOC.replace(
        "Phase: IMPLEMENT\nDepends on: A\nAcceptance: the reader lands",
        "Phase: IMPLEMENT\nDepends on: A, B\nAcceptance: the reader lands",
    )

    outcome = _compile(document)

    assert isinstance(outcome, CompilationRejected)
    assert "self_dependency" in _codes(outcome)


def test_a_milestone_without_acceptance_criteria_cannot_compile() -> None:
    document = ACCEPTANCE_DESIGN_DOC.replace("Acceptance: the suite passes\n", "")

    outcome = _compile(document)

    assert isinstance(outcome, CompilationRejected)
    assert "missing_acceptance_criteria" in _codes(outcome)


def test_required_artifacts_are_injected_from_the_executor_registry() -> None:
    document = ACCEPTANCE_DESIGN_DOC.replace("Artifacts: test_result\n", "")

    outcome = _compile(document)

    assert isinstance(outcome, CompiledPlanOutcome)
    # The document stopped asking for evidence; the registry still requires it.
    assert outcome.plan.milestone("d").required_artifacts == ("test_result",)


def test_implementation_without_a_plan_milestone_is_rejected() -> None:
    document = """# No plan

## Milestone B: implement something

Phase: IMPLEMENT
Acceptance: it works
Artifacts: source_patch

## Milestone D: verify it

Phase: VERIFY
Depends on: B
Acceptance: the suite passes
Artifacts: test_result
"""

    outcome = _compile(document)

    assert isinstance(outcome, CompilationRejected)
    assert "implement_without_plan" in _codes(outcome)


def test_implementation_that_does_not_follow_the_plan_is_rejected() -> None:
    document = ACCEPTANCE_DESIGN_DOC.replace(
        "Phase: IMPLEMENT\nDepends on: A\nAcceptance: the reader lands",
        "Phase: IMPLEMENT\nAcceptance: the reader lands",
    )

    outcome = _compile(document)

    assert isinstance(outcome, CompilationRejected)
    assert "implement_without_plan_prerequisite" in _codes(outcome)


def test_implementation_with_no_verification_is_rejected() -> None:
    document = """# No verification

## Milestone A: plan it

Phase: PLAN
Acceptance: a plan exists
Artifacts: implementation_plan

## Milestone B: implement it

Phase: IMPLEMENT
Depends on: A
Acceptance: it works
Artifacts: source_patch
"""

    outcome = _compile(document)

    assert isinstance(outcome, CompilationRejected)
    assert "missing_verification" in _codes(outcome)


def test_delivery_that_does_not_depend_on_verification_is_rejected() -> None:
    document = ACCEPTANCE_DESIGN_DOC.replace(
        "Phase: DELIVER\nDepends on: E\nAcceptance: the delivery record exists",
        "Phase: DELIVER\nAcceptance: the delivery record exists",
    )

    outcome = _compile(document)

    assert isinstance(outcome, CompilationRejected)
    assert "deliver_without_verification" in _codes(outcome)


def test_an_unregistered_executor_is_rejected() -> None:
    document = ACCEPTANCE_DESIGN_DOC.replace(
        "Executor: review.operator", "Executor: review.shell_script"
    )

    outcome = _compile(document)

    assert isinstance(outcome, CompilationRejected)
    assert "unregistered_executor" in _codes(outcome)


def test_an_executor_declared_for_another_phase_is_rejected() -> None:
    document = ACCEPTANCE_DESIGN_DOC.replace(
        "Executor: review.operator", "Executor: implement.code_change"
    )

    outcome = _compile(document)

    assert isinstance(outcome, CompilationRejected)
    assert "executor_phase_mismatch" in _codes(outcome)


def test_an_approval_request_on_a_gateless_executor_is_rejected() -> None:
    document = ACCEPTANCE_DESIGN_DOC.replace(
        "Phase: VERIFY\nDepends on: B, C",
        "Phase: VERIFY\nApproval: required\nDepends on: B, C",
    )

    outcome = _compile(document)

    assert isinstance(outcome, CompilationRejected)
    assert "approval_not_available" in _codes(outcome)


def test_an_always_approval_executor_compiles_the_gate_on() -> None:
    outcome = _compile(ACCEPTANCE_DESIGN_DOC)

    assert isinstance(outcome, CompiledPlanOutcome)
    review = outcome.plan.milestone("e")
    assert review.executor_kind is ExecutorKind.REVIEW_OPERATOR
    assert review.approval_policy.required is True
    assert outcome.plan.authority_policy.operator_approval_inferrable is False


def test_deterministic_input_produces_byte_identical_canonical_output() -> None:
    first = _compile(ACCEPTANCE_DESIGN_DOC)
    second = _compile(ACCEPTANCE_DESIGN_DOC)
    assert isinstance(first, CompiledPlanOutcome)
    assert isinstance(second, CompiledPlanOutcome)

    assert first.plan.canonical_json() == second.plan.canonical_json()
    assert first.plan.plan_hash() == second.plan.plan_hash()


def test_changing_authority_bearing_content_changes_the_hash() -> None:
    baseline = _compile(ACCEPTANCE_DESIGN_DOC)
    changed = _compile(
        ACCEPTANCE_DESIGN_DOC.replace("Acceptance: the suite passes", "Acceptance: anything at all")
    )
    assert isinstance(baseline, CompiledPlanOutcome)
    assert isinstance(changed, CompiledPlanOutcome)

    assert baseline.plan.plan_hash() != changed.plan.plan_hash()


def test_permission_envelope_is_hashed_and_narrows_every_milestone() -> None:
    document = (
        ACCEPTANCE_DESIGN_DOC
        + """

## Permission Envelope

Autonomous permissions:
- read_repo_context
- write_ledger_artifacts

Requested permissions:
- test_command_execution: operator approves test commands at start

Denied without explicit approval:
- code_worktree_write
- run_local_model_delegates
"""
    )

    outcome = _compile(document)

    assert isinstance(outcome, CompiledPlanOutcome)
    assert outcome.plan.permission_policy is not None
    assert outcome.plan.permission_policy.capability_ceiling == (
        "read_repository",
        "run_command",
        "write_artifact",
    )
    assert outcome.plan.milestone("b").tool_policy.permitted_tools == (
        "read_repository",
        "run_command",
    )
    assert outcome.plan.milestone("f").tool_policy.permitted_tools == (
        "read_repository",
        "write_artifact",
    )
    assert outcome.plan.plan_hash() != _compile(ACCEPTANCE_DESIGN_DOC).plan.plan_hash()  # type: ignore[union-attr]
    # Narrowing write away from an implement milestone is representable, and it
    # is also an execution blocker: milestone "c" is implement.code_change and
    # can never produce a diff under this envelope. The plan compiles, hashes,
    # and refuses to run, which are three different facts.
    assert any(
        "write_repository" in blocker and "'c'" in blocker for blocker in outcome.execution_blockers
    )


def test_a_prose_only_envelope_blocks_execution_rather_than_compiling_an_actionless_plan() -> None:
    """The 2026-08-10 incident, caught where it belongs.

    A permission envelope written as prose prohibitions maps to zero
    capabilities, and the ceiling then strips write and run from every
    milestone. That plan used to compile clean, spawn a read-only implementer,
    and surface as an empty diff blocked by staff review two dispatches later.
    The compiler is the first reader that can see the milestone can never act,
    so the compiler is where it becomes an execution blocker, named well enough
    that the fix is a copy-paste into the envelope.
    """

    document = (
        ACCEPTANCE_DESIGN_DOC
        + """

## Permission Envelope

No milestone may deploy, use a credential, contact anyone, or write outside this repository.
Merge requires an approved operator review of the exact commit.
"""
    )

    outcome = _compile(document)

    assert isinstance(outcome, CompiledPlanOutcome)
    implement_blockers = [
        blocker for blocker in outcome.execution_blockers if "implement.code_change" in blocker
    ]
    assert implement_blockers, "an actionless implement milestone must block execution"
    assert any("write_repository" in blocker for blocker in implement_blockers)
    assert any("run_command" in blocker for blocker in implement_blockers)
    # The message must carry its own fix: the envelope vocabulary, not just the
    # missing capability's internal name.
    assert any("code_worktree_write" in blocker for blocker in implement_blockers)
    assert any("test_command_execution" in blocker for blocker in implement_blockers)
    assert outcome.validation_status is ValidationStatus.BLOCKED
    assert not outcome.runnable


def test_an_envelope_that_grants_the_act_capabilities_compiles_runnable() -> None:
    """The correctly declared counterpart: same milestones, no blockers."""

    document = (
        ACCEPTANCE_DESIGN_DOC
        + """

## Permission Envelope

Autonomous permissions:
- read_repo_context
- code_worktree_write
- test_command_execution
- write_ledger_artifacts
- run_local_model_delegates
- request_operator_decisions

Denied without explicit approval:
- deploy
- external_communications
- merge_to_main
"""
    )

    outcome = _compile(document)

    assert isinstance(outcome, CompiledPlanOutcome)
    assert outcome.execution_blockers == ()
    assert outcome.runnable
    implement = outcome.plan.milestone("c")
    assert "write_repository" in implement.tool_policy.permitted_tools
    assert "run_command" in implement.tool_policy.permitted_tools


def test_implementation_without_a_terminal_delivery_contract_is_rejected() -> None:
    document = ACCEPTANCE_DESIGN_DOC.replace(
        "## Milestone F: deliver the artifact",
        "## Removed delivery",
    )

    outcome = _compile(document)

    assert isinstance(outcome, CompilationRejected)
    assert "missing_delivery_contract" in _codes(outcome)


def test_delivery_contract_is_a_non_empty_sum_type() -> None:
    outcome = _compile(ACCEPTANCE_DESIGN_DOC)

    assert isinstance(outcome, CompiledPlanOutcome)
    assert isinstance(outcome.plan.delivery_contract, RequiredDelivery)
    assert outcome.plan.delivery_contract.artifact_types == ("delivery_record",)


def test_a_plan_round_trips_through_its_canonical_payload() -> None:
    outcome = _compile(ACCEPTANCE_DESIGN_DOC)
    assert isinstance(outcome, CompiledPlanOutcome)

    rebuilt = CompiledWorkPlan.from_payload(outcome.plan.to_payload())

    assert rebuilt.canonical_json() == outcome.plan.canonical_json()


def test_a_tampered_payload_fails_closed_on_its_declared_hash() -> None:
    outcome = _compile(ACCEPTANCE_DESIGN_DOC)
    assert isinstance(outcome, CompiledPlanOutcome)
    payload = outcome.plan.to_payload()
    payload["milestones"][0]["acceptance_criteria"] = ["whatever an agent decides"]

    try:
        CompiledWorkPlan.from_payload(payload)
    except PlanIntegrityError as exc:
        assert exc.expected != exc.actual
    else:  # pragma: no cover - the raise is the behavior under test
        raise AssertionError("a tampered plan payload must not load")


def test_an_unresolved_blocking_question_prevents_execution() -> None:
    document = ACCEPTANCE_DESIGN_DOC + (
        "\n## Unresolved questions\n\n- BLOCKING: which database owns the ledger?\n"
    )

    outcome = _compile(document)

    assert isinstance(outcome, CompiledPlanOutcome)
    assert outcome.validation_status is ValidationStatus.BLOCKED
    assert outcome.runnable is False
    assert any("which database" in item for item in outcome.execution_blockers)


def test_a_non_blocking_question_does_not_prevent_execution() -> None:
    document = ACCEPTANCE_DESIGN_DOC + (
        "\n## Unresolved questions\n\n- Should the cockpit show token counts?\n"
    )

    outcome = _compile(document)

    assert isinstance(outcome, CompiledPlanOutcome)
    assert outcome.validation_status is ValidationStatus.VALID


def test_an_unconfirmed_inferred_phase_blocks_execution() -> None:
    document = """# Legacy doc

## Milestone A: think about it

Acceptance: a plan exists
Artifacts: implementation_plan

## Milestone D: check it

Phase: VERIFY
Depends on: A
Acceptance: the suite passes
Artifacts: test_result
"""
    parsed = apply_phase_inference(
        parse_design_doc(document, design_doc_id="legacy"),
        [
            PhaseInference(
                milestone_key="a",
                phase=LifecyclePhase.PLAN,
                confidence=0.6,
                reasoning="the title names deliberation",
            )
        ],
    )

    outcome = compile_design_doc(parsed, design_doc_revision_id="ddr_legacy")

    assert isinstance(outcome, CompiledPlanOutcome)
    assert outcome.runnable is False
    assert outcome.plan.milestone("a").phase_inferred is True


def test_source_provenance_points_back_at_the_document() -> None:
    outcome = _compile(ACCEPTANCE_DESIGN_DOC)
    assert isinstance(outcome, CompiledPlanOutcome)

    provenance = outcome.plan.milestone("b").source_provenance
    assert provenance.design_doc_revision_id == "ddr_test"
    excerpt = ACCEPTANCE_DESIGN_DOC[provenance.source_start : provenance.source_end]
    assert "implement the reader" in excerpt


def test_an_acceptance_criterion_keeps_its_commas() -> None:
    """A criterion is prose, and prose has commas in it.

    Splitting it the way a dependency list is split turned one sentence into
    three fragments, and an agent was then asked to satisfy "and the capability"
    as a criterion in its own right.
    """

    document = """# Commas

## Milestone A: plan it

Phase: PLAN
Acceptance: a plan names the caller, the call site, and the capability
Artifacts: implementation_plan
"""

    parsed = parse_design_doc(document, design_doc_id="doc")

    assert parsed.milestone_candidates[0].acceptance_criteria == (
        "a plan names the caller, the call site, and the capability",
    )


def test_repeating_the_acceptance_field_adds_a_criterion() -> None:
    """The way to write several is to write several, since one line is one."""

    document = """# Repeats

## Milestone A: plan it

Phase: PLAN
Acceptance: the plan names each call site
Acceptance: the plan says which surfaces stay ungated
Artifacts: implementation_plan
"""

    parsed = parse_design_doc(document, design_doc_id="doc")

    assert parsed.milestone_candidates[0].acceptance_criteria == (
        "the plan names each call site",
        "the plan says which surfaces stay ungated",
    )


def test_a_dependency_list_still_splits_on_commas() -> None:
    """Because that one really is a list of tokens rather than a sentence."""

    document = """# Lists

## Milestone A: plan it

Phase: PLAN
Acceptance: a plan exists
Artifacts: implementation_plan

## Milestone B: build it

Phase: IMPLEMENT
Depends on: A
Acceptance: it builds
Artifacts: source_patch

## Milestone C: build more

Phase: IMPLEMENT
Depends on: A, B
Acceptance: it still builds
Artifacts: source_patch, test_result
"""

    parsed = parse_design_doc(document, design_doc_id="doc")
    third = parsed.milestone_candidates[2]

    assert third.dependencies == ("a", "b")
    assert third.required_artifacts == ("source_patch", "test_result")


def test_motivation_reaches_the_plan_and_the_prompt_it_renders() -> None:
    """The document context is the only part of a plan the executing agent reads.

    A collection that stops at the parsed document is dropped at compile, which
    is the failure this context exists to prevent.
    """

    pain = "Operators re-derive plan state by hand today."
    outcome = _compile(ACCEPTANCE_DESIGN_DOC + f"\n## 2. Why This Exists\n\n- {pain}\n")

    assert isinstance(outcome, CompiledPlanOutcome)
    context = outcome.plan.document_context
    assert context.motivation == (pain,)

    rendered = context.render()
    assert "Why this work exists:" in rendered
    assert pain in rendered

    # Motivation explains the work; it never becomes something the work must ship.
    assert pain not in outcome.plan.required_final_artifacts


def test_a_document_with_no_declared_target_cannot_be_started() -> None:
    """Silence used to resolve to the project-center default with zero diagnostics.

    That cost a real dispatch. Five design documents in `docs/` had no
    `Target project:` line, every one compiled VALID and runnable against an
    unrelated repository, and starting one sent a frontier agent to read that
    repository for context while implementing in a worktree of this one. The
    failure surfaced four layers later as a complaint about missing staff-review
    evidence, which is nowhere near the cause.

    It blocks rather than inferring the target from the document's own location.
    The compiler is pure and offline so one document compiles to one plan hash on
    every host, and a default read from the filesystem would make the same text
    produce different plans in different checkouts.
    """

    document = ACCEPTANCE_DESIGN_DOC.replace("Target project: local_first_agent_os\n", "")
    assert "Target project:" not in document

    parsed = parse_design_doc(document, design_doc_id="no-declared-target")
    outcome = compile_design_doc(parsed, design_doc_revision_id="ddr-no-target")

    assert isinstance(outcome, CompiledPlanOutcome)
    assert outcome.runnable is False
    assert outcome.validation_status is ValidationStatus.BLOCKED
    blocker = "; ".join(outcome.execution_blockers)
    assert "declares no target project" in blocker
    assert "Target project:" in blocker, "the blocker must name the line that fixes it"


def test_a_declared_target_is_not_blocked() -> None:
    """The blocker must not fire for a document that says what it means."""

    parsed = parse_design_doc(ACCEPTANCE_DESIGN_DOC, design_doc_id="declared-target")
    outcome = compile_design_doc(parsed, design_doc_revision_id="ddr-declared")

    assert isinstance(outcome, CompiledPlanOutcome)
    assert outcome.runnable is True
    assert not [item for item in outcome.execution_blockers if "target project" in item]


def test_a_document_that_declares_no_envelope_still_compiles_a_ceiling() -> None:
    """Silence is not unlimited authority.

    A document with no Permission Envelope used to compile to
    ``permission_policy is None``, which meant no ceiling at all: every
    executor kept every capability it declared, including the one that
    publishes a deployment, in a document that never said the word.

    The baseline replaces that with read, write an isolated worktree, run the
    declared tests, record artifacts, and ask. It adds no start approval,
    because a document that asked for nothing has given the operator nothing to
    approve, and a gate on every document is a gate nobody reads.
    """

    outcome = _compile(ACCEPTANCE_DESIGN_DOC)

    assert isinstance(outcome, CompiledPlanOutcome)
    assert outcome.plan.permission_policy is not None
    policy = outcome.plan.permission_policy
    assert policy.capability_ceiling == (
        "ask_operator",
        "invoke_model",
        "read_repository",
        "run_command",
        "write_artifact",
        "write_repository",
    )
    assert "publish_deployment" in policy.denied_capabilities
    assert "spend_money" in policy.denied_capabilities
    assert "merge_to_main" in policy.denied_capabilities
    assert not policy.requires_start_approval
    assert outcome.execution_blockers == ()
    assert outcome.runnable
    # The implement milestone still has what it needs to act.
    implement = outcome.plan.milestone("c")
    assert "write_repository" in implement.tool_policy.permitted_tools
    assert "run_command" in implement.tool_policy.permitted_tools


def test_an_undeclared_document_cannot_compile_a_deployer_that_cannot_deploy() -> None:
    """The `deliver.deployment` counterpart of the 2026-08-10 empty-diff bug.

    `publish_deployment` is the one capability an undeclared document loses to
    the baseline ceiling, and `deliver.deployment` declares nothing else it
    could act with. Without this the milestone compiles clean, passes its
    `ALWAYS` approval gate, runs, and delivers nothing - a gate opening onto an
    executor that was already disarmed.
    """

    document = (
        ACCEPTANCE_DESIGN_DOC
        + """

### Milestone G: publish the release

Phase: DELIVER
Executor: deliver.deployment
Depends on: F
Acceptance: the deployment is live
Artifacts: deployment_record
"""
    )

    outcome = _compile(document)

    assert isinstance(outcome, CompiledPlanOutcome)
    blockers = [item for item in outcome.execution_blockers if "'g'" in item]
    assert blockers, "a deployer with no deploy capability must block execution"
    assert any("publish_deployment" in item for item in blockers)
    # The message carries its own fix in the envelope's vocabulary.
    assert any("'deploy'" in item for item in blockers)
    assert not outcome.runnable


def test_declaring_deploy_lets_the_same_document_compile_runnable() -> None:
    """The declared counterpart: the document says deploy, so it may deploy."""

    document = (
        ACCEPTANCE_DESIGN_DOC
        + """

### Milestone G: publish the release

Phase: DELIVER
Executor: deliver.deployment
Depends on: F
Acceptance: the deployment is live
Artifacts: deployment_record

## Permission Envelope

Autonomous permissions:
- read_repo_context
- code_worktree_write
- test_command_execution
- write_ledger_artifacts
- run_local_model_delegates
- request_operator_decisions
- deploy

Denied without explicit approval:
- merge_to_main
- external_communications
"""
    )

    outcome = _compile(document)

    assert isinstance(outcome, CompiledPlanOutcome)
    assert outcome.execution_blockers == ()
    assert outcome.runnable
    assert "publish_deployment" in outcome.plan.milestone("g").tool_policy.permitted_tools
