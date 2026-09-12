# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from local_first_agent_os.coordination.dispatch_diagnostics import (
    DISPATCH_CONTRACT_VIOLATION_EVENT,
    DispatchDiagnosticPersistenceError,
    record_dispatch_contract_violation,
)
from local_first_agent_os.coordination.outcomes import DispatchPromotionState, DispatchResultState
from local_first_agent_os.coordination.store import tx
from local_first_agent_os.dispatch_contracts import (
    DispatchContractCode,
    DispatchContractViolation,
    InvalidDispatchReport,
)
from local_first_agent_os.dispatch_results import (
    AvailableDispatchReport,
    MissingDispatchReport,
    UnscopedDispatchFailureSubject,
    decode_dispatch_runner_result,
    normalize_dispatch_runner_result,
)
from local_first_agent_os.pow_wow import run_coordination_command


@pytest.mark.parametrize("explicit_null", [False, True])
def test_unscoped_crash_can_record_its_own_failure(explicit_null: bool) -> None:
    payload = {
        "schema_version": "dispatch_runner_result.v1",
        "intent_id": "failed-before-project",
        "result_origin": "runner_crash",
        "result_state": "FAILED",
        "promotion_state": "RESULT_RECORDED",
        "run_result": {
            "status": "FAILED",
            **({"target_project_id": None} if explicit_null else {}),
        },
        **({"target_project_id": None} if explicit_null else {}),
    }
    decoded = decode_dispatch_runner_result(
        intent_result=json.dumps(payload),
        approval_payload={},
        expected_subject=UnscopedDispatchFailureSubject("failed-before-project"),
    )
    assert isinstance(decoded, AvailableDispatchReport)
    assert decoded.result.state is DispatchResultState.FAILED


@pytest.mark.parametrize(
    "change",
    [
        {"intent_id": "another-intent"},
        {"target_project_id": "another-project"},
        {"target_project_id": ""},
        {"run_result": {"status": "FAILED", "target_project_id": "another-project"}},
        {"result_origin": "AUTOMATED"},
        {"result_state": "COMPLETED", "run_result": {"status": "COMPLETED"}},
        {"promotion_state": "MERGE_PENDING"},
        {"result_state": None},
    ],
)
def test_unscoped_failure_subject_cannot_admit_other_evidence(change: dict[str, object]) -> None:
    payload = {
        "schema_version": "dispatch_runner_result.v1",
        "intent_id": "failed-before-project",
        "result_origin": "runner_crash",
        "result_state": "FAILED",
        "promotion_state": "RESULT_RECORDED",
        "run_result": {"status": "FAILED"},
        **change,
    }
    decoded = decode_dispatch_runner_result(
        intent_result=json.dumps(payload),
        approval_payload={},
        expected_subject=UnscopedDispatchFailureSubject("failed-before-project"),
    )
    assert isinstance(decoded, InvalidDispatchReport)


def test_runner_crash_is_a_declared_failure_origin() -> None:
    result = normalize_dispatch_runner_result(
        intent_result=json.dumps(
            {
                "schema_version": "dispatch_runner_result.v1",
                "result_origin": "runner_crash",
                "result_state": "FAILED",
                "promotion_state": "RESULT_RECORDED",
                "run_result": {"status": "FAILED"},
            }
        ),
        approval_payload={},
    )
    assert result.origin.value == "runner_crash"
    assert result.state is DispatchResultState.FAILED
    assert result.promotion_state is DispatchPromotionState.RESULT_RECORDED


@pytest.mark.parametrize(
    "fields",
    [
        {"result_state": "COMPLETED", "run_result": {"status": "COMPLETED"}},
        {"promotion_state": "MERGE_PENDING"},
        {"run_result": {"status": "COMPLETED"}},
    ],
)
def test_runner_crash_origin_cannot_grant_completion_or_merge(fields: dict[str, object]) -> None:
    payload = {
        "schema_version": "dispatch_runner_result.v1",
        "result_origin": "runner_crash",
        "result_state": "FAILED",
        "promotion_state": "RESULT_RECORDED",
        "run_result": {"status": "FAILED"},
        **fields,
    }
    decoded = decode_dispatch_runner_result(intent_result=json.dumps(payload), approval_payload={})
    assert isinstance(decoded, InvalidDispatchReport)
    assert decoded.code is DispatchContractCode.INCONSISTENT_OUTCOME


@pytest.mark.parametrize("nonfinite", [float("nan"), float("inf"), -float("inf")])
def test_embedded_and_serialized_reports_share_nonfinite_value_rejection(nonfinite: float) -> None:
    payload = {
        "schema_version": "dispatch_runner_result.v1",
        "result_origin": "AUTOMATED",
        "result_state": "COMPLETED",
        "promotion_state": "RESULT_RECORDED",
        "run_result": {
            "status": "COMPLETED",
            "artifacts": [{"artifact_type": "test_result", "content": {"score": nonfinite}}],
        },
    }
    embedded = decode_dispatch_runner_result(
        intent_result=None, approval_payload={"dispatch_result": payload}
    )
    serialized = decode_dispatch_runner_result(
        intent_result=json.dumps(payload), approval_payload={}
    )
    assert isinstance(embedded, InvalidDispatchReport)
    assert isinstance(serialized, InvalidDispatchReport)
    assert embedded.code is serialized.code is DispatchContractCode.INVALID_ENVELOPE


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("{broken", DispatchContractCode.MALFORMED_JSON),
        ("[]", DispatchContractCode.INVALID_ENVELOPE),
        ("null", DispatchContractCode.INVALID_ENVELOPE),
        (17, DispatchContractCode.INVALID_ENVELOPE),
        (
            '{"schema_version":"dispatch_runner_result.v1","run_result":{"status":"FAILED","status":"COMPLETED"}}',
            DispatchContractCode.INVALID_ENVELOPE,
        ),
        (
            '{"schema_version":"dispatch_runner_result.v1","run_result":{"status":"COMPLETED","artifacts":[{"artifact_type":"test_result","content":{"score":NaN}}]}}',
            DispatchContractCode.INVALID_ENVELOPE,
        ),
        ('{"schema_version":"future"}', DispatchContractCode.UNSUPPORTED_SCHEMA),
        (
            '{"schema_version":"dispatch_runner_result.v1","run_result":{"status":"invented"}}',
            DispatchContractCode.INVALID_ENVELOPE,
        ),
    ],
)
def test_invalid_ingress_has_a_typed_diagnostic_instead_of_unavailable(
    raw: object, code: DispatchContractCode
) -> None:
    ingress = decode_dispatch_runner_result(intent_result=raw, approval_payload={})
    assert isinstance(ingress, InvalidDispatchReport)
    assert ingress.code is code
    with pytest.raises(DispatchContractViolation) as raised:
        normalize_dispatch_runner_result(intent_result=raw, approval_payload={})
    assert raised.value.diagnostic == ingress


@pytest.mark.parametrize("raw", [None, "", "  "])
def test_absent_evidence_remains_a_supported_variant(raw: object) -> None:
    assert isinstance(
        decode_dispatch_runner_result(intent_result=raw, approval_payload={}), MissingDispatchReport
    )
    assert (
        normalize_dispatch_runner_result(intent_result=raw, approval_payload={}).state
        is DispatchResultState.UNAVAILABLE
    )


@pytest.mark.parametrize(
    "status",
    [
        "FAILED",
        "VERIFICATION_FAILED",
        "BLOCKED",
        "DRY_RUN_COMPLETED",
        "TIMED_OUT",
        "CANCELED",
        "UNAVAILABLE",
    ],
)
def test_declared_unsuccessful_outcomes_preserve_evidence_without_merge_promotion(
    status: str,
) -> None:
    raw = json.dumps(
        {
            "schema_version": "dispatch_runner_result.v1",
            "run_result": {
                "status": status,
                "changed_files": ["partial.py"],
                "verification_output": ["retained failure evidence"],
            },
        }
    )
    ingress = decode_dispatch_runner_result(intent_result=raw, approval_payload={})
    assert isinstance(ingress, AvailableDispatchReport)
    assert ingress.result.state is DispatchResultState.FAILED
    assert ingress.result.promotion_state is DispatchPromotionState.RESULT_RECORDED
    assert ingress.result.observation is not None
    assert ingress.result.observation.status == status
    assert ingress.result.observation.changed_files == ("partial.py",)


@pytest.mark.parametrize("embedded", [None, "bad", {}, []])
def test_invalid_embedded_report_does_not_fall_back_to_success(embedded: object) -> None:
    raw = json.dumps(
        {"schema_version": "dispatch_runner_result.v1", "run_result": {"status": "COMPLETED"}}
    )
    ingress = decode_dispatch_runner_result(
        intent_result=raw, approval_payload={"dispatch_result": embedded}
    )
    assert isinstance(ingress, InvalidDispatchReport)


@pytest.mark.parametrize("field", ["changed_files", "verification", "risks"])
def test_legacy_manual_evidence_does_not_silently_drop_malformed_collections(field: str) -> None:
    payload = {
        "manual_recovery": True,
        "branch": "branch",
        "base_sha": "a" * 40,
        "commit_sha": "b" * 40,
        "staff_review": {"verdict": "APPROVE"},
        field: "not a collection",
    }
    ingress = decode_dispatch_runner_result(intent_result=None, approval_payload=payload)
    assert isinstance(ingress, InvalidDispatchReport)
    assert ingress.code is DispatchContractCode.INVALID_ENVELOPE


def test_diagnostic_is_durable_publicly_readable_and_replay_deduplicated(
    work_unit_ledger: Path,
) -> None:
    raw = '{"password":"do-not-log-me",broken'
    diagnostic = decode_dispatch_runner_result(intent_result=raw, approval_payload={})
    assert isinstance(diagnostic, InvalidDispatchReport)
    with tx() as connection:
        first = record_dispatch_contract_violation(
            connection, intent_id="ledger-owned-intent", diagnostic=diagnostic
        )
    with tx() as connection:
        second = record_dispatch_contract_violation(
            connection, intent_id="ledger-owned-intent", diagnostic=diagnostic
        )
    assert first == second
    result = run_coordination_command(["list_ledger_events"], root=work_unit_ledger)
    events = result["events"]
    assert len(events) == 1
    assert events[0]["event_type"] == DISPATCH_CONTRACT_VIOLATION_EVENT
    assert events[0]["status"] == "PENDING"
    assert events[0]["aggregate_id"] == "ledger-owned-intent"
    assert events[0]["payload"]["code"] == DispatchContractCode.MALFORMED_JSON.value
    assert events[0]["payload"]["payload_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert "password" not in json.dumps(events)
    assert "do-not-log-me" not in json.dumps(events)


def test_diagnostic_persistence_failure_is_explicit_and_does_not_leak_inputs(
    work_unit_ledger: Path,
) -> None:
    diagnostic = InvalidDispatchReport.from_input(DispatchContractCode.INVALID_ENVELOPE, "secret")
    with (
        pytest.raises(
            DispatchDiagnosticPersistenceError, match="persistence unavailable"
        ) as raised,
        tx() as connection,
    ):
        # A real database privilege failure, scoped to this transaction.
        connection.execute("SET TRANSACTION READ ONLY")
        record_dispatch_contract_violation(
            connection, intent_id="ledger-owned-intent", diagnostic=diagnostic
        )
    assert "secret" not in str(raised.value)
    assert run_coordination_command(["list_ledger_events"], root=work_unit_ledger)["events"] == []
