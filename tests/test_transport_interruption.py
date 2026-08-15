# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A provider whose stream dies must not be billed to the milestone's budget.

The case these pin happened on 2026-08-11. Milestone "5" of the ACL WorkUnit
lost its implementation turn to `API Error: Connection closed mid-response`,
which left `dependencies did not complete` behind it. The classifier matched the
consequence, recorded `DEPENDENCY_FAILED` - a `BusinessFailure`, meaning the
work ran and its contract was not satisfied - and the executor stamped
`CORRECTABLE`, which spends a try. That was the third infrastructure failure in
a row on a milestone whose code had never been judged once, and clearing it took
an operator retry-budget override.
"""

from __future__ import annotations

import pytest

from local_first_agent_os.coordination.outcomes import (
    FailureCategory,
    TerminalOutcome,
    classify_failure,
    failure_category,
)
from local_first_agent_os.work_units.execution import _failure_class_for_outcome
from local_first_agent_os.work_units.lifecycle import (
    FailureClass,
    LifecyclePhase,
    MilestoneExecutionStatus,
)
from local_first_agent_os.work_units.retry import (
    RetryGrounds,
    RetryPermitted,
    decide_retry,
    spends_an_attempt,
)

# The failure text exactly as the ledger recorded it, consequence clause and all.
_OBSERVED = (
    "claude reported: API Error: Connection closed mid-response. The response above may "
    "be incomplete.; dependencies did not complete: "
    "dispatch_5ecebb9b_senior_implementation; Review verdict still requests changes "
    "after 0 revision round(s) (staff review produced no typed review_result.v1 "
    "evidence); failing closed to the operator gate."
)


def test_the_observed_failure_is_read_as_transport_not_dependency() -> None:
    """The cause outranks the consequence it produced.

    Both sentences are in the text. `dependencies did not complete` is true and
    is the effect: the implementation turn died, so review had nothing to read.
    Answering with it describes the wreckage instead of the crash.
    """

    assert classify_failure(_OBSERVED) is TerminalOutcome.TRANSPORT_INTERRUPTED


@pytest.mark.parametrize(
    "text",
    [
        "API Error: Connection closed mid-response.",
        "httpx.RemoteProtocolError: peer closed connection without sending complete message",
        "urllib3.exceptions.ProtocolError: Connection aborted",
        "ConnectionResetError: [Errno 54] Connection reset by peer",
        "http.client.IncompleteRead: IncompleteRead(1024 bytes read)  incomplete chunked read",
        "httpcore.RemoteProtocolError: Server disconnected without sending a response",
    ],
)
def test_the_shapes_a_dropped_stream_arrives_in(text: str) -> None:
    assert classify_failure(text) is TerminalOutcome.TRANSPORT_INTERRUPTED


def test_a_real_verdict_outranks_a_dropped_connection_in_the_same_log() -> None:
    """Under-charging is the safe direction; discarding a judgment is not.

    A run that verified and failed said something about the work. A connection
    error elsewhere in that log does not take it back, so the verdict keeps the
    outcome and the milestone is still charged for it.
    """

    both = "verification failed: 3 tests failed\nwarning: connection reset by peer during upload"

    assert classify_failure(both) is TerminalOutcome.VERIFICATION_FAILED


def test_a_dropped_stream_is_infrastructure_not_business() -> None:
    """The category is the whole claim: nothing about the work was learned."""

    assert failure_category(TerminalOutcome.TRANSPORT_INTERRUPTED) is FailureCategory.INFRASTRUCTURE


def test_the_executor_classes_a_dropped_stream_as_transient() -> None:
    assert _failure_class_for_outcome(TerminalOutcome.TRANSPORT_INTERRUPTED.value) is (
        FailureClass.TRANSIENT
    )


@pytest.mark.parametrize(
    "outcome",
    [
        TerminalOutcome.VERIFICATION_FAILED.value,
        TerminalOutcome.DEPENDENCY_FAILED.value,
        TerminalOutcome.USAGE_LIMIT.value,
        TerminalOutcome.UNKNOWN_FAILURE.value,
        "DISPATCH_FAILED",
    ],
)
def test_every_other_outcome_still_spends_its_attempt(outcome: str) -> None:
    """The narrowness is the safety.

    An outcome that cannot be told apart from work which would fail the same way
    again keeps charging, so the budget still stops a milestone that is simply
    wrong. `UNKNOWN_FAILURE` is the one that matters most: it is what an
    unrecognised message becomes, and exempting it would exempt everything.
    """

    assert _failure_class_for_outcome(outcome) is FailureClass.CORRECTABLE
    assert spends_an_attempt(_failure_class_for_outcome(outcome)) is True


def test_a_milestone_at_its_budget_may_still_retry_a_dropped_stream() -> None:
    """The end of the path, which is the behaviour that cost the override.

    At attempt 3 of 3 a spent class refuses and demands an operator decision. The
    same milestone, blocked because a stream died, is permitted without one.
    """

    def decide(failure_class: FailureClass):
        return decide_retry(
            milestone_key="5",
            phase=LifecyclePhase.VERIFY,
            status=MilestoneExecutionStatus.BLOCKED,
            attempt=3,
            failure_class=failure_class,
            max_attempts=3,
        )

    dropped = decide(FailureClass.TRANSIENT)

    assert isinstance(dropped, RetryPermitted)
    assert dropped.next_attempt == 4
    assert dropped.grounds is RetryGrounds.NO_ATTEMPT_SPENT
    assert not isinstance(decide(FailureClass.CORRECTABLE), RetryPermitted)


# The failure text exactly as the ledger recorded it on 2026-08-12, consequence
# clause and all. Milestone 1 of the worktree-loss WorkUnit was charged one of
# its three attempts for this.
_OBSERVED_OVERLOAD = (
    "claude advisory exited 1; claude reported: API Error: 529 Overloaded. This is a "
    "server-side issue, usually temporary - try again in a moment. If it persists, check "
    "https://status.claude.com.; dependencies did not complete: "
    "dispatch_9f75e62a_senior_implementation"
)


def test_a_server_side_overload_is_not_the_milestone_s_dependency_failing() -> None:
    """The same confusion as the dropped stream, through a marker nobody had seen.

    Both sentences are in the text again. `dependencies did not complete` is the
    wreckage; the 529 is the crash.
    """

    assert classify_failure(_OBSERVED_OVERLOAD) is TerminalOutcome.PROVIDER_OVERLOADED


@pytest.mark.parametrize(
    "text",
    [
        "API Error: 529 Overloaded.",
        "HTTP 529 returned by the upstream provider",
        '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
    ],
)
def test_the_shapes_an_overload_arrives_in(text: str) -> None:
    assert classify_failure(text) is TerminalOutcome.PROVIDER_OVERLOADED


def test_an_overload_is_infrastructure_not_business() -> None:
    assert failure_category(TerminalOutcome.PROVIDER_OVERLOADED) is FailureCategory.INFRASTRUCTURE


def test_the_executor_classes_an_overload_as_transient() -> None:
    assert _failure_class_for_outcome(TerminalOutcome.PROVIDER_OVERLOADED.value) is (
        FailureClass.TRANSIENT
    )
    overload_class = _failure_class_for_outcome(TerminalOutcome.PROVIDER_OVERLOADED.value)
    assert spends_an_attempt(overload_class) is False


def test_a_real_verdict_still_outranks_an_overload_in_the_same_log() -> None:
    """Under-charging stays the safe direction; discarding a judgment does not."""

    both = "verification failed: 3 tests failed\nwarning: API Error: 529 Overloaded"

    assert classify_failure(both) is TerminalOutcome.VERIFICATION_FAILED


def test_a_spent_quota_is_still_not_an_overload() -> None:
    """Both are the provider refusing; only one of them clears in moments."""

    quota = "claude reported: You've hit your session limit - resets 4:20pm (America/New_York)"

    assert classify_failure(quota) is TerminalOutcome.USAGE_LIMIT
    assert _failure_class_for_outcome(TerminalOutcome.USAGE_LIMIT.value) is FailureClass.CORRECTABLE
