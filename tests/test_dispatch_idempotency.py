# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Deterministic dispatch identity.

The failure being prevented: a milestone executor submits an intent, waits
inside a DBOS step, and the process dies before the step checkpoints. Recovery
re-runs the step from the top. Without a key derived from the work, the second
submit creates a second intent and two agents do the same milestone at full
cost, either of which can land a branch.
"""

from __future__ import annotations

from typing import Any

from local_first_agent_os.coordination.dispatch import (
    complete_dispatch_intent,
    submit_dispatch_intent,
)
from local_first_agent_os.work_units.execution import DispatchBackedExecutorRuntime

KEY = "work_unit:w1:milestone:build:attempt:1"


def _submit(key: str | None = KEY, **kwargs: Any) -> dict[str, Any]:
    return submit_dispatch_intent(
        "senior",
        "do the thing",
        "code",
        None,
        "work_unit:w1:milestone_execution:build",
        idempotency_key=key,
        **kwargs,
    )


def test_resubmitting_the_same_work_returns_the_first_intent() -> None:
    first = _submit()
    second = _submit()

    assert first["ok"] and second["ok"]
    assert second["intent_id"] == first["intent_id"]
    assert first["deduplicated"] is False
    assert second["deduplicated"] is True


def test_a_deduplicated_submit_reports_the_incumbent_status() -> None:
    """Not 'PENDING' by default: the caller queued nothing and must not think it did."""

    first = _submit()
    completed = complete_dispatch_intent(str(first["intent_id"]), "DONE", result="done")
    assert completed["ok"], completed

    second = _submit()

    assert second["intent_id"] == first["intent_id"]
    assert second["deduplicated"] is True
    assert second["status"] == "DONE"


def test_a_different_attempt_is_different_work() -> None:
    """A retry must get its own intent, or a milestone could never be retried."""

    first = _submit()
    retry = _submit(key="work_unit:w1:milestone:build:attempt:2")

    assert retry["intent_id"] != first["intent_id"]
    assert retry["deduplicated"] is False


def test_omitting_the_key_keeps_every_call_distinct() -> None:
    """Producers with no natural identity (an operator typing) are unaffected."""

    first = _submit(key=None)
    second = _submit(key=None)

    assert first["intent_id"] != second["intent_id"]
    assert first["deduplicated"] is False


def test_a_quorum_submit_refuses_a_key_rather_than_deduplicating_its_children() -> None:
    result = submit_dispatch_intent(
        "senior",
        "judge this",
        "advisory",
        None,
        "src",
        fanout=3,
        allow_tiers=["senior", "staff"],
        reduce="vote",
        idempotency_key=KEY,
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_idempotency_key"


# --------------------------------------------------------------------------- #
# The key the executor derives
# --------------------------------------------------------------------------- #


def test_the_executor_key_carries_work_unit_milestone_and_attempt() -> None:
    """Exactly the three facts `_milestone_workflow_id` names a workflow with."""

    class _Milestone:
        stable_key = "build"

    class _Context:
        work_unit_id = "w1"
        milestone = _Milestone()
        attempt = 2

    key = DispatchBackedExecutorRuntime().idempotency_key(_Context())  # type: ignore[arg-type]

    assert key == "work_unit:w1:milestone:build:attempt:2"


def test_the_executor_key_separates_attempts_but_not_re_executions() -> None:
    class _Milestone:
        stable_key = "build"

    def _context(attempt: int) -> Any:
        class _Context:
            work_unit_id = "w1"
            milestone = _Milestone()

        ctx = _Context()
        ctx.attempt = attempt  # type: ignore[attr-defined]
        return ctx

    runtime = DispatchBackedExecutorRuntime()

    assert runtime.idempotency_key(_context(1)) == runtime.idempotency_key(_context(1))
    assert runtime.idempotency_key(_context(1)) != runtime.idempotency_key(_context(2))
