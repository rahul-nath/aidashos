# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The execution ledger returns tallies, and cannot be made to return payloads."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from local_first_agent_os.coordination.execution_ledger import (
    read_execution_ledger as read_execution_ledger_command,
)
from local_first_agent_os.durable_execution_ledger import (
    PAYLOAD_BEARING_COLUMNS,
    STEP_TALLY_SQL,
    WORKFLOW_TALLY_SQL,
    ExecutionLedger,
    LedgerUnavailable,
    build_execution_ledger,
    ledger_reader_url,
    read_execution_ledger,
)
from local_first_agent_os.settings import Settings

GRANT_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "grant_execution_ledger_reader.sql"


def selected_columns(statement: str) -> str:
    """The projection clause alone.

    Asserting against the whole statement would be wrong rather than merely
    imprecise: `dbos.operation_outputs` contains the substring "output", so a
    naive check fails on a statement that reads no payload at all.
    """

    projection = statement.lower().split(" from ", 1)[0]
    return projection.removeprefix("select ")


@pytest.mark.parametrize("statement", [WORKFLOW_TALLY_SQL, STEP_TALLY_SQL])
@pytest.mark.parametrize("column", PAYLOAD_BEARING_COLUMNS)
def test_no_statement_selects_a_payload_bearing_column(statement: str, column: str) -> None:
    """The projection is closed by inspection, and stays closed by this test.

    The module's whole safety claim is that a workflow's inputs cannot leave it.
    That claim rests on the text of two statements, so the text is what is
    asserted; anything wider would have to change these constants first.
    """

    assert not re.search(rf"\b{column}\b", selected_columns(statement))


def test_the_projections_are_exactly_the_granted_columns() -> None:
    """Pin what each statement selects, so a widening is a failing diff.

    The test above rules out four names. This rules in the only three there are,
    which is the property the column grant enforces at the other end.
    """

    assert selected_columns(WORKFLOW_TALLY_SQL) == "name, status, count(*) as execution_count"
    assert selected_columns(STEP_TALLY_SQL) == "function_name, count(*) as execution_count"


@pytest.mark.parametrize("statement", [WORKFLOW_TALLY_SQL, STEP_TALLY_SQL])
def test_no_statement_interpolates(statement: str) -> None:
    """No placeholder, no format field: a caller cannot widen either statement."""

    assert "%" not in statement
    assert "{" not in statement
    assert "?" not in statement


def test_the_grant_script_grants_only_the_columns_the_statements_read() -> None:
    """The database-level privilege matches the code-level projection.

    Two enforcement points that disagree would be worse than one: the narrower
    would break reads the wider allows, and nobody would know which was intended.
    """

    grant_sql = GRANT_SCRIPT.read_text(encoding="utf-8")
    assert "GRANT SELECT (name, status) ON dbos.workflow_status" in grant_sql
    assert "GRANT SELECT (function_name) ON dbos.operation_outputs" in grant_sql
    for column in PAYLOAD_BEARING_COLUMNS:
        assert f"GRANT SELECT ({column}" not in grant_sql


def test_rows_become_tallies() -> None:
    ledger = build_execution_ledger(
        workflow_rows=[
            ("durable_workflow_entrypoint", "SUCCESS", 330),
            ("durable_workflow_entrypoint", "ERROR", 5),
        ],
        step_rows=[("Repository.register_ingress_event", 342)],
    )
    assert ledger.workflows[0].workflow_name == "durable_workflow_entrypoint"
    assert ledger.workflows[0].execution_count == 330
    assert ledger.steps[0].step_name == "Repository.register_ingress_event"
    assert ledger.steps[0].execution_count == 342


def test_a_workflow_that_only_errored_still_executed() -> None:
    """`has_ever_executed` answers about execution, not about success.

    The question it exists for is "has this code path ever run at all", which a
    failed run answers yes to. A caller wanting successes asks `tallies_for`.
    """

    ledger = build_execution_ledger(
        workflow_rows=[("execute_work_unit", "ERROR", 2)],
        step_rows=[],
    )
    assert ledger.has_ever_executed("execute_work_unit") is True
    assert ledger.has_ever_executed("run_phase") is False


def test_tallies_for_narrows_to_one_workflow() -> None:
    ledger = build_execution_ledger(
        workflow_rows=[
            ("durable_workflow_entrypoint", "SUCCESS", 330),
            ("durable_session_item_entrypoint", "SUCCESS", 106),
        ],
        step_rows=[],
    )
    narrowed = ledger.tallies_for("durable_session_item_entrypoint")
    assert [tally.workflow_name for tally in narrowed] == ["durable_session_item_entrypoint"]


def test_the_reader_url_prefers_the_least_privileged_one() -> None:
    """The restricted role wins whenever it is configured.

    A process handed both should use the one Postgres would stop, not the one it
    would not.
    """

    settings = Settings(
        ledger_reader_database_url="postgresql://reader@127.0.0.1:5432/local_agent_dbos",
        dbos_system_database_url="postgresql://postgres@127.0.0.1:5432/local_agent_dbos",
    )
    assert ledger_reader_url(settings) == "postgresql://reader@127.0.0.1:5432/local_agent_dbos"


def test_the_admin_url_is_the_fallback() -> None:
    settings = Settings(
        dbos_system_database_url="postgresql://postgres@127.0.0.1:5432/local_agent_dbos"
    )
    assert ledger_reader_url(settings) == "postgresql://postgres@127.0.0.1:5432/local_agent_dbos"


def test_an_unconfigured_ledger_is_a_value_not_an_exception() -> None:
    """No configured database is a condition an operator fixes, so it is reported.

    Distinguishing this from a programmer error matters: the caller can print the
    reason and carry on, which a traceback out of a read-only query would not let
    it do.
    """

    settings = Settings(dbos_system_database_url=None, ledger_reader_database_url=None)
    reading = read_execution_ledger(settings)
    assert isinstance(reading, LedgerUnavailable)
    assert "LOCAL_AGENT_LEDGER_READER_DATABASE_URL" in reading.reason


def test_the_command_reports_an_unavailable_ledger_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "local_first_agent_os.coordination.execution_ledger.read_ledger",
        lambda: LedgerUnavailable(reason="the DBOS system database is not accepting connections"),
    )
    result = read_execution_ledger_command()
    assert result["ok"] is False
    assert result["error"] == "execution_ledger_unavailable"
    assert "not accepting connections" in result["message"]


def test_the_command_answers_has_ever_executed_for_a_named_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = build_execution_ledger(
        workflow_rows=[("durable_workflow_entrypoint", "SUCCESS", 330)],
        step_rows=[("ArtifactStore.write_json", 337)],
    )
    monkeypatch.setattr(
        "local_first_agent_os.coordination.execution_ledger.read_ledger",
        lambda: ledger,
    )

    present = read_execution_ledger_command("durable_workflow_entrypoint")
    assert present["ok"] is True
    assert present["has_ever_executed"] is True
    assert present["workflows"][0]["execution_count"] == 330

    absent = read_execution_ledger_command("execute_work_unit")
    assert absent["ok"] is True
    assert absent["has_ever_executed"] is False
    assert absent["workflows"] == []
    # Steps stay whole on a miss: they are how a reader tells "never ran" from
    # "ran under a name I guessed wrong".
    assert absent["steps"][0]["step_name"] == "ArtifactStore.write_json"


def test_the_command_returns_the_whole_ledger_when_unnarrowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "local_first_agent_os.coordination.execution_ledger.read_ledger",
        lambda: build_execution_ledger(
            workflow_rows=[("durable_workflow_entrypoint", "SUCCESS", 330)],
            step_rows=[("ModelManager.call_model", 34)],
        ),
    )
    result = read_execution_ledger_command()
    assert result["ok"] is True
    assert result["workflows"][0]["workflow_name"] == "durable_workflow_entrypoint"
    assert result["steps"][0]["step_name"] == "ModelManager.call_model"
    assert "has_ever_executed" not in result


def test_a_tally_carries_no_field_a_payload_could_hide_in() -> None:
    """The published shape is three scalars, so there is nowhere to put content.

    A dict-shaped row would have let a widened SELECT flow straight through to a
    caller. These dataclasses are what make that impossible rather than merely
    absent today.
    """

    ledger = build_execution_ledger(
        workflow_rows=[("durable_workflow_entrypoint", "SUCCESS", 1)],
        step_rows=[("ArtifactStore.write_json", 1)],
    )
    assert isinstance(ledger, ExecutionLedger)
    assert set(ledger.workflows[0].to_payload()) == {
        "workflow_name",
        "status",
        "execution_count",
    }
    assert set(ledger.steps[0].to_payload()) == {"step_name", "execution_count"}
