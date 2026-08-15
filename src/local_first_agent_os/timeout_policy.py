# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Typed timeout inference for blocking control-plane operations.

Timeouts are failure detectors.  They are inferred from the kind of operation,
not copied from the longest model budget or guessed from the happy-path time.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum

from .constants import (
    DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
    DEFAULT_ARTIFACT_WRITE_TIMEOUT_SECONDS,
    DEFAULT_COORDINATION_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS,
    DEFAULT_PROGRESS_ASSESSMENT_TIMEOUT_SECONDS,
    DEFAULT_STREAM_DRAIN_TIMEOUT_SECONDS,
)


class OperationKind(StrEnum):
    COORDINATION = "coordination"
    GIT = "git"
    HTTP_HEALTH = "http_health"
    HTTP_APPLICATION = "http_application"
    ARTIFACT_WRITE = "artifact_write"
    STREAM_DRAIN = "stream_drain"
    PROGRESS_ASSESSMENT = "progress_assessment"
    FRONTIER_MODEL = "frontier_model"


@dataclass(frozen=True)
class TimeoutBudget:
    operation: OperationKind
    timeout_seconds: float
    grace_seconds: float
    retry_attempts: int
    retryable: bool
    rationale: str

    def to_payload(self) -> dict[str, object]:
        return asdict(self)


_BASE: dict[OperationKind, TimeoutBudget] = {
    OperationKind.COORDINATION: TimeoutBudget(
        OperationKind.COORDINATION,
        DEFAULT_COORDINATION_COMMAND_TIMEOUT_SECONDS,
        2,
        3,
        True,
        "one typed ledger transition; never a model turn",
    ),
    OperationKind.GIT: TimeoutBudget(
        OperationKind.GIT,
        DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS,
        2,
        1,
        False,
        "normally sub-second; timeout detects locks or filesystem failure",
    ),
    OperationKind.HTTP_HEALTH: TimeoutBudget(
        OperationKind.HTTP_HEALTH,
        10,
        1,
        3,
        True,
        "small idempotent readiness probe",
    ),
    OperationKind.HTTP_APPLICATION: TimeoutBudget(
        OperationKind.HTTP_APPLICATION,
        120,
        5,
        2,
        True,
        "bounded application request; caller may supply expected duration",
    ),
    OperationKind.ARTIFACT_WRITE: TimeoutBudget(
        OperationKind.ARTIFACT_WRITE,
        DEFAULT_ARTIFACT_WRITE_TIMEOUT_SECONDS,
        2,
        3,
        True,
        "local fsync plus database insert or optional object-store upload",
    ),
    OperationKind.STREAM_DRAIN: TimeoutBudget(
        OperationKind.STREAM_DRAIN,
        DEFAULT_STREAM_DRAIN_TIMEOUT_SECONDS,
        1,
        1,
        False,
        "terminal process pipes should reach EOF promptly",
    ),
    OperationKind.PROGRESS_ASSESSMENT: TimeoutBudget(
        OperationKind.PROGRESS_ASSESSMENT,
        DEFAULT_PROGRESS_ASSESSMENT_TIMEOUT_SECONDS,
        5,
        1,
        False,
        "on-demand advisory junior model; failure cannot own the process",
    ),
    OperationKind.FRONTIER_MODEL: TimeoutBudget(
        OperationKind.FRONTIER_MODEL,
        DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
        30,
        1,
        False,
        "long tool-using implementation or review process",
    ),
}


def infer_timeout_budget(
    operation: OperationKind | str,
    *,
    expected_seconds: float | None = None,
) -> TimeoutBudget:
    """Return a typed budget, widening application/model work when justified.

    Fast infrastructure operations retain their policy ceiling even when a
    caller supplies an implausibly large expectation.  Only application HTTP
    and model work infer from expected duration.
    """

    kind = OperationKind(operation)
    base = _BASE[kind]
    if expected_seconds is None:
        return base
    if expected_seconds <= 0:
        raise ValueError("expected_seconds must be positive")
    if kind not in {
        OperationKind.HTTP_APPLICATION,
        OperationKind.PROGRESS_ASSESSMENT,
        OperationKind.FRONTIER_MODEL,
    }:
        return base
    ceiling = {
        OperationKind.HTTP_APPLICATION: 900.0,
        OperationKind.PROGRESS_ASSESSMENT: 600.0,
        OperationKind.FRONTIER_MODEL: 14_400.0,
    }[kind]
    inferred = min(ceiling, max(base.timeout_seconds, expected_seconds * 2.0))
    return TimeoutBudget(
        operation=kind,
        timeout_seconds=inferred,
        grace_seconds=base.grace_seconds,
        retry_attempts=base.retry_attempts,
        retryable=base.retryable,
        rationale=f"{base.rationale}; inferred from expected_seconds={expected_seconds:g}",
    )


def timeout_policy_payload() -> dict[str, object]:
    return {
        "schema_version": "timeout_policy.v1",
        "budgets": [budget.to_payload() for budget in _BASE.values()],
    }
