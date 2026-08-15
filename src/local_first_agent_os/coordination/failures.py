# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Durable failure records shared by command results and telemetry.

``failure.v1`` is deliberately a data contract, not a subclass for every known
failure.  Existing outcome enums remain the source of truth for execution
failures; expected coordination rejections may keep their established stable
codes (for example ``invalid_base_sha``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

from .outcomes import (
    FailureCategory,
    InfrastructureFailure,
    TerminalOutcome,
    classify_failure,
    failure_category,
)

FAILURE_SCHEMA_VERSION = "failure.v1"

_RETRYABLE_INFRASTRUCTURE_FAILURES = frozenset(
    {
        InfrastructureFailure.USAGE_LIMIT.value,
        InfrastructureFailure.DEADLINE_EXCEEDED.value,
        InfrastructureFailure.ORPHANED_LEASE_EXPIRED.value,
        InfrastructureFailure.SUPERVISOR_FAILED.value,
        InfrastructureFailure.PROCESS_FAILED.value,
        InfrastructureFailure.ARTIFACT_WRITE_FAILED.value,
        InfrastructureFailure.EVENT_WRITE_FAILED.value,
        InfrastructureFailure.CHECKPOINT_WRITE_FAILED.value,
    }
)


@dataclass(frozen=True, slots=True)
class FailureV1:
    """Stable, serializable failure dimensions used by ledger projections."""

    error_code: str
    category: FailureCategory
    retryable: bool
    operation: str
    message: str = ""
    terminal_outcome: str = ""
    exception_type: str = ""
    schema_version: str = FAILURE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["category"] = self.category.value
        return payload

    def observability_fields(self) -> dict[str, str | bool]:
        """Return flat, low-cardinality fields suitable for logs and spans."""

        return {
            "error_code": self.error_code,
            "category": self.category.value,
            "retryable": self.retryable,
            "operation": self.operation,
        }


class DurableFailureError(RuntimeError):
    """One generic exception carrier for code that cannot return a typed result."""

    def __init__(self, failure: FailureV1):
        self.failure = failure
        super().__init__(failure.message or failure.error_code)


def expected_failure(
    error_code: str,
    *,
    operation: str,
    message: str = "",
    category: FailureCategory | None = None,
    retryable: bool | None = None,
) -> FailureV1:
    """Describe an expected, non-exceptional command rejection."""

    normalized_code = _require_error_code(error_code)
    inferred_category = category or failure_category(normalized_code)
    resolved_category = inferred_category or FailureCategory.BUSINESS
    resolved_retryable = (
        _retryable(normalized_code, resolved_category) if retryable is None else retryable
    )
    return FailureV1(
        error_code=normalized_code,
        category=resolved_category,
        retryable=resolved_retryable,
        operation=operation,
        message=message,
        terminal_outcome=(
            normalized_code if normalized_code in TerminalOutcome._value2member_map_ else ""
        ),
    )


def exceptional_failure(error: BaseException, *, operation: str) -> FailureV1:
    """Normalize an exception without trusting its free-form message as a code."""

    if isinstance(error, DurableFailureError):
        failure = error.failure
        if failure.operation or not operation:
            return failure
        return replace(failure, operation=operation)

    if isinstance(error, TimeoutError):
        outcome = TerminalOutcome.DEADLINE_EXCEEDED
    elif isinstance(error, AssertionError):
        outcome = TerminalOutcome.INTERNAL_ASSERTION
    else:
        evidence = f"{type(error).__name__}: {error}"
        outcome = classify_failure(evidence)
    category = failure_category(outcome) or FailureCategory.INFRASTRUCTURE
    return FailureV1(
        error_code=outcome.value,
        category=category,
        retryable=_retryable(outcome.value, category),
        operation=operation,
        message=str(error),
        terminal_outcome=outcome.value,
        exception_type=type(error).__name__,
    )


def _require_error_code(value: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError("failure error_code must not be empty")
    return normalized


def _retryable(error_code: str, category: FailureCategory) -> bool:
    if category is FailureCategory.BUSINESS:
        return False
    return error_code in _RETRYABLE_INFRASTRUCTURE_FAILURES


__all__ = [
    "FAILURE_SCHEMA_VERSION",
    "DurableFailureError",
    "FailureV1",
    "exceptional_failure",
    "expected_failure",
]
