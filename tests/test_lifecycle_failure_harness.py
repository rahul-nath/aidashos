# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from collections.abc import Mapping

import pytest
from pydantic import ValidationError

from local_first_agent_os.contracts import ApprovalStatus
from local_first_agent_os.coordination import DispatchTerminalStatus
from local_first_agent_os.dispatcher import IntentResult, LedgerDispatcher
from local_first_agent_os.lifecycle_failure_harness import (
    CRITICAL_LIFECYCLE_FAULT_CASES,
    IMPLEMENTED_LIFECYCLE_TRANSITION_POINTS,
    LIFECYCLE_FAULT_CATALOG,
    LIFECYCLE_FAULT_CATALOG_DOCUMENT,
    VALID_LIFECYCLE_TRANSITIONS,
    LifecycleEvidenceKind,
    LifecycleFailureHarness,
    LifecycleFailureScenario,
    LifecycleFault,
    LifecycleFaultAction,
    LifecycleFaultCatalogDocument,
    LifecycleFaultInvocation,
    LifecycleFrontierFixture,
    LifecycleInvariantFacts,
    LifecycleInvariantViolation,
    LifecycleProjectFixture,
    LifecycleScenarioExpected,
    LifecycleTransitionPoint,
    LifecycleVerificationStatus,
    ModelUsageLimitDisposition,
    assert_lifecycle_invariants,
    evaluate_lifecycle_invariants,
    generate_pairwise_fault_cases,
    generate_seeded_state_machine_fault_cases,
    generate_single_fault_cases,
    lifecycle_failure_harness,
    lifecycle_faults_can_coexist,
    reach_lifecycle_transition,
    scenario_from_fault_case,
)
from local_first_agent_os.project_action import ProjectActionKind


def _expected() -> LifecycleScenarioExpected:
    return LifecycleScenarioExpected(
        action_state=ProjectActionKind.RECOVERABLE_FAILURE,
        preserved_commit=True,
        preserved_findings=True,
        duplicate_intents=0,
        merge_performed=False,
        next_action="resume_bounded_revision",
    )


def _scenario(
    *faults: LifecycleFault,
    seed: int = 7,
) -> LifecycleFailureScenario:
    return LifecycleFailureScenario(
        name="blocked-review-crash-before-revision",
        seed=seed,
        project_fixture=LifecycleProjectFixture.DISPOSABLE_GIT_REPO,
        frontier_fixture=LifecycleFrontierFixture.FAKE_CLAUDE_THEN_FAKE_CODEX,
        faults=faults,
        restart=True,
        expected=_expected(),
    )


def _valid_invariant_facts() -> LifecycleInvariantFacts:
    return LifecycleInvariantFacts(
        active_claim_count=1,
        acknowledged_evidence=frozenset(
            {
                LifecycleEvidenceKind.INTENT,
                LifecycleEvidenceKind.REVIEW,
                LifecycleEvidenceKind.APPROVAL,
                LifecycleEvidenceKind.CHECKPOINT,
            }
        ),
        preserved_evidence=frozenset(
            {
                LifecycleEvidenceKind.INTENT,
                LifecycleEvidenceKind.REVIEW,
                LifecycleEvidenceKind.APPROVAL,
                LifecycleEvidenceKind.CHECKPOINT,
            }
        ),
        verification_status=LifecycleVerificationStatus.PASSED,
        approvable_merge_request=True,
        review_verdict_recorded=True,
        reviewed_commit_sha="approved-commit",
        review_checkpoint_commit_sha="approved-commit",
        revision_started=True,
        revision_reused_accepted_base=True,
        revision_reused_blocked_commit=True,
        revision_reused_findings=True,
        revision_reused_permission_envelope=True,
        model_usage_limit_disposition=ModelUsageLimitDisposition.NOT_OBSERVED,
        fallback_used=False,
        fallback_permissions_widened=False,
        fallback_metered_spend_enabled=False,
        approval_status=ApprovalStatus.APPROVED,
        approval_resolution_claimed_git_integration=False,
        git_integration_performed=True,
        approved_commit_sha="approved-commit",
        integrated_commit_sha="approved-commit",
        milestone_completed=True,
        git_merge_count=1,
        remote_preview_creation_count=1,
        production_deployment_performed=False,
        outbound_contact_performed=False,
        visible_next_action_valid=True,
    )


def test_design_example_is_a_strict_round_trippable_contract() -> None:
    scenario = LifecycleFailureScenario.model_validate(
        {
            "schema_version": "lifecycle_failure_scenario.v1",
            "name": "blocked-review-crash-before-revision",
            "project_fixture": "disposable_git_repo",
            "frontier_fixture": "fake_claude_then_fake_codex",
            "faults": [
                {
                    "at": "after_review_block_recorded",
                    "action": "terminate_runtime",
                }
            ],
            "restart": True,
            "expected": {
                "action_state": "RECOVERABLE_FAILURE",
                "preserved_commit": True,
                "preserved_findings": True,
                "duplicate_intents": 0,
                "merge_performed": False,
                "next_action": "resume_bounded_revision",
            },
        }
    )

    restored = LifecycleFailureScenario.model_validate_json(scenario.model_dump_json())

    assert restored == scenario
    assert len(scenario.reproduction_id) == 64
    assert restored.reproduction_id == scenario.reproduction_id


def test_fault_catalog_and_transition_matrix_are_versioned_round_trippable_data() -> None:
    encoded = LIFECYCLE_FAULT_CATALOG_DOCUMENT.model_dump_json()
    decoded = LifecycleFaultCatalogDocument.model_validate_json(encoded)

    assert decoded == LIFECYCLE_FAULT_CATALOG_DOCUMENT
    assert decoded.schema_version == "lifecycle_fault_catalog.v1"
    assert {entry.action for entry in decoded.fault_actions} == set(LifecycleFaultAction)
    assert {rule.point for rule in decoded.transitions} == set(LifecycleTransitionPoint)
    assert {entry.action: frozenset(entry.valid_at) for entry in decoded.fault_actions} == {
        action: rule.valid_at for action, rule in LIFECYCLE_FAULT_CATALOG.items()
    }
    assert {rule.point: rule.successors for rule in decoded.transitions} == dict(
        VALID_LIFECYCLE_TRANSITIONS
    )


def test_unknown_schema_fields_and_invalid_fault_points_fail_closed() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        LifecycleFault.model_validate(
            {
                "at": "during_agent_stream",
                "action": "return_model_usage_limit",
                "silent_fallback_to_paid_model": True,
            }
        )

    with pytest.raises(ValidationError, match="is not valid at"):
        LifecycleFault(
            at=LifecycleTransitionPoint.AFTER_INTENT_CREATED,
            action=LifecycleFaultAction.RETURN_MODEL_USAGE_LIMIT,
        )


def test_same_transition_exclusive_actions_are_unrepresentable() -> None:
    malformed = LifecycleFault(
        at=LifecycleTransitionPoint.DURING_AGENT_STREAM,
        action=LifecycleFaultAction.EMIT_MALFORMED_TERMINAL_OUTPUT,
    )
    missing = LifecycleFault(
        at=LifecycleTransitionPoint.DURING_AGENT_STREAM,
        action=LifecycleFaultAction.OMIT_TERMINAL_OUTPUT,
    )

    assert lifecycle_faults_can_coexist(malformed, missing) is False
    with pytest.raises(ValidationError, match="cannot coexist"):
        _scenario(malformed, missing)


def test_occurrences_make_repeated_stream_faults_precise() -> None:
    first = LifecycleFault(
        at=LifecycleTransitionPoint.DURING_AGENT_STREAM,
        action=LifecycleFaultAction.EMIT_MALFORMED_TERMINAL_OUTPUT,
        occurrence=1,
    )
    second = LifecycleFault(
        at=LifecycleTransitionPoint.DURING_AGENT_STREAM,
        action=LifecycleFaultAction.OMIT_TERMINAL_OUTPUT,
        occurrence=2,
    )

    assert _scenario(first, second).faults == (first, second)


def test_harness_triggers_in_order_with_stable_per_invocation_seeds() -> None:
    first = LifecycleFault(
        at=LifecycleTransitionPoint.AFTER_INTENT_CLAIMED,
        action=LifecycleFaultAction.DROP_DATABASE_CONNECTION,
    )
    second = LifecycleFault(
        at=LifecycleTransitionPoint.DURING_AGENT_STREAM,
        action=LifecycleFaultAction.EMIT_OVERSIZED_JSONL,
        occurrence=2,
    )
    seen: list[LifecycleFaultInvocation] = []
    handlers = {
        LifecycleFaultAction.DROP_DATABASE_CONNECTION: seen.append,
        LifecycleFaultAction.EMIT_OVERSIZED_JSONL: seen.append,
    }

    harness = LifecycleFailureHarness(_scenario(first, second), handlers)
    harness.reach(LifecycleTransitionPoint.AFTER_INTENT_CLAIMED, intent_id="intent-1")
    harness.reach(LifecycleTransitionPoint.DURING_AGENT_STREAM, lease_id="lease-1")
    harness.reach(LifecycleTransitionPoint.DURING_AGENT_STREAM, lease_id="lease-1")
    harness.assert_all_faults_triggered()

    replay: list[LifecycleFaultInvocation] = []
    replay_harness = LifecycleFailureHarness(
        _scenario(first, second),
        {
            LifecycleFaultAction.DROP_DATABASE_CONNECTION: replay.append,
            LifecycleFaultAction.EMIT_OVERSIZED_JSONL: replay.append,
        },
    )
    replay_harness.reach(
        LifecycleTransitionPoint.AFTER_INTENT_CLAIMED,
        intent_id="intent-1",
    )
    replay_harness.reach(LifecycleTransitionPoint.DURING_AGENT_STREAM, lease_id="lease-1")
    replay_harness.reach(LifecycleTransitionPoint.DURING_AGENT_STREAM, lease_id="lease-1")

    assert [item.fault for item in seen] == [first, second]
    assert [item.seed for item in seen] == [item.seed for item in replay]
    assert seen[0].transition.facts == {"intent_id": "intent-1"}
    assert seen[1].transition.occurrence == 2


def test_handler_must_exist_before_a_scenario_can_start() -> None:
    fault = LifecycleFault(
        at=LifecycleTransitionPoint.AFTER_INTENT_CREATED,
        action=LifecycleFaultAction.DROP_DATABASE_CONNECTION,
    )

    with pytest.raises(ValueError, match="drop_database_connection"):
        LifecycleFailureHarness(_scenario(fault), {})


def test_context_local_hook_is_inert_without_an_installed_harness() -> None:
    fault = LifecycleFault(
        at=LifecycleTransitionPoint.AFTER_REVIEW_BLOCK_RECORDED,
        action=LifecycleFaultAction.TERMINATE_RUNTIME,
    )
    seen: list[LifecycleFaultInvocation] = []
    harness = LifecycleFailureHarness(
        _scenario(fault),
        {LifecycleFaultAction.TERMINATE_RUNTIME: seen.append},
    )

    assert (
        reach_lifecycle_transition(
            LifecycleTransitionPoint.AFTER_REVIEW_BLOCK_RECORDED,
            commit_sha="abc",
        )
        == ()
    )
    with lifecycle_failure_harness(harness):
        reach_lifecycle_transition(
            LifecycleTransitionPoint.AFTER_REVIEW_BLOCK_RECORDED,
            commit_sha="abc",
        )
    assert len(seen) == 1


def test_dispatcher_exposes_the_durable_post_claim_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fault = LifecycleFault(
        at=LifecycleTransitionPoint.AFTER_INTENT_CLAIMED,
        action=LifecycleFaultAction.RAISE_PROCESS_EXCEPTION,
    )
    scenario = _scenario(fault)
    runner_called = False

    def runner(_intent: Mapping[str, object]) -> IntentResult:
        nonlocal runner_called
        runner_called = True
        return DispatchTerminalStatus.DONE, None, None

    dispatcher = LedgerDispatcher(runner)
    commands: list[object] = []

    def coordinate(command: object) -> dict[str, object]:
        commands.append(command)
        return {
            "intent": {
                "intent_id": "intent-1",
                "tier": "senior",
                "target_project_id": "target-1",
                "payload": {},
            }
        }

    monkeypatch.setattr(dispatcher, "_coord", coordinate)

    class InjectedClaimFailure(RuntimeError):
        pass

    def raise_failure(_invocation: LifecycleFaultInvocation) -> None:
        raise InjectedClaimFailure("crash after durable claim")

    harness = LifecycleFailureHarness(
        scenario,
        {LifecycleFaultAction.RAISE_PROCESS_EXCEPTION: raise_failure},
    )
    with (
        lifecycle_failure_harness(harness),
        pytest.raises(InjectedClaimFailure, match="durable claim"),
    ):
        dispatcher.poll_once()

    harness.assert_all_faults_triggered()
    assert len(commands) == 1
    assert runner_called is False


def test_non_json_transition_facts_are_rejected_before_fault_execution() -> None:
    fault = LifecycleFault(
        at=LifecycleTransitionPoint.AFTER_INTENT_CREATED,
        action=LifecycleFaultAction.DROP_DATABASE_CONNECTION,
    )
    harness = LifecycleFailureHarness(
        _scenario(fault),
        {LifecycleFaultAction.DROP_DATABASE_CONNECTION: lambda _invocation: None},
    )

    with pytest.raises(TypeError):
        harness.reach(
            LifecycleTransitionPoint.AFTER_INTENT_CREATED,
            invalid=object(),
        )
    with pytest.raises(AssertionError, match="without triggering"):
        harness.assert_all_faults_triggered()


def test_shared_oracle_accepts_a_safe_recovered_lifecycle() -> None:
    facts = _valid_invariant_facts()
    fault = LifecycleFault(
        at=LifecycleTransitionPoint.AFTER_INTENT_CREATED,
        action=LifecycleFaultAction.DROP_DATABASE_CONNECTION,
    )
    harness = LifecycleFailureHarness(
        _scenario(fault),
        {LifecycleFaultAction.DROP_DATABASE_CONNECTION: lambda _invocation: None},
    )

    assert evaluate_lifecycle_invariants(facts) == ()
    assert_lifecycle_invariants(facts)
    harness.assert_invariants(facts)
    harness.assert_expected_outcome(_expected())


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"active_claim_count": 2}, LifecycleInvariantViolation.MULTIPLE_ACTIVE_CLAIMS),
        (
            {"preserved_evidence": {LifecycleEvidenceKind.INTENT}},
            LifecycleInvariantViolation.ACKNOWLEDGED_EVIDENCE_LOST,
        ),
        (
            {"verification_status": LifecycleVerificationStatus.FAILED},
            LifecycleInvariantViolation.UNVERIFIED_MERGE_REQUEST,
        ),
        (
            {"review_checkpoint_commit_sha": "different"},
            LifecycleInvariantViolation.REVIEW_COMMIT_MISMATCH,
        ),
        (
            {"revision_reused_findings": False},
            LifecycleInvariantViolation.REVISION_CONTEXT_WIDENED,
        ),
        (
            {"model_usage_limit_disposition": ModelUsageLimitDisposition.APPROVAL},
            LifecycleInvariantViolation.USAGE_LIMIT_PROMOTED,
        ),
        (
            {"fallback_used": True, "fallback_permissions_widened": True},
            LifecycleInvariantViolation.FALLBACK_WIDENED_PERMISSIONS,
        ),
        (
            {"fallback_used": True, "fallback_metered_spend_enabled": True},
            LifecycleInvariantViolation.FALLBACK_ENABLED_METERED_SPEND,
        ),
        (
            {"approval_resolution_claimed_git_integration": True},
            LifecycleInvariantViolation.APPROVAL_CLAIMED_GIT_INTEGRATION,
        ),
        (
            {"approval_status": ApprovalStatus.REVOKED},
            LifecycleInvariantViolation.REVOKED_APPROVAL_PROMOTED,
        ),
        (
            {"integrated_commit_sha": "unapproved"},
            LifecycleInvariantViolation.UNAPPROVED_COMMIT_INTEGRATED,
        ),
        (
            {"approval_status": ApprovalStatus.PENDING},
            LifecycleInvariantViolation.UNAPPROVED_COMMIT_INTEGRATED,
        ),
        (
            {"git_integration_performed": False},
            LifecycleInvariantViolation.MILESTONE_COMPLETED_BEFORE_EXACT_MERGE,
        ),
        ({"git_merge_count": 2}, LifecycleInvariantViolation.DUPLICATE_GIT_MERGE),
        (
            {"remote_preview_creation_count": 2},
            LifecycleInvariantViolation.DUPLICATE_REMOTE_PREVIEW,
        ),
        (
            {"production_deployment_performed": True},
            LifecycleInvariantViolation.FORBIDDEN_EXTERNAL_EFFECT,
        ),
        (
            {"visible_next_action_valid": False},
            LifecycleInvariantViolation.INVALID_VISIBLE_NEXT_ACTION,
        ),
    ],
)
def test_shared_oracle_reports_each_design_invariant(
    updates: dict[str, object],
    expected: LifecycleInvariantViolation,
) -> None:
    facts = _valid_invariant_facts().model_copy(update=updates)

    assert expected in evaluate_lifecycle_invariants(facts)
    with pytest.raises(AssertionError, match=expected.value):
        assert_lifecycle_invariants(facts)


def test_expected_outcome_mismatch_is_visible() -> None:
    fault = LifecycleFault(
        at=LifecycleTransitionPoint.AFTER_INTENT_CREATED,
        action=LifecycleFaultAction.DROP_DATABASE_CONNECTION,
    )
    harness = LifecycleFailureHarness(
        _scenario(fault),
        {LifecycleFaultAction.DROP_DATABASE_CONNECTION: lambda _invocation: None},
    )
    wrong = _expected().model_copy(update={"action_state": ProjectActionKind.COMPLETE})

    with pytest.raises(AssertionError, match="outcome mismatch"):
        harness.assert_expected_outcome(wrong)


def test_single_fault_generation_covers_the_entire_catalog_once() -> None:
    cases = generate_single_fault_cases(seed=50)
    generated = {
        (case.faults[0].at, case.faults[0].action, case.faults[0].occurrence) for case in cases
    }
    expected = {
        (point, action, 1)
        for action, rule in LIFECYCLE_FAULT_CATALOG.items()
        for point in rule.valid_at
    }

    assert generated == expected
    assert len(cases) == len(expected)
    assert len({case.name for case in cases}) == len(cases)


def _covered_pairs(
    cases: tuple,
) -> set[frozenset[LifecycleFault]]:
    covered: set[frozenset[LifecycleFault]] = set()
    for case in cases:
        for index, left in enumerate(case.faults):
            for right in case.faults[index + 1 :]:
                if lifecycle_faults_can_coexist(left, right):
                    covered.add(frozenset((left, right)))
    return covered


def test_pairwise_generation_covers_every_coexistent_pair_deterministically() -> None:
    catalog_faults = tuple(case.faults[0] for case in generate_single_fault_cases())
    expected_pairs = {
        frozenset((left, right))
        for index, left in enumerate(catalog_faults)
        for right in catalog_faults[index + 1 :]
        if lifecycle_faults_can_coexist(left, right)
    }

    cases = generate_pairwise_fault_cases(seed=19)

    assert _covered_pairs(cases) == expected_pairs
    assert cases == generate_pairwise_fault_cases(seed=19)
    assert all(2 <= len(case.faults) <= 4 for case in cases)
    assert len(cases) < len(expected_pairs)


def test_critical_cases_pin_checkpoint_merge_and_deploy_ambiguity_windows() -> None:
    names = {case.name for case in CRITICAL_LIFECYCLE_FAULT_CASES}

    assert names == {
        "checkpoint-commit-crash-before-persistence",
        "approval-branch-drift-before-merge",
        "approval-revoked-before-merge",
        "merge-crash-before-milestone-completion",
        "remote-preview-local-persistence-ambiguity",
    }
    assert {
        fault.at for case in CRITICAL_LIFECYCLE_FAULT_CASES for fault in case.faults
    }.issuperset(
        {
            LifecycleTransitionPoint.AFTER_CHECKPOINT_GIT_COMMIT,
            LifecycleTransitionPoint.BEFORE_CHECKPOINT_PERSISTED,
            LifecycleTransitionPoint.AFTER_MERGE_APPROVAL_RESOLVED,
            LifecycleTransitionPoint.AFTER_GIT_MERGE,
            LifecycleTransitionPoint.AFTER_REMOTE_PREVIEW_CREATED,
        }
    )


def test_only_real_runtime_boundaries_are_marked_implemented() -> None:
    assert (
        frozenset(
            {
                LifecycleTransitionPoint.AFTER_MILESTONE_SELECTED,
                LifecycleTransitionPoint.AFTER_INTENT_CREATED,
                LifecycleTransitionPoint.AFTER_INTENT_CLAIMED,
                LifecycleTransitionPoint.AFTER_LEASE_STARTED,
                LifecycleTransitionPoint.DURING_AGENT_STREAM,
                LifecycleTransitionPoint.AFTER_CHECKPOINT_GIT_COMMIT,
                LifecycleTransitionPoint.BEFORE_CHECKPOINT_PERSISTED,
                LifecycleTransitionPoint.AFTER_VERIFICATION_RECORDED,
                LifecycleTransitionPoint.AFTER_REVIEW_BLOCK_RECORDED,
                LifecycleTransitionPoint.AFTER_REVISION_STARTED,
                LifecycleTransitionPoint.AFTER_MERGE_APPROVAL_RESOLVED,
                LifecycleTransitionPoint.BEFORE_MILESTONE_COMPLETED,
            }
        )
        == IMPLEMENTED_LIFECYCLE_TRANSITION_POINTS
    )
    assert LifecycleTransitionPoint.AFTER_GIT_MERGE not in (IMPLEMENTED_LIFECYCLE_TRANSITION_POINTS)
    assert LifecycleTransitionPoint.AFTER_REMOTE_PREVIEW_CREATED not in (
        IMPLEMENTED_LIFECYCLE_TRANSITION_POINTS
    )
    assert LifecycleTransitionPoint.BEFORE_DEPLOYMENT_EVIDENCE_PERSISTED not in (
        IMPLEMENTED_LIFECYCLE_TRANSITION_POINTS
    )


def test_seeded_state_machine_generation_is_bounded_reproducible_and_composable() -> None:
    cases = generate_seeded_state_machine_fault_cases(
        seed=90210,
        case_count=20,
        max_steps=30,
        max_faults=5,
    )

    assert cases == generate_seeded_state_machine_fault_cases(
        seed=90210,
        case_count=20,
        max_steps=30,
        max_faults=5,
    )
    assert len(cases) == 20
    assert len({case.faults for case in cases}) == 20
    assert all(1 <= len(case.faults) <= 5 for case in cases)
    assert all(
        lifecycle_faults_can_coexist(left, right)
        for case in cases
        for index, left in enumerate(case.faults)
        for right in case.faults[index + 1 :]
    )


def test_fault_case_becomes_a_serializable_scenario_only_with_an_oracle() -> None:
    case = CRITICAL_LIFECYCLE_FAULT_CASES[0]

    scenario = scenario_from_fault_case(
        case,
        expected=_expected(),
        restart=True,
    )

    assert scenario.name == case.name
    assert scenario.faults == case.faults
    assert scenario.expected.next_action == "resume_bounded_revision"
    assert scenario.expected.production_deployment_performed is False
    assert scenario.expected.outbound_contact_performed is False
