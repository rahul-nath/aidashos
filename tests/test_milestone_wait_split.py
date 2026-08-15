# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The submit and the wait are two steps now, not one.

The defect: a DBOS step checkpoints when it returns, so a step that submitted
work and then blocked for an hour had recorded nothing when the process died at
minute fifty-nine. Recovery re-ran it from the top and submitted again.

What makes the split safe is that `start` returns as soon as the submission is
durable. What makes it correct is that `settle` re-reads the ledger rather than
believing whatever woke it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_dispatch_backed_runtime import _context

from local_first_agent_os.constants import dispatch_settlement_topic
from local_first_agent_os.contracts import DispatchIntentStatus
from local_first_agent_os.work_units.execution import (
    CompositeExecutorRuntime,
    DeferrableMilestoneRuntime,
    DispatchBackedExecutorRuntime,
    DispatchWaitTimeout,
    MilestoneAwaitingDispatch,
    MilestoneFailed,
    MilestoneSucceeded,
    SimulatedExecutorRuntime,
)
from local_first_agent_os.work_units.executors import ExecutorKind


class _Submitter:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.count += 1
        return {"ok": True, "intent_id": "intent-1"}


def _runtime(submitter: Any) -> DispatchBackedExecutorRuntime:
    return DispatchBackedExecutorRuntime(
        intent_submitter=submitter,
        target_project_id="proj",
        fact_recorder=lambda *_: None,
    )


# --------------------------------------------------------------------------- #
# start returns instead of blocking
# --------------------------------------------------------------------------- #


def test_start_returns_a_token_rather_than_waiting() -> None:
    """The whole point: the submit is durable before anything waits."""

    started = _runtime(_Submitter()).start(_context())

    assert isinstance(started, MilestoneAwaitingDispatch)
    assert started.dispatch_intent_id == "intent-1"


def test_start_carries_the_plans_timeout_not_the_flat_hour() -> None:
    started = _runtime(_Submitter()).start(_context())

    assert isinstance(started, MilestoneAwaitingDispatch)
    assert started.timeout_seconds == float(_context().milestone.timeout_seconds)


def test_start_submits_exactly_once() -> None:
    """A start that also waited would re-submit on every re-execution."""

    submitter = _Submitter()
    _runtime(submitter).start(_context())

    assert submitter.count == 1


# --------------------------------------------------------------------------- #
# settle reads the ledger, not the message
# --------------------------------------------------------------------------- #


def test_settle_refuses_an_intent_that_has_not_settled() -> None:
    """A spurious wake must not be read as an outcome."""

    runtime = _runtime(_Submitter())
    awaiting = MilestoneAwaitingDispatch(dispatch_intent_id="missing", timeout_seconds=5.0)

    with pytest.raises(DispatchWaitTimeout):
        runtime.settle(_context(), awaiting)


def _runner_result(**run_result: Any) -> str:
    """A settled result shaped the way the dispatcher actually writes one."""

    return json.dumps(
        {
            "schema_version": "dispatch_runner_result.v1",
            "intent_id": "intent-1",
            "run_result": {"output_summary": "", **run_result},
        }
    )


def test_settle_translates_a_terminal_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "local_first_agent_os.work_units.execution.dispatch_intent_row",
        lambda _id: {
            "status": DispatchIntentStatus.DONE.value,
            "result": _runner_result(output_summary="the agent's answer"),
            "outcome": None,
            "error": None,
        },
    )
    runtime = _runtime(_Submitter())
    awaiting = MilestoneAwaitingDispatch(dispatch_intent_id="intent-1", timeout_seconds=5.0)

    outcome = runtime.settle(_context(), awaiting)

    assert isinstance(outcome, MilestoneSucceeded)


def test_a_result_that_is_not_a_runner_payload_cannot_be_vouched_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hand-completed intent may be finished; it is not *checkable*.

    The old behaviour accepted any DONE row and wrote its result string as the
    body of every required artifact, so a one-word completion produced a full set
    of evidence.
    """

    monkeypatch.setattr(
        "local_first_agent_os.work_units.execution.dispatch_intent_row",
        lambda _id: {
            "status": DispatchIntentStatus.DONE.value,
            "result": "done",
            "outcome": None,
            "error": None,
        },
    )
    runtime = _runtime(_Submitter())
    awaiting = MilestoneAwaitingDispatch(dispatch_intent_id="intent-1", timeout_seconds=5.0)

    outcome = runtime.settle(_context(), awaiting)

    assert isinstance(outcome, MilestoneFailed)
    assert outcome.failure_code == "unverifiable_dispatch_result"


def test_a_run_that_changed_nothing_does_not_produce_a_source_patch() -> None:
    """The regression that matters: evidence must be able to be absent.

    Asserted against the derivation directly rather than through a milestone, so
    it pins the rule for every executor kind rather than for whichever one the
    acceptance document happens to put first.
    """

    from local_first_agent_os.work_units.events import ArtifactKind
    from local_first_agent_os.work_units.execution import _agent_evidence

    empty_run = {"output_summary": "I reviewed the code and it looks fine."}
    assert _agent_evidence(ArtifactKind.SOURCE_PATCH, empty_run) is None
    assert _agent_evidence(ArtifactKind.TEST_RESULT, empty_run) is None
    assert _agent_evidence(ArtifactKind.OPERATOR_APPROVAL, empty_run) is None

    real_run = {
        "output_summary": "patched it",
        "changed_files": ["src/a.py", "src/b.py"],
        "verification_commands": ["pytest"],
        "verification_output": ["2 passed"],
    }
    patch = _agent_evidence(ArtifactKind.SOURCE_PATCH, real_run)
    tests = _agent_evidence(ArtifactKind.TEST_RESULT, real_run)
    assert patch is not None and "src/a.py" in patch
    assert tests is not None and "2 passed" in tests
    # Distinguishable bodies. The whole defect was that these were identical.
    assert patch != tests


def test_settle_translates_a_failed_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "local_first_agent_os.work_units.execution.dispatch_intent_row",
        lambda _id: {
            "status": DispatchIntentStatus.FAILED.value,
            "result": None,
            "outcome": "AGENT_ERROR",
            "error": "it broke",
        },
    )
    runtime = _runtime(_Submitter())
    awaiting = MilestoneAwaitingDispatch(dispatch_intent_id="intent-1", timeout_seconds=5.0)

    outcome = runtime.settle(_context(), awaiting)

    assert isinstance(outcome, MilestoneFailed)
    assert outcome.failure_code == "AGENT_ERROR"


# --------------------------------------------------------------------------- #
# Who can defer, and who is left alone
# --------------------------------------------------------------------------- #


def test_a_synchronous_runtime_is_not_deferrable() -> None:
    """A simulation owes no `start`/`settle`, and the engine must not ask."""

    assert not isinstance(SimulatedExecutorRuntime(), DeferrableMilestoneRuntime)


def test_the_dispatch_runtime_is_deferrable() -> None:
    assert isinstance(_runtime(_Submitter()), DeferrableMilestoneRuntime)


def test_a_composite_finishes_a_synchronous_route_inside_start() -> None:
    """Mixed routing: a run-only runtime's outcome is already a valid start."""

    composite = CompositeExecutorRuntime(
        routes={ExecutorKind.PLAN_IMPLEMENTATION: SimulatedExecutorRuntime()}
    )
    context = _context()

    started = composite.start(context)

    assert not isinstance(started, MilestoneAwaitingDispatch)


def test_a_composite_refuses_to_settle_a_route_that_never_deferred() -> None:
    composite = CompositeExecutorRuntime(
        routes={ExecutorKind.PLAN_IMPLEMENTATION: SimulatedExecutorRuntime()}
    )
    awaiting = MilestoneAwaitingDispatch(dispatch_intent_id="x", timeout_seconds=1.0)

    with pytest.raises(RuntimeError, match="cannot settle"):
        composite.settle(_context(), awaiting)


# --------------------------------------------------------------------------- #
# Sender and receiver must agree on the topic
# --------------------------------------------------------------------------- #


def test_the_topic_is_keyed_by_intent_so_a_settle_wakes_only_its_own_wait() -> None:
    assert dispatch_settlement_topic("a") != dispatch_settlement_topic("b")
    assert dispatch_settlement_topic("a") == dispatch_settlement_topic("a")


def test_the_submitter_records_where_to_send_the_wake() -> None:
    """Without this the ledger cannot find the workflow parked on the intent."""

    captured: dict[str, Any] = {}

    def submitter(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"ok": True, "intent_id": "intent-1"}

    context = _context()
    _runtime(submitter).start(context)

    assert captured["notify_workflow_id"] == context.child_workflow_id
