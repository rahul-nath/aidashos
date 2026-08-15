# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import contextlib
import json
import logging
import sys
from types import SimpleNamespace
from typing import Any

import pytest

import local_first_agent_os.observability as observability
from local_first_agent_os.coordination.failures import (
    FAILURE_SCHEMA_VERSION,
    DurableFailureError,
    exceptional_failure,
    expected_failure,
)
from local_first_agent_os.coordination.outcomes import (
    BusinessFailure,
    FailureCategory,
    TerminalOutcome,
)
from local_first_agent_os.coordination.store import err
from local_first_agent_os.observability import JsonLogFormatter


def test_typed_err_preserves_legacy_shape_and_adds_failure_v1(caplog) -> None:
    with caplog.at_level(logging.INFO):
        result = err(
            "invalid_base_sha",
            _operation="request_recovery_staff_review",
            base_sha="abc",
        )

    assert result["ok"] is False
    assert result["error"] == "invalid_base_sha"
    assert result["base_sha"] == "abc"
    assert result["failure"] == {
        "error_code": "invalid_base_sha",
        "category": "BUSINESS",
        "retryable": False,
        "operation": "request_recovery_staff_review",
        "message": "",
        "terminal_outcome": "",
        "exception_type": "",
        "schema_version": FAILURE_SCHEMA_VERSION,
    }
    record = next(
        record for record in caplog.records if record.message == "coordination_command_rejected"
    )
    assert record.error_code == "invalid_base_sha"
    assert record.category == "BUSINESS"
    assert record.retryable is False
    assert record.operation == "request_recovery_staff_review"


def test_existing_outcome_enums_drive_failure_category_and_retryability() -> None:
    business = expected_failure(
        BusinessFailure.VERIFICATION_FAILED,
        operation="verify",
    )
    timeout = exceptional_failure(TimeoutError("provider stalled"), operation="dispatch")

    assert business.category is FailureCategory.BUSINESS
    assert business.retryable is False
    assert business.terminal_outcome == TerminalOutcome.VERIFICATION_FAILED
    assert timeout.error_code == TerminalOutcome.DEADLINE_EXCEEDED
    assert timeout.category is FailureCategory.INFRASTRUCTURE
    assert timeout.retryable is True
    assert timeout.exception_type == "TimeoutError"


def test_generic_durable_exception_preserves_host_assigned_failure() -> None:
    failure = expected_failure(
        "lease_conflict",
        operation="claim_execution_lease",
    )

    normalized = exceptional_failure(
        DurableFailureError(failure),
        operation="outer_boundary",
    )

    assert normalized is failure


def test_json_logs_add_failure_dimensions_for_untyped_exceptions() -> None:
    settings = SimpleNamespace(service_name="test-service", env="test")
    formatter = JsonLogFormatter(settings)  # type: ignore[arg-type]
    try:
        raise TimeoutError("model deadline")
    except TimeoutError:
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "dispatch failed",
            (),
            sys.exc_info(),
            func="run_dispatch",
        )

    payload = json.loads(formatter.format(record))

    assert payload["error_code"] == "DEADLINE_EXCEEDED"
    assert payload["category"] == "INFRASTRUCTURE"
    assert payload["retryable"] is True
    assert payload["operation"] == "run_dispatch"
    assert "TimeoutError: model deadline" in payload["exc_info"]


class _FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}
        self.exceptions: list[BaseException] = []

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def record_exception(self, error: BaseException) -> None:
        self.exceptions.append(error)


class _FakeTracer:
    def __init__(self, span: _FakeSpan) -> None:
        self.span = span

    @contextlib.contextmanager
    def start_as_current_span(self, _name: str):
        yield self.span


def test_failed_span_receives_same_failure_dimensions(monkeypatch) -> None:
    span = _FakeSpan()
    monkeypatch.setattr(observability, "_tracer", _FakeTracer(span))

    with (
        pytest.raises(TimeoutError, match="staff review deadline"),
        observability._span("staff_review", {"task_id": "task-1"}),
    ):
        raise TimeoutError("staff review deadline")

    assert span.attributes["local_agent.error_code"] == "DEADLINE_EXCEEDED"
    assert span.attributes["local_agent.category"] == "INFRASTRUCTURE"
    assert span.attributes["local_agent.retryable"] is True
    assert span.attributes["local_agent.operation"] == "staff_review"
    assert len(span.exceptions) == 1
