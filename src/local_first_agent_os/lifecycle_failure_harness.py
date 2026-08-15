# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Typed, deterministic failure injection for durable lifecycle tests.

The production runtime only names lifecycle transitions. Test fixtures own the
mechanics for killing processes, disconnecting Postgres, corrupting frontier
output, or failing artifact stores. This separation prevents test-only failure
capabilities from becoming a hidden runtime control plane.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import itertools
import json
import random
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import ApprovalStatus, ProjectActionKind

LIFECYCLE_FAILURE_SCENARIO_SCHEMA_VERSION = "lifecycle_failure_scenario.v1"
LIFECYCLE_FAULT_CATALOG_SCHEMA_VERSION = "lifecycle_fault_catalog.v1"


class LifecycleTransitionPoint(StrEnum):
    AFTER_MILESTONE_SELECTED = "after_milestone_selected"
    AFTER_INTENT_CREATED = "after_intent_created"
    AFTER_INTENT_CLAIMED = "after_intent_claimed"
    AFTER_LEASE_STARTED = "after_lease_started"
    DURING_AGENT_STREAM = "during_agent_stream"
    AFTER_CHECKPOINT_GIT_COMMIT = "after_checkpoint_git_commit"
    BEFORE_CHECKPOINT_PERSISTED = "before_checkpoint_persisted"
    AFTER_VERIFICATION_RECORDED = "after_verification_recorded"
    AFTER_REVIEW_BLOCK_RECORDED = "after_review_block_recorded"
    AFTER_REVISION_STARTED = "after_revision_started"
    AFTER_MERGE_APPROVAL_RESOLVED = "after_merge_approval_resolved"
    AFTER_GIT_MERGE = "after_git_merge"
    BEFORE_MILESTONE_COMPLETED = "before_milestone_completed"
    AFTER_REMOTE_PREVIEW_CREATED = "after_remote_preview_created"
    BEFORE_DEPLOYMENT_EVIDENCE_PERSISTED = "before_deployment_evidence_persisted"


class LifecycleFaultAction(StrEnum):
    RAISE_PROCESS_EXCEPTION = "raise_process_exception"
    SIGTERM_WORKER = "sigterm_worker"
    SIGTERM_THEN_SIGKILL_WORKER = "sigterm_then_sigkill_worker"
    TERMINATE_RUNTIME = "terminate_runtime"
    DROP_DATABASE_CONNECTION = "drop_database_connection"
    BLOCK_ARTIFACT_PERSISTENCE = "block_artifact_persistence"
    FAIL_ARTIFACT_PERSISTENCE = "fail_artifact_persistence"
    EMIT_OVERSIZED_JSONL = "emit_oversized_jsonl"
    EMIT_MALFORMED_TERMINAL_OUTPUT = "emit_malformed_terminal_output"
    OMIT_TERMINAL_OUTPUT = "omit_terminal_output"
    RETURN_MODEL_USAGE_LIMIT = "return_model_usage_limit"
    FAIL_VERIFICATION = "fail_verification"
    ADVANCE_TARGET_BRANCH = "advance_target_branch"
    REVOKE_APPROVAL = "revoke_approval"
    REMOTE_SUCCESS_THEN_PERSISTENCE_FAILURE = "remote_success_then_persistence_failure"


class LifecycleProjectFixture(StrEnum):
    DISPOSABLE_GIT_REPO = "disposable_git_repo"


class LifecycleFrontierFixture(StrEnum):
    FAKE_CLAUDE_THEN_FAKE_CODEX = "fake_claude_then_fake_codex"


class LifecycleEvidenceKind(StrEnum):
    INTENT = "intent"
    REVIEW = "review"
    APPROVAL = "approval"
    CHECKPOINT = "checkpoint"


class LifecycleVerificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_RUN = "not_run"
    STALE = "stale"


class ModelUsageLimitDisposition(StrEnum):
    NOT_OBSERVED = "not_observed"
    FAILURE = "failure"
    APPROVAL = "approval"
    SUCCESS = "success"


class LifecycleInvariantViolation(StrEnum):
    MULTIPLE_ACTIVE_CLAIMS = "multiple_active_claims"
    ACKNOWLEDGED_EVIDENCE_LOST = "acknowledged_evidence_lost"
    UNVERIFIED_MERGE_REQUEST = "unverified_merge_request"
    REVIEW_COMMIT_MISMATCH = "review_commit_mismatch"
    REVISION_CONTEXT_WIDENED = "revision_context_widened"
    USAGE_LIMIT_PROMOTED = "usage_limit_promoted"
    FALLBACK_WIDENED_PERMISSIONS = "fallback_widened_permissions"
    FALLBACK_ENABLED_METERED_SPEND = "fallback_enabled_metered_spend"
    APPROVAL_CLAIMED_GIT_INTEGRATION = "approval_claimed_git_integration"
    REVOKED_APPROVAL_PROMOTED = "revoked_approval_promoted"
    UNAPPROVED_COMMIT_INTEGRATED = "unapproved_commit_integrated"
    MILESTONE_COMPLETED_BEFORE_EXACT_MERGE = "milestone_completed_before_exact_merge"
    DUPLICATE_GIT_MERGE = "duplicate_git_merge"
    DUPLICATE_REMOTE_PREVIEW = "duplicate_remote_preview"
    FORBIDDEN_EXTERNAL_EFFECT = "forbidden_external_effect"
    INVALID_VISIBLE_NEXT_ACTION = "invalid_visible_next_action"


class LifecycleInvariantFacts(BaseModel):
    """Normalized final facts from a scenario driver.

    This diagnostic boundary deliberately represents invalid combinations so
    the oracle can report them. Runtime contracts should continue to use sum
    types that make those combinations impossible.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    active_claim_count: int = Field(ge=0)
    acknowledged_evidence: frozenset[LifecycleEvidenceKind] = frozenset()
    preserved_evidence: frozenset[LifecycleEvidenceKind] = frozenset()
    verification_status: LifecycleVerificationStatus
    approvable_merge_request: bool
    review_verdict_recorded: bool
    reviewed_commit_sha: str | None = None
    review_checkpoint_commit_sha: str | None = None
    revision_started: bool
    revision_reused_accepted_base: bool
    revision_reused_blocked_commit: bool
    revision_reused_findings: bool
    revision_reused_permission_envelope: bool
    model_usage_limit_disposition: ModelUsageLimitDisposition
    fallback_used: bool
    fallback_permissions_widened: bool
    fallback_metered_spend_enabled: bool
    approval_status: ApprovalStatus | None
    approval_resolution_claimed_git_integration: bool
    git_integration_performed: bool
    approved_commit_sha: str | None = None
    integrated_commit_sha: str | None = None
    milestone_completed: bool
    git_merge_count: int = Field(ge=0)
    remote_preview_creation_count: int = Field(ge=0)
    production_deployment_performed: bool
    outbound_contact_performed: bool
    visible_next_action_valid: bool


def evaluate_lifecycle_invariants(
    facts: LifecycleInvariantFacts,
) -> tuple[LifecycleInvariantViolation, ...]:
    violations: list[LifecycleInvariantViolation] = []
    if facts.active_claim_count > 1:
        violations.append(LifecycleInvariantViolation.MULTIPLE_ACTIVE_CLAIMS)
    if not facts.acknowledged_evidence.issubset(facts.preserved_evidence):
        violations.append(LifecycleInvariantViolation.ACKNOWLEDGED_EVIDENCE_LOST)
    if (
        facts.approvable_merge_request
        and facts.verification_status is not LifecycleVerificationStatus.PASSED
    ):
        violations.append(LifecycleInvariantViolation.UNVERIFIED_MERGE_REQUEST)
    if facts.review_verdict_recorded and (
        not facts.reviewed_commit_sha
        or facts.reviewed_commit_sha != facts.review_checkpoint_commit_sha
    ):
        violations.append(LifecycleInvariantViolation.REVIEW_COMMIT_MISMATCH)
    if facts.revision_started and not all(
        (
            facts.revision_reused_accepted_base,
            facts.revision_reused_blocked_commit,
            facts.revision_reused_findings,
            facts.revision_reused_permission_envelope,
        )
    ):
        violations.append(LifecycleInvariantViolation.REVISION_CONTEXT_WIDENED)
    if facts.model_usage_limit_disposition not in {
        ModelUsageLimitDisposition.NOT_OBSERVED,
        ModelUsageLimitDisposition.FAILURE,
    }:
        violations.append(LifecycleInvariantViolation.USAGE_LIMIT_PROMOTED)
    if facts.fallback_used and facts.fallback_permissions_widened:
        violations.append(LifecycleInvariantViolation.FALLBACK_WIDENED_PERMISSIONS)
    if facts.fallback_used and facts.fallback_metered_spend_enabled:
        violations.append(LifecycleInvariantViolation.FALLBACK_ENABLED_METERED_SPEND)
    if facts.approval_resolution_claimed_git_integration:
        violations.append(LifecycleInvariantViolation.APPROVAL_CLAIMED_GIT_INTEGRATION)
    if facts.approval_status is ApprovalStatus.REVOKED and (
        facts.approvable_merge_request
        or facts.git_integration_performed
        or facts.milestone_completed
    ):
        violations.append(LifecycleInvariantViolation.REVOKED_APPROVAL_PROMOTED)
    exact_approved_commit_integrated = (
        facts.git_integration_performed
        and facts.approval_status is ApprovalStatus.APPROVED
        and bool(facts.approved_commit_sha)
        and facts.integrated_commit_sha == facts.approved_commit_sha
    )
    if facts.git_integration_performed and not exact_approved_commit_integrated:
        violations.append(LifecycleInvariantViolation.UNAPPROVED_COMMIT_INTEGRATED)
    if facts.milestone_completed and not exact_approved_commit_integrated:
        violations.append(LifecycleInvariantViolation.MILESTONE_COMPLETED_BEFORE_EXACT_MERGE)
    if facts.git_merge_count > 1:
        violations.append(LifecycleInvariantViolation.DUPLICATE_GIT_MERGE)
    if facts.remote_preview_creation_count > 1:
        violations.append(LifecycleInvariantViolation.DUPLICATE_REMOTE_PREVIEW)
    if facts.production_deployment_performed or facts.outbound_contact_performed:
        violations.append(LifecycleInvariantViolation.FORBIDDEN_EXTERNAL_EFFECT)
    if not facts.visible_next_action_valid:
        violations.append(LifecycleInvariantViolation.INVALID_VISIBLE_NEXT_ACTION)
    return tuple(violations)


def assert_lifecycle_invariants(facts: LifecycleInvariantFacts) -> None:
    violations = evaluate_lifecycle_invariants(facts)
    if violations:
        rendered = ", ".join(violation.value for violation in violations)
        raise AssertionError(f"lifecycle invariant violation(s): {rendered}")


@dataclass(frozen=True)
class LifecycleFaultRule:
    valid_at: frozenset[LifecycleTransitionPoint]
    exclusive_group: str | None = None


class LifecycleFaultCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: LifecycleFaultAction
    valid_at: tuple[LifecycleTransitionPoint, ...] = Field(min_length=1)
    exclusive_group: str | None = None


class LifecycleTransitionRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    point: LifecycleTransitionPoint
    successors: tuple[LifecycleTransitionPoint, ...]


class LifecycleFaultCatalogDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["lifecycle_fault_catalog.v1"] = LIFECYCLE_FAULT_CATALOG_SCHEMA_VERSION
    fault_actions: tuple[LifecycleFaultCatalogEntry, ...]
    transitions: tuple[LifecycleTransitionRule, ...]

    @model_validator(mode="after")
    def require_total_one_to_one_catalog(self) -> Self:
        actions = tuple(entry.action for entry in self.fault_actions)
        if len(set(actions)) != len(actions) or set(actions) != set(LifecycleFaultAction):
            raise ValueError("fault catalog must define every action exactly once")
        points = tuple(rule.point for rule in self.transitions)
        if len(set(points)) != len(points) or set(points) != set(LifecycleTransitionPoint):
            raise ValueError("transition matrix must define every point exactly once")
        return self


_ALL_TRANSITION_POINTS = frozenset(LifecycleTransitionPoint)
_WORKER_TRANSITION_POINTS = frozenset(
    {
        LifecycleTransitionPoint.AFTER_INTENT_CLAIMED,
        LifecycleTransitionPoint.AFTER_LEASE_STARTED,
        LifecycleTransitionPoint.DURING_AGENT_STREAM,
        LifecycleTransitionPoint.AFTER_CHECKPOINT_GIT_COMMIT,
        LifecycleTransitionPoint.BEFORE_CHECKPOINT_PERSISTED,
        LifecycleTransitionPoint.AFTER_VERIFICATION_RECORDED,
        LifecycleTransitionPoint.AFTER_REVIEW_BLOCK_RECORDED,
        LifecycleTransitionPoint.AFTER_REVISION_STARTED,
    }
)
_ARTIFACT_TRANSITION_POINTS = frozenset(
    {
        LifecycleTransitionPoint.AFTER_LEASE_STARTED,
        LifecycleTransitionPoint.DURING_AGENT_STREAM,
        LifecycleTransitionPoint.AFTER_CHECKPOINT_GIT_COMMIT,
        LifecycleTransitionPoint.BEFORE_CHECKPOINT_PERSISTED,
        LifecycleTransitionPoint.AFTER_VERIFICATION_RECORDED,
        LifecycleTransitionPoint.AFTER_REVIEW_BLOCK_RECORDED,
        LifecycleTransitionPoint.BEFORE_DEPLOYMENT_EVIDENCE_PERSISTED,
    }
)

LIFECYCLE_FAULT_CATALOG: Mapping[LifecycleFaultAction, LifecycleFaultRule] = MappingProxyType(
    {
        LifecycleFaultAction.RAISE_PROCESS_EXCEPTION: LifecycleFaultRule(
            _ALL_TRANSITION_POINTS,
            exclusive_group="immediate_control_flow",
        ),
        LifecycleFaultAction.SIGTERM_WORKER: LifecycleFaultRule(
            _WORKER_TRANSITION_POINTS,
            exclusive_group="worker_termination",
        ),
        LifecycleFaultAction.SIGTERM_THEN_SIGKILL_WORKER: LifecycleFaultRule(
            _WORKER_TRANSITION_POINTS,
            exclusive_group="worker_termination",
        ),
        LifecycleFaultAction.TERMINATE_RUNTIME: LifecycleFaultRule(
            _ALL_TRANSITION_POINTS,
            exclusive_group="immediate_control_flow",
        ),
        LifecycleFaultAction.DROP_DATABASE_CONNECTION: LifecycleFaultRule(_ALL_TRANSITION_POINTS),
        LifecycleFaultAction.BLOCK_ARTIFACT_PERSISTENCE: LifecycleFaultRule(
            _ARTIFACT_TRANSITION_POINTS,
            exclusive_group="artifact_persistence",
        ),
        LifecycleFaultAction.FAIL_ARTIFACT_PERSISTENCE: LifecycleFaultRule(
            _ARTIFACT_TRANSITION_POINTS,
            exclusive_group="artifact_persistence",
        ),
        LifecycleFaultAction.EMIT_OVERSIZED_JSONL: LifecycleFaultRule(
            frozenset({LifecycleTransitionPoint.DURING_AGENT_STREAM}),
            exclusive_group="frontier_output",
        ),
        LifecycleFaultAction.EMIT_MALFORMED_TERMINAL_OUTPUT: LifecycleFaultRule(
            frozenset({LifecycleTransitionPoint.DURING_AGENT_STREAM}),
            exclusive_group="frontier_output",
        ),
        LifecycleFaultAction.OMIT_TERMINAL_OUTPUT: LifecycleFaultRule(
            frozenset({LifecycleTransitionPoint.DURING_AGENT_STREAM}),
            exclusive_group="frontier_output",
        ),
        LifecycleFaultAction.RETURN_MODEL_USAGE_LIMIT: LifecycleFaultRule(
            frozenset({LifecycleTransitionPoint.DURING_AGENT_STREAM}),
            exclusive_group="frontier_output",
        ),
        LifecycleFaultAction.FAIL_VERIFICATION: LifecycleFaultRule(
            frozenset({LifecycleTransitionPoint.AFTER_CHECKPOINT_GIT_COMMIT})
        ),
        LifecycleFaultAction.ADVANCE_TARGET_BRANCH: LifecycleFaultRule(
            frozenset(
                {
                    LifecycleTransitionPoint.AFTER_CHECKPOINT_GIT_COMMIT,
                    LifecycleTransitionPoint.AFTER_VERIFICATION_RECORDED,
                    LifecycleTransitionPoint.AFTER_REVIEW_BLOCK_RECORDED,
                    LifecycleTransitionPoint.AFTER_MERGE_APPROVAL_RESOLVED,
                }
            )
        ),
        LifecycleFaultAction.REVOKE_APPROVAL: LifecycleFaultRule(
            frozenset({LifecycleTransitionPoint.AFTER_MERGE_APPROVAL_RESOLVED}),
            exclusive_group="approval_resolution",
        ),
        LifecycleFaultAction.REMOTE_SUCCESS_THEN_PERSISTENCE_FAILURE: (
            LifecycleFaultRule(
                frozenset({LifecycleTransitionPoint.AFTER_REMOTE_PREVIEW_CREATED}),
                exclusive_group="deployment_persistence",
            )
        ),
    }
)

if frozenset(LIFECYCLE_FAULT_CATALOG) != frozenset(LifecycleFaultAction):
    raise AssertionError("every lifecycle fault action must have exactly one catalog rule")

IMPLEMENTED_LIFECYCLE_TRANSITION_POINTS = frozenset(
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


class LifecycleFault(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    at: LifecycleTransitionPoint
    action: LifecycleFaultAction
    occurrence: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def require_valid_transition(self) -> Self:
        if self.at not in LIFECYCLE_FAULT_CATALOG[self.action].valid_at:
            raise ValueError(f"{self.action.value} is not valid at {self.at.value}")
        return self


class LifecycleScenarioExpected(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_state: ProjectActionKind
    preserved_commit: bool
    preserved_findings: bool
    duplicate_intents: int = Field(ge=0)
    merge_performed: bool
    next_action: str = Field(min_length=1)
    production_deployment_performed: Literal[False] = False
    outbound_contact_performed: Literal[False] = False


def lifecycle_faults_can_coexist(left: LifecycleFault, right: LifecycleFault) -> bool:
    if left == right:
        return False
    if (left.at, left.occurrence) != (right.at, right.occurrence):
        return True
    left_group = LIFECYCLE_FAULT_CATALOG[left.action].exclusive_group
    right_group = LIFECYCLE_FAULT_CATALOG[right.action].exclusive_group
    return left_group is None or left_group != right_group


class LifecycleFailureScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["lifecycle_failure_scenario.v1"] = (
        LIFECYCLE_FAILURE_SCENARIO_SCHEMA_VERSION
    )
    name: str = Field(min_length=1, pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    seed: int = Field(default=0, ge=0)
    project_fixture: LifecycleProjectFixture
    frontier_fixture: LifecycleFrontierFixture
    faults: tuple[LifecycleFault, ...] = Field(min_length=1)
    restart: bool
    expected: LifecycleScenarioExpected

    @model_validator(mode="after")
    def require_composable_faults(self) -> Self:
        if len(set(self.faults)) != len(self.faults):
            raise ValueError("a lifecycle scenario cannot contain duplicate faults")
        for left, right in itertools.combinations(self.faults, 2):
            if not lifecycle_faults_can_coexist(left, right):
                raise ValueError(
                    "lifecycle faults cannot coexist at the same transition occurrence: "
                    f"{left.action.value}, {right.action.value}"
                )
        return self

    @property
    def reproduction_id(self) -> str:
        payload = self.model_dump(mode="json")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class LifecycleTransitionObservation:
    point: LifecycleTransitionPoint
    occurrence: int
    facts: Mapping[str, Any]


@dataclass(frozen=True)
class LifecycleFaultInvocation:
    fault: LifecycleFault
    transition: LifecycleTransitionObservation
    seed: int


class LifecycleFaultHandler(Protocol):
    def __call__(self, invocation: LifecycleFaultInvocation, /) -> None: ...


class LifecycleFailureHarness:
    """Trigger one serialized scenario against fixture-owned fault handlers."""

    def __init__(
        self,
        scenario: LifecycleFailureScenario,
        handlers: Mapping[LifecycleFaultAction, LifecycleFaultHandler],
    ) -> None:
        required_actions = {fault.action for fault in scenario.faults}
        missing = required_actions.difference(handlers)
        if missing:
            names = ", ".join(sorted(action.value for action in missing))
            raise ValueError(f"scenario has no fault handler for: {names}")
        self.scenario = scenario
        self._handlers = MappingProxyType(dict(handlers))
        self._random = random.Random(scenario.seed)
        self._occurrences: Counter[LifecycleTransitionPoint] = Counter()
        self._invocations: list[LifecycleFaultInvocation] = []
        self._triggered: set[LifecycleFault] = set()

    @property
    def invocations(self) -> tuple[LifecycleFaultInvocation, ...]:
        return tuple(self._invocations)

    def reach(
        self,
        point: LifecycleTransitionPoint,
        **facts: Any,
    ) -> tuple[LifecycleFaultInvocation, ...]:
        json.dumps(facts, sort_keys=True)
        self._occurrences[point] += 1
        transition = LifecycleTransitionObservation(
            point=point,
            occurrence=self._occurrences[point],
            facts=MappingProxyType(dict(facts)),
        )
        triggered_here: list[LifecycleFaultInvocation] = []
        for fault in self.scenario.faults:
            if fault.at is not point or fault.occurrence != transition.occurrence:
                continue
            invocation = LifecycleFaultInvocation(
                fault=fault,
                transition=transition,
                seed=self._random.getrandbits(64),
            )
            self._triggered.add(fault)
            self._invocations.append(invocation)
            triggered_here.append(invocation)
            self._handlers[fault.action](invocation)
        return tuple(triggered_here)

    def assert_all_faults_triggered(self) -> None:
        missing = set(self.scenario.faults).difference(self._triggered)
        if missing:
            rendered = ", ".join(
                sorted(
                    f"{fault.at.value}:{fault.occurrence}:{fault.action.value}" for fault in missing
                )
            )
            raise AssertionError(f"scenario completed without triggering: {rendered}")

    def assert_invariants(self, facts: LifecycleInvariantFacts) -> None:
        assert_lifecycle_invariants(facts)

    def assert_expected_outcome(self, actual: LifecycleScenarioExpected) -> None:
        if actual != self.scenario.expected:
            raise AssertionError(
                "lifecycle scenario outcome mismatch: "
                f"expected={self.scenario.expected.model_dump(mode='json')!r}, "
                f"actual={actual.model_dump(mode='json')!r}"
            )


_active_harness: contextvars.ContextVar[LifecycleFailureHarness | None] = contextvars.ContextVar(
    "local_agent_lifecycle_failure_harness", default=None
)


@contextlib.contextmanager
def lifecycle_failure_harness(
    harness: LifecycleFailureHarness,
) -> Iterator[LifecycleFailureHarness]:
    token = _active_harness.set(harness)
    try:
        yield harness
    finally:
        _active_harness.reset(token)


def reach_lifecycle_transition(
    point: LifecycleTransitionPoint,
    **facts: Any,
) -> tuple[LifecycleFaultInvocation, ...]:
    harness = _active_harness.get()
    return () if harness is None else harness.reach(point, **facts)


@dataclass(frozen=True)
class LifecycleFaultCase:
    name: str
    seed: int
    faults: tuple[LifecycleFault, ...]


def _all_catalog_faults() -> tuple[LifecycleFault, ...]:
    return tuple(
        LifecycleFault(at=point, action=action)
        for action in LifecycleFaultAction
        for point in LifecycleTransitionPoint
        if point in LIFECYCLE_FAULT_CATALOG[action].valid_at
    )


def generate_single_fault_cases(*, seed: int = 0) -> tuple[LifecycleFaultCase, ...]:
    return tuple(
        LifecycleFaultCase(
            name=(
                f"single-{fault.at.value.replace('_', '-')}-{fault.action.value.replace('_', '-')}"
            ),
            seed=seed + index,
            faults=(fault,),
        )
        for index, fault in enumerate(_all_catalog_faults())
    )


def _case_pairs(
    faults: Sequence[LifecycleFault],
) -> set[frozenset[LifecycleFault]]:
    return {
        frozenset((left, right))
        for left, right in itertools.combinations(faults, 2)
        if lifecycle_faults_can_coexist(left, right)
    }


def generate_pairwise_fault_cases(
    *,
    seed: int = 0,
    faults_per_case: int = 4,
) -> tuple[LifecycleFaultCase, ...]:
    """Cover every coexistent fault pair without generating the Cartesian product."""

    if faults_per_case < 2:
        raise ValueError("faults_per_case must be at least two")
    catalog_faults = _all_catalog_faults()
    uncovered = _case_pairs(catalog_faults)
    randomizer = random.Random(seed)
    cases: list[LifecycleFaultCase] = []
    while uncovered:
        anchor = min(
            uncovered,
            key=lambda pair: tuple(
                sorted(f"{fault.at.value}:{fault.action.value}" for fault in pair)
            ),
        )
        selected = list(anchor)
        candidates = list(catalog_faults)
        randomizer.shuffle(candidates)
        while len(selected) < faults_per_case:
            compatible = [
                candidate
                for candidate in candidates
                if candidate not in selected
                and all(lifecycle_faults_can_coexist(candidate, item) for item in selected)
            ]
            if not compatible:
                break
            candidate = max(
                compatible,
                key=lambda item: (
                    sum(frozenset((item, existing)) in uncovered for existing in selected),
                    item.at.value,
                    item.action.value,
                ),
            )
            selected.append(candidate)
            candidates.remove(candidate)
        selected.sort(key=lambda item: (item.at.value, item.action.value, item.occurrence))
        covered = _case_pairs(selected).intersection(uncovered)
        if not covered:
            raise AssertionError("pairwise generator failed to reduce its uncovered set")
        uncovered.difference_update(covered)
        index = len(cases)
        cases.append(
            LifecycleFaultCase(
                name=f"pairwise-{index:04d}",
                seed=seed + index,
                faults=tuple(selected),
            )
        )
    return tuple(cases)


CRITICAL_LIFECYCLE_FAULT_CASES = (
    LifecycleFaultCase(
        name="checkpoint-commit-crash-before-persistence",
        seed=101,
        faults=(
            LifecycleFault(
                at=LifecycleTransitionPoint.AFTER_CHECKPOINT_GIT_COMMIT,
                action=LifecycleFaultAction.DROP_DATABASE_CONNECTION,
            ),
            LifecycleFault(
                at=LifecycleTransitionPoint.BEFORE_CHECKPOINT_PERSISTED,
                action=LifecycleFaultAction.TERMINATE_RUNTIME,
            ),
        ),
    ),
    LifecycleFaultCase(
        name="approval-branch-drift-before-merge",
        seed=102,
        faults=(
            LifecycleFault(
                at=LifecycleTransitionPoint.AFTER_MERGE_APPROVAL_RESOLVED,
                action=LifecycleFaultAction.ADVANCE_TARGET_BRANCH,
            ),
        ),
    ),
    LifecycleFaultCase(
        name="approval-revoked-before-merge",
        seed=105,
        faults=(
            LifecycleFault(
                at=LifecycleTransitionPoint.AFTER_MERGE_APPROVAL_RESOLVED,
                action=LifecycleFaultAction.REVOKE_APPROVAL,
            ),
        ),
    ),
    LifecycleFaultCase(
        name="merge-crash-before-milestone-completion",
        seed=103,
        faults=(
            LifecycleFault(
                at=LifecycleTransitionPoint.AFTER_GIT_MERGE,
                action=LifecycleFaultAction.TERMINATE_RUNTIME,
            ),
        ),
    ),
    LifecycleFaultCase(
        name="remote-preview-local-persistence-ambiguity",
        seed=104,
        faults=(
            LifecycleFault(
                at=LifecycleTransitionPoint.AFTER_REMOTE_PREVIEW_CREATED,
                action=LifecycleFaultAction.REMOTE_SUCCESS_THEN_PERSISTENCE_FAILURE,
            ),
        ),
    ),
)

VALID_LIFECYCLE_TRANSITIONS: Mapping[
    LifecycleTransitionPoint, tuple[LifecycleTransitionPoint, ...]
] = MappingProxyType(
    {
        LifecycleTransitionPoint.AFTER_MILESTONE_SELECTED: (
            LifecycleTransitionPoint.AFTER_INTENT_CREATED,
        ),
        LifecycleTransitionPoint.AFTER_INTENT_CREATED: (
            LifecycleTransitionPoint.AFTER_INTENT_CLAIMED,
        ),
        LifecycleTransitionPoint.AFTER_INTENT_CLAIMED: (
            LifecycleTransitionPoint.AFTER_LEASE_STARTED,
        ),
        LifecycleTransitionPoint.AFTER_LEASE_STARTED: (
            LifecycleTransitionPoint.DURING_AGENT_STREAM,
        ),
        LifecycleTransitionPoint.DURING_AGENT_STREAM: (
            LifecycleTransitionPoint.DURING_AGENT_STREAM,
            LifecycleTransitionPoint.AFTER_CHECKPOINT_GIT_COMMIT,
        ),
        LifecycleTransitionPoint.AFTER_CHECKPOINT_GIT_COMMIT: (
            LifecycleTransitionPoint.BEFORE_CHECKPOINT_PERSISTED,
            LifecycleTransitionPoint.AFTER_VERIFICATION_RECORDED,
        ),
        LifecycleTransitionPoint.BEFORE_CHECKPOINT_PERSISTED: (
            LifecycleTransitionPoint.AFTER_VERIFICATION_RECORDED,
        ),
        LifecycleTransitionPoint.AFTER_VERIFICATION_RECORDED: (
            LifecycleTransitionPoint.AFTER_REVIEW_BLOCK_RECORDED,
            LifecycleTransitionPoint.AFTER_MERGE_APPROVAL_RESOLVED,
        ),
        LifecycleTransitionPoint.AFTER_REVIEW_BLOCK_RECORDED: (
            LifecycleTransitionPoint.AFTER_REVISION_STARTED,
        ),
        LifecycleTransitionPoint.AFTER_REVISION_STARTED: (
            LifecycleTransitionPoint.DURING_AGENT_STREAM,
        ),
        LifecycleTransitionPoint.AFTER_MERGE_APPROVAL_RESOLVED: (
            LifecycleTransitionPoint.AFTER_GIT_MERGE,
        ),
        LifecycleTransitionPoint.AFTER_GIT_MERGE: (
            LifecycleTransitionPoint.BEFORE_MILESTONE_COMPLETED,
        ),
        LifecycleTransitionPoint.BEFORE_MILESTONE_COMPLETED: (
            LifecycleTransitionPoint.AFTER_REMOTE_PREVIEW_CREATED,
        ),
        LifecycleTransitionPoint.AFTER_REMOTE_PREVIEW_CREATED: (
            LifecycleTransitionPoint.BEFORE_DEPLOYMENT_EVIDENCE_PERSISTED,
        ),
        LifecycleTransitionPoint.BEFORE_DEPLOYMENT_EVIDENCE_PERSISTED: (),
    }
)


LIFECYCLE_FAULT_CATALOG_DOCUMENT = LifecycleFaultCatalogDocument(
    fault_actions=tuple(
        LifecycleFaultCatalogEntry(
            action=action,
            valid_at=tuple(
                point
                for point in LifecycleTransitionPoint
                if point in LIFECYCLE_FAULT_CATALOG[action].valid_at
            ),
            exclusive_group=LIFECYCLE_FAULT_CATALOG[action].exclusive_group,
        )
        for action in LifecycleFaultAction
    ),
    transitions=tuple(
        LifecycleTransitionRule(
            point=point,
            successors=VALID_LIFECYCLE_TRANSITIONS[point],
        )
        for point in LifecycleTransitionPoint
    ),
)


def generate_seeded_state_machine_fault_cases(
    *,
    seed: int,
    case_count: int,
    max_steps: int = 40,
    max_faults: int = 6,
) -> tuple[LifecycleFaultCase, ...]:
    if case_count < 1:
        raise ValueError("case_count must be positive")
    if max_steps < 1:
        raise ValueError("max_steps must be positive")
    if max_faults < 1:
        raise ValueError("max_faults must be positive")
    randomizer = random.Random(seed)
    cases: list[LifecycleFaultCase] = []
    fingerprints: set[tuple[LifecycleFault, ...]] = set()
    attempts = 0
    while len(cases) < case_count:
        attempts += 1
        if attempts > case_count * 100:
            raise ValueError("requested more unique seeded cases than the bounds can produce")
        point = LifecycleTransitionPoint.AFTER_MILESTONE_SELECTED
        occurrences: Counter[LifecycleTransitionPoint] = Counter()
        selected: list[LifecycleFault] = []
        for _ in range(max_steps):
            occurrences[point] += 1
            actions = [
                action for action, rule in LIFECYCLE_FAULT_CATALOG.items() if point in rule.valid_at
            ]
            if actions and len(selected) < max_faults and randomizer.random() < 0.45:
                candidate = LifecycleFault(
                    at=point,
                    action=randomizer.choice(actions),
                    occurrence=occurrences[point],
                )
                if all(lifecycle_faults_can_coexist(candidate, fault) for fault in selected):
                    selected.append(candidate)
            successors = VALID_LIFECYCLE_TRANSITIONS[point]
            if not successors:
                break
            point = randomizer.choice(successors)
        if not selected:
            continue
        fingerprint = tuple(selected)
        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        index = len(cases)
        cases.append(
            LifecycleFaultCase(
                name=f"seeded-state-machine-{seed}-{index:03d}",
                seed=randomizer.getrandbits(63),
                faults=fingerprint,
            )
        )
    return tuple(cases)


def scenario_from_fault_case(
    case: LifecycleFaultCase,
    *,
    expected: LifecycleScenarioExpected,
    restart: bool,
    project_fixture: LifecycleProjectFixture = (LifecycleProjectFixture.DISPOSABLE_GIT_REPO),
    frontier_fixture: LifecycleFrontierFixture = (
        LifecycleFrontierFixture.FAKE_CLAUDE_THEN_FAKE_CODEX
    ),
) -> LifecycleFailureScenario:
    return LifecycleFailureScenario(
        name=case.name,
        seed=case.seed,
        project_fixture=project_fixture,
        frontier_fixture=frontier_fixture,
        faults=case.faults,
        restart=restart,
        expected=expected,
    )


__all__ = [
    "CRITICAL_LIFECYCLE_FAULT_CASES",
    "IMPLEMENTED_LIFECYCLE_TRANSITION_POINTS",
    "LIFECYCLE_FAILURE_SCENARIO_SCHEMA_VERSION",
    "LIFECYCLE_FAULT_CATALOG",
    "LIFECYCLE_FAULT_CATALOG_DOCUMENT",
    "LIFECYCLE_FAULT_CATALOG_SCHEMA_VERSION",
    "VALID_LIFECYCLE_TRANSITIONS",
    "LifecycleFailureHarness",
    "LifecycleFailureScenario",
    "LifecycleEvidenceKind",
    "LifecycleFault",
    "LifecycleFaultAction",
    "LifecycleFaultCatalogDocument",
    "LifecycleFaultCatalogEntry",
    "LifecycleFaultCase",
    "LifecycleFaultHandler",
    "LifecycleFaultInvocation",
    "LifecycleFrontierFixture",
    "LifecycleInvariantFacts",
    "LifecycleInvariantViolation",
    "LifecycleProjectFixture",
    "LifecycleScenarioExpected",
    "LifecycleTransitionObservation",
    "LifecycleTransitionPoint",
    "LifecycleTransitionRule",
    "LifecycleVerificationStatus",
    "ModelUsageLimitDisposition",
    "assert_lifecycle_invariants",
    "evaluate_lifecycle_invariants",
    "generate_pairwise_fault_cases",
    "generate_seeded_state_machine_fault_cases",
    "generate_single_fault_cases",
    "lifecycle_failure_harness",
    "lifecycle_faults_can_coexist",
    "reach_lifecycle_transition",
    "scenario_from_fault_case",
]
