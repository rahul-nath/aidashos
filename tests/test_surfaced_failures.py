# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Failures that reach somebody.

The scenarios in ``features/surfaced_failures.feature`` cover the edge cases. The
unit tests below take one decision variable each along the same path rather than
their cross product: whether a progress event carries risks, whether its fields
collide with a LogRecord attribute, whether a field survives the formatter's
allowlist, whether a drained row raised, how many consecutive passes raised, and
which consumer is reading the outcome.

The formatter is exercised through ``JsonLogFormatter`` rather than through the
``LogRecord``, deliberately: the allowlist is where a field is silently dropped,
so a test that reads attributes off the record cannot see the drop it exists to
prevent.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when
from work_unit_support import compile_acceptance_doc

from local_first_agent_os.observability import JsonLogFormatter
from local_first_agent_os.progress_events import (
    ReservedProgressField,
    emit_progress,
    progress_event_sink,
)
from local_first_agent_os.work_units import commands as work_unit_commands
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.enqueue_drainer import (
    EnqueueDrainer,
    Idle,
    Stalled,
)
from local_first_agent_os.work_units.root_workflow import (
    EnqueueDelivery,
    EnqueueFailed,
    EnqueueSettled,
    drain_enqueue_outbox,
)

scenarios("features/surfaced_failures.feature")


BOOM = RuntimeError("dbos refused the workflow")


def _formatted(record: logging.LogRecord) -> dict[str, Any]:
    """What actually reaches the log stream, allowlist and all."""

    settings = SimpleNamespace(service_name="test-service", env="test")
    return json.loads(JsonLogFormatter(settings).format(record))  # type: ignore[arg-type]


def _progress_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    return next(record for record in caplog.records if record.msg == "dispatch_progress")


def _enqueued_work_unit() -> str:
    compiled = compile_acceptance_doc(design_doc_id="surfaced_failures")
    assert compiled.compiled_plan_revision_id is not None
    started = repo.start_work_unit(compiled.compiled_plan_revision_id, title="surfaced")
    return started.work_unit.work_unit_id


@pytest.fixture()
def start_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(work_unit_id: str, delivery: EnqueueDelivery) -> dict[str, Any]:
        raise BOOM

    monkeypatch.setattr("local_first_agent_os.work_units.root_workflow.start_root_workflow", _raise)


# --- gherkin steps ------------------------------------------------------------


@pytest.fixture()
def world() -> dict[str, Any]:
    return {}


@when(parsers.parse('a progress event says "{message}"'))
def _emits(world: dict[str, Any], message: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        emit_progress(message, phase="task_completed", task_name="junior_context")
    world["payload"] = _formatted(_progress_record(caplog))


@when(parsers.parse('a task finishes failed with the risk "{risk}"'))
def _emits_with_risk(world: dict[str, Any], risk: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        emit_progress(
            "analyst turn junior_context finished failed: the agent could not start",
            phase="task_completed",
            task_name="junior_context",
            status="failed",
            risks=(risk,),
        )
    world["payload"] = _formatted(_progress_record(caplog))


@when(parsers.parse('a progress event names a field "{field}"'))
def _emits_reserved(world: dict[str, Any], field: str) -> None:
    try:
        emit_progress("a message", phase="p", **{field: "value"})
    except ReservedProgressField as exc:
        world["refusal"] = exc


@then(parsers.parse('the log carries "{text}"'))
def _log_carries(world: dict[str, Any], text: str) -> None:
    assert text in json.dumps(world["payload"])


@then(parsers.parse('the log message is still "{message}"'))
def _log_message_is(world: dict[str, Any], message: str) -> None:
    assert world["payload"]["msg"] == message


@then("it is refused")
def _refused(world: dict[str, Any]) -> None:
    assert isinstance(world["refusal"], ReservedProgressField)


@given("an enqueued WorkUnit whose start raises")
def _enqueued_and_broken(world: dict[str, Any], work_unit_ledger: Path, start_raises: None) -> None:
    world["work_unit_id"] = _enqueued_work_unit()


@when("the outbox is drained")
def _drained(world: dict[str, Any]) -> None:
    world["outcomes"] = drain_enqueue_outbox()


@when(parsers.parse("the drainer polls {count:d} times"))
def _polled(world: dict[str, Any], count: int) -> None:
    drainer = EnqueueDrainer()
    for _ in range(count):
        world["outcome"] = drainer.poll_once()


@when("the drain command runs")
def _drain_command(world: dict[str, Any]) -> None:
    world["result"] = work_unit_commands.drain_work_unit_enqueues()


@then("the drain reports one failed delivery")
def _one_failure(world: dict[str, Any]) -> None:
    failed = [item for item in world["outcomes"] if isinstance(item, EnqueueFailed)]
    assert [item.work_unit_id for item in failed] == [world["work_unit_id"]]


@then("the failure names why")
def _failure_names_why(world: dict[str, Any]) -> None:
    failed = next(item for item in world["outcomes"] if isinstance(item, EnqueueFailed))
    assert str(BOOM) in failed.failure.message
    assert failed.failure.exception_type == "RuntimeError"


@then("it reports itself stalled")
def _stalled(world: dict[str, Any]) -> None:
    assert isinstance(world["outcome"], Stalled)


@then("the stall names the rows that raised")
def _stall_names(world: dict[str, Any]) -> None:
    outcome = world["outcome"]
    assert isinstance(outcome, Stalled)
    assert [item.work_unit_id for item in outcome.failed] == [world["work_unit_id"]]


@then("the command reports failure")
def _command_failed(world: dict[str, Any]) -> None:
    assert world["result"]["ok"] is False
    assert world["result"]["error"] == "enqueue_delivery_failed"


# --- unit tests: one per decision variable on the reporting path --------------


# Variable 1: whether the computed sentence reaches the log.
def test_the_operator_sentence_reaches_the_formatted_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The defect, as an assertion.

    Eleven call sites computed a sentence and the log call passed the literal
    `dispatch_progress`, so every line said the same word. The sentence survived
    only in the in-process terminal event, which the resident loops never have.
    """

    with caplog.at_level(logging.INFO):
        emit_progress("planning intent abc for target portfolio", phase="dispatch_planning")

    payload = _formatted(_progress_record(caplog))
    assert payload["detail"] == "planning intent abc for target portfolio"


def test_the_message_stays_the_stable_aggregation_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The sentence is a body field, not a label.

    `dispatch_progress` is what everything groups on, and free-form model text
    must never become a Loki label; `stage.labels` in the Alloy config is where
    that boundary is kept, and it is untouched.
    """

    with caplog.at_level(logging.INFO):
        emit_progress("something happened", phase="dispatch_planning")

    assert _formatted(_progress_record(caplog))["msg"] == "dispatch_progress"


# Variable 2: whether the event carries risks.
def test_a_failed_task_s_risks_reach_the_log(caplog: pytest.LogCaptureFixture) -> None:
    """`401 Not logged in` reached only `agent_execution_leases.result_json`."""

    with caplog.at_level(logging.INFO):
        emit_progress(
            "analyst turn junior_context finished failed",
            phase="task_completed",
            status="failed",
            risks=("401 Not logged in", "the model was never reached"),
        )

    payload = _formatted(_progress_record(caplog))
    assert payload["risks"] == "401 Not logged in; the model was never reached"


def test_a_successful_task_carries_no_risks_field(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        emit_progress("analyst turn finished completed", phase="task_completed", status="completed")

    assert "risks" not in _formatted(_progress_record(caplog))


def test_the_terminal_event_keeps_the_risks_as_a_list() -> None:
    """The SSE projection is structured; only the log payload is flat."""

    events: list[dict[str, Any]] = []
    with progress_event_sink(events.append):
        emit_progress("a turn failed", phase="task_completed", risks=("first", "second"))

    assert events[0]["risks"] == ["first", "second"]
    assert events[0]["message"] == "a turn failed"


# Variable 3: whether a field collides with a LogRecord attribute.
@pytest.mark.parametrize("field", ["module", "name", "args", "msg", "filename"])
def test_a_reserved_field_name_is_refused(field: str) -> None:
    """`makeRecord` raises `KeyError` on these, which is a crash in a dispatch loop.

    Raising here names the mistake; the alternative, dropping it, would make a
    field that looks emitted and is not.

    `message` itself is absent from this list because the signature already owns
    that name: `emit_progress("x", message="y")` is a `TypeError` from Python
    before this check runs. It stays in the frozenset anyway, because the check
    is about what reaches `makeRecord` and not about this one signature.
    """

    with pytest.raises(ReservedProgressField, match=field):
        emit_progress("a message", phase="p", **{field: "value"})


def test_an_ordinary_field_is_not_refused(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        emit_progress("a message", phase="p", intent_id="intent-1")

    assert _formatted(_progress_record(caplog))["intent_id"] == "intent-1"


# Variable 4: whether the field survives the formatter's allowlist.
def test_a_field_outside_the_allowlist_is_dropped(caplog: pytest.LogCaptureFixture) -> None:
    """Named so the drop is a decision rather than a surprise.

    `tier`, `role`, and `pow_wow_id` are attached to the record by callers and
    then removed by the formatter. That is the design - the allowlist is what
    keeps the log's shape stable - but nothing said so, and it is why adding a
    field to `emit_progress` is not enough on its own.
    """

    with caplog.at_level(logging.INFO):
        emit_progress("a message", phase="p", tier="junior")

    record = _progress_record(caplog)
    assert record.tier == "junior"  # type: ignore[attr-defined]
    assert "tier" not in _formatted(record)


def test_the_two_new_fields_are_inside_the_allowlist(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO):
        emit_progress("the sentence", phase="p", risks=("the reason",))

    payload = _formatted(_progress_record(caplog))
    assert payload["detail"] == "the sentence"
    assert payload["risks"] == "the reason"


# Variable 5: whether a drained row raised.
def test_a_row_that_raises_is_a_failed_outcome(work_unit_ledger: Path, start_raises: None) -> None:
    """It used to be nothing at all: caught, counted, and `continue`d."""

    work_unit_id = _enqueued_work_unit()

    outcomes = drain_enqueue_outbox()

    assert [type(item) for item in outcomes] == [EnqueueFailed]
    failed = outcomes[0]
    assert isinstance(failed, EnqueueFailed)
    assert failed.work_unit_id == work_unit_id
    assert failed.attempts == 1


def test_a_row_that_raises_still_stays_pending(work_unit_ledger: Path, start_raises: None) -> None:
    """Retry-forever on PENDING is the existing semantic; reporting it is new."""

    _enqueued_work_unit()
    drain_enqueue_outbox()

    assert len(repo.list_pending_enqueues()) == 1


def test_a_row_that_raises_is_logged_with_its_failure_dimensions(
    work_unit_ledger: Path, start_raises: None, caplog: pytest.LogCaptureFixture
) -> None:
    """This module had no logger at all, which is how the swallow left no trace."""

    _enqueued_work_unit()
    with caplog.at_level(logging.ERROR):
        drain_enqueue_outbox()

    record = next(record for record in caplog.records if record.msg == "work_unit_enqueue_failed")
    payload = _formatted(record)
    assert payload["operation"] == "drain_enqueue_outbox"
    assert str(BOOM) in payload["detail"]


def test_a_deferred_row_is_not_a_failed_row(work_unit_ledger: Path) -> None:
    """`delivered: False` with no runtime is a real answer, not a failure."""

    _enqueued_work_unit()

    outcomes = drain_enqueue_outbox()

    assert [type(item) for item in outcomes] == [EnqueueSettled]
    settled = outcomes[0]
    assert isinstance(settled, EnqueueSettled)
    assert settled.delivered is False


# Variable 6: how many consecutive passes raised.
def test_a_pass_of_crashing_rows_does_not_read_as_idle(
    work_unit_ledger: Path, start_raises: None
) -> None:
    """The downstream half of the swallow.

    A row that raised was in neither the delivered nor the undeliverable list, so
    `poll_once` reset its stall counter and returned `Idle`. A drainer that could
    never deliver anything looked like a quiet queue.
    """

    _enqueued_work_unit()
    drainer = EnqueueDrainer()

    outcome = drainer.poll_once()

    assert isinstance(outcome, Idle)
    assert outcome.undeliverable  # counted, not silently dropped
    assert drainer.consecutive_undeliverable_passes == 1


def test_repeated_passes_of_crashing_rows_escalate_to_stalled(
    work_unit_ledger: Path, start_raises: None
) -> None:
    work_unit_id = _enqueued_work_unit()
    drainer = EnqueueDrainer()

    for _ in range(3):
        outcome = drainer.poll_once()

    assert isinstance(outcome, Stalled)
    assert [item.work_unit_id for item in outcome.failed] == [work_unit_id]


# Variable 7: which consumer is reading the outcome.
def test_the_drain_command_reports_failure_rather_than_an_empty_success(
    work_unit_ledger: Path, start_raises: None
) -> None:
    """It used to answer `{"ok": true, "outcomes": []}` and exit zero."""

    _enqueued_work_unit()

    result = work_unit_commands.drain_work_unit_enqueues()

    assert result["ok"] is False
    assert result["error"] == "enqueue_delivery_failed"
    assert str(BOOM) in result["message"]


def test_starting_a_work_unit_reports_a_delivery_that_raised(
    work_unit_ledger: Path, start_raises: None
) -> None:
    """A crashed inline start returned a success payload with an empty dispatch."""

    from local_first_agent_os.work_units import service

    compiled = compile_acceptance_doc(design_doc_id="surfaced_failures_start")
    assert compiled.compiled_plan_revision_id is not None

    payload = service.start_work_unit(
        compiled.compiled_plan_revision_id, delivery=EnqueueDelivery.DURABLE
    )

    assert payload["dispatch_failed"]
    assert payload["dispatch_failed"][0]["failure"]["message"] == str(BOOM)


def test_a_clean_drain_command_still_succeeds(work_unit_ledger: Path) -> None:
    _enqueued_work_unit()

    result = work_unit_commands.drain_work_unit_enqueues()

    assert result["ok"] is True
    assert [item["delivered"] for item in result["outcomes"]] == [False]
