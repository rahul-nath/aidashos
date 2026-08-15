# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

from local_first_agent_os.coordination.dispatch import (
    claim_next_dispatch_intent,
    complete_dispatch_intent,
    list_dispatch_intents,
    submit_dispatch_intent,
)
from local_first_agent_os.coordination.execution import (
    complete_execution_lease,
    open_execution_lease,
)
from local_first_agent_os.coordination.milestones import (
    complete_saga_milestone,
    create_saga_milestone,
)
from local_first_agent_os.coordination.outcomes import (
    BusinessFailure,
    ExecutionTransition,
    FailureCategory,
    InfrastructureFailure,
    TerminalOutcome,
    classify_failure,
    failure_category,
)
from local_first_agent_os.coordination.projects import create_saga
from local_first_agent_os.coordination.store import set_root


def test_manual_milestone_completion_is_distinct_from_automated_success(
    tmp_path: Path, monkeypatch
) -> None:
    set_root(str(tmp_path))
    saga_id = create_saga("outcome test")["saga_id"]
    milestone_id = create_saga_milestone(saga_id, "recovered", 1)["milestone"]["milestone_id"]

    completed = complete_saga_milestone(milestone_id)

    assert completed["milestone"]["status"] == "COMPLETED"
    assert completed["milestone"]["outcome"] == TerminalOutcome.MANUAL_RECOVERY_COMPLETION


def test_known_dispatch_and_lease_failures_receive_structured_outcomes(
    tmp_path: Path, monkeypatch
) -> None:
    set_root(str(tmp_path))
    submitted = submit_dispatch_intent("senior", "test")
    intent_id = submitted["intent_id"]
    claim_next_dispatch_intent("worker", "senior")

    complete_dispatch_intent(
        intent_id,
        "FAILED",
        error="OSError: [Errno 7] Argument list too long: python3",
    )
    intent = next(
        item for item in list_dispatch_intents()["intents"] if item["intent_id"] == intent_id
    )
    assert intent["outcome"] == TerminalOutcome.ARGUMENT_LIST_TOO_LONG

    opened = open_execution_lease("expired", "worker", timeout_seconds=10)
    completed = complete_execution_lease(
        opened["lease"]["lease_id"],
        "TIMED_OUT",
        error="frontier execution deadline reached",
    )
    assert completed["lease"]["outcome"] == TerminalOutcome.DEADLINE_EXCEEDED
    assert completed["lease"]["agent_status"] == "FAILED"
    assert completed["lease"]["agent_failure_category"] == "INFRASTRUCTURE"
    assert completed["lease"]["agent_failure"] == "DEADLINE_EXCEEDED"
    assert completed["lease"]["supervisor_status"] == "COMPLETED"


def test_streamed_provider_limit_variants_classify_as_usage_limit() -> None:
    assert ExecutionTransition.SWITCH_TO_FALLBACK == "SWITCH_TO_FALLBACK"
    for evidence in (
        "rate_limit_event",
        "You've hit your session limit",
        '"api_error_status":429',
        "quota exceeded",
    ):
        assert classify_failure(evidence) == TerminalOutcome.USAGE_LIMIT


def test_business_and_infrastructure_failures_are_disjoint() -> None:
    assert set(BusinessFailure).isdisjoint(set(InfrastructureFailure))
    assert failure_category(BusinessFailure.VERIFICATION_FAILED) == FailureCategory.BUSINESS
    assert failure_category(InfrastructureFailure.USAGE_LIMIT) == FailureCategory.INFRASTRUCTURE
    assert classify_failure("codex authentication invalid or expired") == (
        TerminalOutcome.AUTHENTICATION_FAILED
    )


def test_duplicate_milestone_intent_is_suppressed_at_claim_time(
    tmp_path: Path, monkeypatch
) -> None:
    set_root(str(tmp_path))
    saga_id = create_saga("duplicate claim guard")["saga_id"]
    milestone_id = create_saga_milestone(saga_id, "single run", 1)["milestone"]["milestone_id"]
    source = f"approved_gawd:test:milestone:{milestone_id}"
    first = submit_dispatch_intent("senior", "first", source=source)
    second = submit_dispatch_intent("senior", "duplicate", source=source)

    claimed = claim_next_dispatch_intent("dispatcher-a", "senior")
    suppressed = claim_next_dispatch_intent("dispatcher-b", "senior")

    assert claimed["intent"]["intent_id"] == first["intent_id"]
    assert suppressed["intent"] is None
    assert suppressed["duplicate_suppressed"]["intent_id"] == second["intent_id"]
    assert suppressed["duplicate_suppressed"]["outcome"] == TerminalOutcome.DUPLICATE_SUPPRESSED
