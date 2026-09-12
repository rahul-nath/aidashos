# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Typed normalization for automated and manually recovered dispatch results."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Never, assert_never, cast

from pydantic import JsonValue, ValidationError

from .coordination.outcomes import (
    DispatchPromotionState,
    DispatchResultOrigin,
    DispatchResultState,
)
from .dispatch_contracts import (
    DispatchContractCode,
    DispatchContractViolation,
    InvalidDispatchReport,
)
from .dispatch_payloads import (
    RUN_OBSERVATION,
    AutomatedRunObservation,
    DispatchReportEnvelope,
    InterruptedRunObservation,
    ManualRecoveryEvidence,
    ManualRunObservation,
    ValidatedRunObservation,
)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class DispatchEvidenceSubject:
    """The ledger-owned subject against which a result is being consumed."""

    intent_id: str
    target_project_id: str

    def __post_init__(self) -> None:
        if not self.intent_id.strip() or not self.target_project_id.strip():
            raise ValueError("dispatch evidence requires an intent and target project")


@dataclass(frozen=True)
class UnscopedDispatchFailureSubject:
    """A ledger intent that failed before acquiring any project scope.

    This variant permits recording only runner-crash diagnostics. It cannot
    qualify execution success, project artifacts, verification, or integration.
    """

    intent_id: str

    def __post_init__(self) -> None:
        if not self.intent_id.strip():
            raise ValueError("unscoped dispatch failure requires its ledger intent")


type DispatchReportSubject = DispatchEvidenceSubject | UnscopedDispatchFailureSubject


@dataclass(frozen=True)
class DispatchRunnerResult:
    """Canonical dispatch evidence consumed by review and promotion code."""

    origin: DispatchResultOrigin
    state: DispatchResultState
    promotion_state: DispatchPromotionState
    observation: ValidatedRunObservation | None

    def __post_init__(self) -> None:
        if not (
            isinstance(self.origin, DispatchResultOrigin)
            and isinstance(self.state, DispatchResultState)
            and isinstance(self.promotion_state, DispatchPromotionState)
        ):
            raise TypeError("normalized dispatch states must use their declared enum types")
        if self.observation is not None and not isinstance(
            self.observation,
            AutomatedRunObservation | ManualRunObservation | InterruptedRunObservation,
        ):
            raise TypeError("raw report mappings cannot enter a normalized dispatch result")
        _validate_origin_state(self.origin, self.state)
        _validate_state_promotion(self.state, self.promotion_state)
        if self.state is DispatchResultState.UNAVAILABLE:
            if self.observation is not None:
                raise ValueError("unavailable result cannot contain an execution observation")
        elif self.observation is None:
            raise ValueError("available result requires a validated execution observation")
        else:
            _validate_nested_state(self.origin, self.state, self.observation)

    @property
    def run_result(self) -> Mapping[str, JsonValue]:
        """Compatibility projection after validation, not an ingress for raw mappings."""
        if self.observation is None:
            return {}
        return cast(
            dict[str, JsonValue],
            self.observation.model_dump(mode="json", exclude_none=True),
        )

    @classmethod
    def from_run_payload(
        cls,
        origin: DispatchResultOrigin,
        state: DispatchResultState,
        promotion_state: DispatchPromotionState,
        payload: Mapping[str, object],
    ) -> DispatchRunnerResult:
        return cls(origin, state, promotion_state, _decode_run(payload))

    @classmethod
    def unavailable(cls) -> DispatchRunnerResult:
        return cls(
            origin=DispatchResultOrigin.UNKNOWN,
            state=DispatchResultState.UNAVAILABLE,
            promotion_state=DispatchPromotionState.RESULT_RECORDED,
            observation=None,
        )


@dataclass(frozen=True, slots=True)
class AvailableDispatchReport:
    result: DispatchRunnerResult


@dataclass(frozen=True, slots=True)
class MissingDispatchReport:
    """No observation was supplied; this is not a corrupt observation."""


type DispatchReportIngress = AvailableDispatchReport | MissingDispatchReport | InvalidDispatchReport


def decode_dispatch_runner_result(
    *,
    intent_result: object,
    approval_payload: Mapping[str, object],
    expected_subject: DispatchReportSubject | None = None,
) -> DispatchReportIngress:
    """Decode ingress without giving malformed data an execution-state meaning.

    This operation is pure so inspection does not manufacture ledger writes.
    Mutation owners persist an invalid-report diagnostic before refusing it.
    """

    selected: object = intent_result
    try:
        if "dispatch_result" in approval_payload:
            selected = approval_payload["dispatch_result"]
            if not isinstance(selected, Mapping):
                return InvalidDispatchReport.from_input(
                    DispatchContractCode.INVALID_ENVELOPE, selected
                )
            _validate_subject(selected, expected_subject)
            result = _parse_typed_dispatch_result_payload(selected)
        elif approval_payload.get("manual_recovery") is True:
            selected = approval_payload
            _validate_subject(approval_payload, expected_subject)
            result = _parse_manual_recovery_dispatch_result(approval_payload)
        elif (
            "manual_recovery" in approval_payload
            and type(approval_payload["manual_recovery"]) is not bool
        ):
            return InvalidDispatchReport.from_input(
                DispatchContractCode.INVALID_ENVELOPE, approval_payload
            )
        elif intent_result is None or (
            isinstance(intent_result, str) and not intent_result.strip()
        ):
            return MissingDispatchReport()
        else:
            result = _build_automated_dispatch_result(intent_result, expected_subject)
        return AvailableDispatchReport(result)
    except DispatchContractViolation as exc:
        return InvalidDispatchReport.from_input(exc.diagnostic.code, selected)
    except json.JSONDecodeError:
        return InvalidDispatchReport.from_input(DispatchContractCode.MALFORMED_JSON, selected)
    except (ValidationError, TypeError, RecursionError):
        return InvalidDispatchReport.from_input(DispatchContractCode.INVALID_ENVELOPE, selected)
    except ValueError:
        return InvalidDispatchReport.from_input(DispatchContractCode.INCONSISTENT_OUTCOME, selected)


def normalize_dispatch_runner_result(
    *,
    intent_result: object,
    approval_payload: Mapping[str, object],
    expected_subject: DispatchReportSubject | None = None,
) -> DispatchRunnerResult:
    """Compatibility result API with an exhaustive, typed contract-failure branch."""

    ingress = decode_dispatch_runner_result(
        intent_result=intent_result,
        approval_payload=approval_payload,
        expected_subject=expected_subject,
    )
    match ingress:
        case AvailableDispatchReport(result):
            return result
        case MissingDispatchReport():
            return DispatchRunnerResult.unavailable()
        case InvalidDispatchReport():
            raise DispatchContractViolation(ingress)
        case _:
            assert_never(ingress)


def _parse_typed_dispatch_result_payload(payload: Mapping[str, object]) -> DispatchRunnerResult:
    report = DispatchReportEnvelope.model_validate_json(_serialize_report(payload))
    if (
        report.result_origin is None
        or report.result_state is None
        or report.promotion_state is None
    ):
        raise ValueError("embedded dispatch report requires explicit origin, state, and promotion")
    return DispatchRunnerResult(
        report.result_origin, report.result_state, report.promotion_state, report.run_result
    )


def _build_automated_dispatch_result(
    intent_result: object, expected_subject: DispatchReportSubject | None
) -> DispatchRunnerResult:
    if not isinstance(intent_result, str):
        raise TypeError("dispatch result must be serialized JSON")
    payload = json.loads(
        intent_result,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_non_json_number,
    )
    if not isinstance(payload, dict):
        raise TypeError("dispatch result must be a JSON object")
    result = payload
    if result.get("schema_version") != "dispatch_runner_result.v1":
        raise DispatchContractViolation(
            InvalidDispatchReport.from_input(DispatchContractCode.UNSUPPORTED_SCHEMA, intent_result)
        )
    _validate_subject(result, expected_subject)
    report = DispatchReportEnvelope.model_validate_json(intent_result)
    state = report.result_state or _automated_run_state(report.run_result)
    origin = report.result_origin or DispatchResultOrigin.AUTOMATED
    promotion = report.promotion_state or (
        DispatchPromotionState.MERGE_PENDING
        if state is DispatchResultState.COMPLETED
        and (report.merge_approval or report.run_result.changed_files)
        else DispatchPromotionState.RESULT_RECORDED
    )
    return DispatchRunnerResult(origin, state, promotion, report.run_result)


def _unique_json_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise TypeError("duplicate JSON member cannot select the authoritative value")
        result[key] = value
    return result


def _reject_non_json_number(value: str) -> Never:
    raise TypeError("nonfinite number is not a JSON value")


def _validate_subject(
    payload: Mapping[str, object], expected: DispatchReportSubject | None
) -> None:
    """Legacy evidence can be read without granting it a completion subject."""

    if expected is None:
        return
    if payload.get("intent_id") != expected.intent_id:
        raise DispatchContractViolation(
            InvalidDispatchReport.from_input(DispatchContractCode.SUBJECT_MISMATCH, payload)
        )
    nested = _mapping(payload.get("run_result"))
    match expected:
        case UnscopedDispatchFailureSubject():
            if (
                payload.get("target_project_id") is not None
                or nested.get("target_project_id") is not None
            ):
                raise DispatchContractViolation(
                    InvalidDispatchReport.from_input(DispatchContractCode.SUBJECT_MISMATCH, payload)
                )
            if (
                payload.get("result_origin") != DispatchResultOrigin.RUNNER_CRASH
                or payload.get("result_state") != DispatchResultState.FAILED
                or payload.get("promotion_state") != DispatchPromotionState.RESULT_RECORDED
            ):
                raise DispatchContractViolation(
                    InvalidDispatchReport.from_input(
                        DispatchContractCode.INCONSISTENT_OUTCOME, payload
                    )
                )
        case DispatchEvidenceSubject():
            if payload.get("target_project_id") != expected.target_project_id or (
                "target_project_id" in nested
                and nested["target_project_id"] != expected.target_project_id
            ):
                raise DispatchContractViolation(
                    InvalidDispatchReport.from_input(DispatchContractCode.SUBJECT_MISMATCH, payload)
                )
        case _:
            assert_never(expected)


def _decode_run(payload: Mapping[str, object]) -> ValidatedRunObservation:
    return RUN_OBSERVATION.validate_json(_serialize_report(payload))


def _serialize_report(payload: Mapping[str, object]) -> str:
    """Embedded reports obey the same JSON domain as serialized reports."""

    try:
        return json.dumps(payload, allow_nan=False)
    except (ValueError, RecursionError):
        raise TypeError("dispatch report contains values outside the JSON contract") from None


def _automated_run_state(
    observation: ValidatedRunObservation,
) -> DispatchResultState:
    match observation.status:
        case "COMPLETED":
            return DispatchResultState.COMPLETED
        case (
            "VERIFICATION_FAILED"
            | "BLOCKED"
            | "FAILED"
            | "DRY_RUN_COMPLETED"
            | "TIMED_OUT"
            | "CANCELED"
            | "UNAVAILABLE"
        ):
            return DispatchResultState.FAILED
        case "MANUAL_RECOVERY_REVIEWED":
            raise ValueError("dispatch result contains an invalid nested run status")
        case _ as unsupported:
            assert_never(unsupported)


def _validate_nested_state(
    origin: DispatchResultOrigin,
    state: DispatchResultState,
    observation: ValidatedRunObservation,
) -> None:
    if origin in {
        DispatchResultOrigin.AUTOMATED,
        DispatchResultOrigin.AUTOMATED_RECOVERY,
        DispatchResultOrigin.RUNNER_CRASH,
    }:
        if _automated_run_state(observation) is not state:
            raise ValueError("dispatch result outer and nested states disagree")
    elif origin is DispatchResultOrigin.MANUAL_RECOVERY and not isinstance(
        observation, ManualRunObservation
    ):
        raise ValueError("manual recovery requires its explicit result variant")


def _parse_manual_recovery_dispatch_result(
    payload: Mapping[str, object],
) -> DispatchRunnerResult:
    evidence = ManualRecoveryEvidence.model_validate_json(
        _serialize_report(
            {name: payload[name] for name in ManualRecoveryEvidence.model_fields if name in payload}
        )
    )
    staff_review = evidence.staff_review
    verdict = staff_review.verdict
    branch = evidence.branch.strip()
    base_sha = evidence.base_sha.strip()
    commit_sha = evidence.commit_sha.strip()
    if not branch or not base_sha or not commit_sha:
        raise ValueError("manual recovery dispatch result requires branch/base/commit")

    initial_verdict = staff_review.initial_verdict.strip()
    initial_finding = staff_review.initial_finding.strip()
    resolution = staff_review.resolution.strip()
    verdict_lines = [f"VERDICT: {verdict}"]
    if initial_verdict or initial_finding:
        verdict_lines.append(
            f"Initial {initial_verdict or 'review'} finding: {initial_finding}".strip()
        )
    if resolution:
        verdict_lines.append(f"Resolution: {resolution}")
    risks = list(staff_review.risks)
    changed_files = list(evidence.changed_files)
    run_result: dict[str, object] = {
        "status": "MANUAL_RECOVERY_REVIEWED",
        "output_summary": evidence.purpose,
        "target_project_id": evidence.target_project_id,
        "changed_files": changed_files,
        "verification_commands": [item.strip() for item in evidence.verification if item.strip()],
        "risks": risks or list(evidence.risks),
        "tasks": [
            {
                "task_name": "manual_recovery_operator_evidence",
                "role": "operator evidence reviewer; independent staff provenance unavailable",
                "status": verdict,
                "summary": resolution or initial_finding,
                "risks": risks,
                "artifacts": [
                    {
                        "artifact_type": "operator_recovery_review",
                        "content": {
                            "schema_version": "operator_recovery_review.v1",
                            "verdict": "\n".join(verdict_lines),
                            "review_origin": "OPERATOR_EVIDENCE",
                            "reviewer_tier": "OPERATOR",
                        },
                    }
                ],
            }
        ],
        "artifacts": [
            {
                "artifact_type": "worktree_commit_checkpoint",
                "content": {
                    "branch_name": branch,
                    "base_head_sha": base_sha,
                    "commit_sha": commit_sha,
                    "commit_created": True,
                    "changed_from_base": True,
                    "checkpointed_files": changed_files,
                },
            }
        ],
    }
    return DispatchRunnerResult(
        origin=DispatchResultOrigin.MANUAL_RECOVERY,
        state=DispatchResultState.REVIEWED,
        promotion_state=DispatchPromotionState.REVIEWED,
        observation=_decode_run(run_result),
    )


def _validate_origin_state(
    origin: DispatchResultOrigin,
    state: DispatchResultState,
) -> None:
    valid = {
        DispatchResultOrigin.AUTOMATED: {
            DispatchResultState.COMPLETED,
            DispatchResultState.FAILED,
        },
        DispatchResultOrigin.AUTOMATED_RECOVERY: {
            DispatchResultState.COMPLETED,
            DispatchResultState.FAILED,
        },
        DispatchResultOrigin.RUNNER_CRASH: {DispatchResultState.FAILED},
        DispatchResultOrigin.MANUAL_RECOVERY: {
            DispatchResultState.REVIEWED,
        },
        DispatchResultOrigin.UNKNOWN: {DispatchResultState.UNAVAILABLE},
    }
    if state not in valid[origin]:
        raise ValueError(f"invalid dispatch result origin/state: {origin}/{state}")


def _validate_state_promotion(
    state: DispatchResultState,
    promotion: DispatchPromotionState,
) -> None:
    valid = {
        DispatchResultState.COMPLETED: {
            DispatchPromotionState.RESULT_RECORDED,
            DispatchPromotionState.MERGE_PENDING,
        },
        DispatchResultState.FAILED: {DispatchPromotionState.RESULT_RECORDED},
        DispatchResultState.PAUSED: {DispatchPromotionState.RESULT_RECORDED},
        DispatchResultState.REVIEWED: {DispatchPromotionState.REVIEWED},
        DispatchResultState.UNAVAILABLE: {DispatchPromotionState.RESULT_RECORDED},
    }
    if promotion not in valid[state]:
        raise ValueError(f"invalid dispatch result state/promotion: {state}/{promotion}")


__all__ = [
    "AvailableDispatchReport",
    "DispatchEvidenceSubject",
    "DispatchReportIngress",
    "DispatchReportSubject",
    "DispatchRunnerResult",
    "MissingDispatchReport",
    "UnscopedDispatchFailureSubject",
    "decode_dispatch_runner_result",
    "normalize_dispatch_runner_result",
]
