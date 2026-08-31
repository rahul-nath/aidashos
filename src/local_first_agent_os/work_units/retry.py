# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Typed retry policy and charged-failure accounting for milestone execution."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, assert_never

from .lifecycle import FailureClass, LifecyclePhase, MilestoneExecutionStatus


class RetryGrounds(StrEnum):
    """Why a retry is permitted."""

    WITHIN_BUDGET = "WITHIN_BUDGET"
    NO_ATTEMPT_SPENT = "NO_ATTEMPT_SPENT"
    OPERATOR_OVERRIDE = "OPERATOR_OVERRIDE"


class RetryPolicyKind(StrEnum):
    CHARGED_FAILURE_BUDGET = "charged_failure_budget"
    OPERATOR_ONLY = "operator_only"


@dataclass(frozen=True)
class ChargedFailureBudget:
    """Permit retries until this many judged failures have accumulated."""

    max_charged_failures: int

    def __post_init__(self) -> None:
        if self.max_charged_failures < 1:
            raise ValueError("max_charged_failures must be positive")

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": RetryPolicyKind.CHARGED_FAILURE_BUDGET.value,
            "max_charged_failures": self.max_charged_failures,
        }


@dataclass(frozen=True)
class OperatorOnly:
    """Require an operator override after any judged failure."""

    def to_payload(self) -> dict[str, object]:
        return {"kind": RetryPolicyKind.OPERATOR_ONLY.value}


type RetryPolicy = ChargedFailureBudget | OperatorOnly


def retry_policy_from_payload(payload: Mapping[str, Any]) -> RetryPolicy:
    kind = RetryPolicyKind(str(payload["kind"]))
    match kind:
        case RetryPolicyKind.CHARGED_FAILURE_BUDGET:
            return ChargedFailureBudget(int(payload["max_charged_failures"]))
        case RetryPolicyKind.OPERATOR_ONLY:
            return OperatorOnly()
    assert_never(kind)


def retry_policy_from_legacy_max_attempts(max_attempts: int) -> RetryPolicy:
    """Interpret a v3/v4 plan without changing its historical serialization."""

    if max_attempts < 1:
        raise ValueError("legacy max_attempts must be positive")
    if max_attempts == 1:
        return OperatorOnly()
    return ChargedFailureBudget(max_attempts)


def legacy_max_attempts(policy: RetryPolicy) -> int:
    match policy:
        case ChargedFailureBudget(max_charged_failures=limit):
            return limit
        case OperatorOnly():
            return 1
    assert_never(policy)


@dataclass(frozen=True)
class ChargedFailure:
    failure_class: FailureClass


@dataclass(frozen=True)
class UnchargedFailure:
    failure_class: FailureClass | None


type AttemptCharge = ChargedFailure | UnchargedFailure


def attempt_charge(failure_class: FailureClass | None) -> AttemptCharge:
    """Whether the milestone's own work was judged and found wanting.

    Three of the uncharged classes never reach a retry decision at all:
    `WorkUnitEngine._status_for_failure` routes REQUIRES_OPERATOR to
    WAITING_FOR_OPERATOR and POLICY_VIOLATION / NONRECOVERABLE to FAILED, while
    `decide_retry` asserts BLOCKED. They are answered here to keep the match
    exhaustive over the enum, not because an uncharged failure is retried freely.

    TRANSIENT does reach BLOCKED and is still uncharged, because the request died
    in flight and the work was never judged. Its bound does not live in this
    budget: the only unattended actor that re-drives a transient-blocked
    milestone is `auto_resume.sweep_transient_blocked`, which counts the
    milestone's recorded transient failures and stops at its own cap, so a
    provider that keeps dropping ends up in front of an operator rather than in
    a loop.
    """

    match failure_class:
        case FailureClass.CORRECTABLE | FailureClass.REQUIRES_REPLAN:
            return ChargedFailure(failure_class)
        case (
            None
            | FailureClass.TRANSIENT
            | FailureClass.REQUIRES_OPERATOR
            | FailureClass.POLICY_VIOLATION
            | FailureClass.NONRECOVERABLE
        ):
            return UnchargedFailure(failure_class)
    assert_never(failure_class)


def count_charged_failures(failure_classes: Iterable[FailureClass | None]) -> int:
    return sum(isinstance(attempt_charge(item), ChargedFailure) for item in failure_classes)


def retry_policy_exhausted(policy: RetryPolicy, *, charged_failures: int) -> bool:
    if charged_failures < 0:
        raise ValueError("charged_failures cannot be negative")
    match policy:
        case ChargedFailureBudget(max_charged_failures=limit):
            return charged_failures >= limit
        case OperatorOnly():
            return charged_failures > 0
    assert_never(policy)


@dataclass(frozen=True)
class RetryPermitted:
    """This milestone may go back to READY on a fresh execution ordinal."""

    milestone_key: str
    phase: LifecyclePhase
    next_execution_ordinal: int
    grounds: RetryGrounds


@dataclass(frozen=True)
class RetryRefused:
    """This milestone needs an operator override before another execution."""

    milestone_key: str
    phase: LifecyclePhase
    execution_ordinal: int
    charged_failures: int
    retry_policy: RetryPolicy

    def describe(self) -> str:
        match self.retry_policy:
            case ChargedFailureBudget(max_charged_failures=limit):
                reason = f"exhausted its {limit} permitted charged failure(s)"
            case OperatorOnly():
                reason = "uses an operator-only retry policy after a charged failure"
            case unreachable:
                assert_never(unreachable)
        return (
            f"milestone {self.milestone_key} {reason}; a new plan revision that "
            "supersedes this one or an explicit operator override is required"
        )


type RetryDecision = RetryPermitted | RetryRefused


def decide_retry(
    *,
    milestone_key: str,
    phase: LifecyclePhase,
    status: MilestoneExecutionStatus,
    execution_ordinal: int,
    charged_failures: int,
    failure_class: FailureClass | None,
    retry_policy: RetryPolicy,
    operator_override: bool = False,
) -> RetryDecision:
    """Decide retry from independent execution identity and policy accounting."""

    assert status is MilestoneExecutionStatus.BLOCKED, (
        f"decide_retry is asked about a blocked milestone, not a {status.value} one"
    )
    if execution_ordinal < 1:
        raise ValueError("execution_ordinal must be positive")
    if operator_override:
        return RetryPermitted(
            milestone_key=milestone_key,
            phase=phase,
            next_execution_ordinal=execution_ordinal + 1,
            grounds=RetryGrounds.OPERATOR_OVERRIDE,
        )
    if isinstance(attempt_charge(failure_class), UnchargedFailure):
        return RetryPermitted(
            milestone_key=milestone_key,
            phase=phase,
            next_execution_ordinal=execution_ordinal + 1,
            grounds=RetryGrounds.NO_ATTEMPT_SPENT,
        )
    if retry_policy_exhausted(retry_policy, charged_failures=charged_failures):
        return RetryRefused(
            milestone_key=milestone_key,
            phase=phase,
            execution_ordinal=execution_ordinal,
            charged_failures=charged_failures,
            retry_policy=retry_policy,
        )
    return RetryPermitted(
        milestone_key=milestone_key,
        phase=phase,
        next_execution_ordinal=execution_ordinal + 1,
        grounds=RetryGrounds.WITHIN_BUDGET,
    )


ATTEMPT_BUDGET_EXHAUSTED = "attempt_budget_exhausted"


__all__ = [
    "ATTEMPT_BUDGET_EXHAUSTED",
    "AttemptCharge",
    "ChargedFailure",
    "ChargedFailureBudget",
    "OperatorOnly",
    "RetryDecision",
    "RetryGrounds",
    "RetryPermitted",
    "RetryPolicy",
    "RetryPolicyKind",
    "RetryRefused",
    "UnchargedFailure",
    "attempt_charge",
    "count_charged_failures",
    "decide_retry",
    "legacy_max_attempts",
    "retry_policy_exhausted",
    "retry_policy_from_legacy_max_attempts",
    "retry_policy_from_payload",
]
