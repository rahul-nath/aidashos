# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest
from typer.testing import CliRunner

from local_first_agent_os.cli import app
from local_first_agent_os.coordination.dispatch_diagnostics import (
    record_dispatch_contract_violation,
)
from local_first_agent_os.coordination.monitor_feedback import (
    CoordinationReactorLedger,
    list_monitor_feedback_events,
)
from local_first_agent_os.coordination.store import connect, now, tx
from local_first_agent_os.dispatch_contracts import DispatchContractCode, InvalidDispatchReport
from local_first_agent_os.dispatch_results import decode_dispatch_runner_result
from local_first_agent_os.monitor_feedback.reactor import decide_cycle, run_feedback_cycle
from local_first_agent_os.monitor_feedback.rules import (
    FeedbackRuleCatalog,
    RuleSelector,
    load_feedback_rules,
)
from local_first_agent_os.monitor_feedback.signals import LedgerFactKind

_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "feedback_rules.toml"


@pytest.mark.parametrize("invalid_input", ["cyclic", "too_deep", "surrogate"])
def test_invalid_recursive_or_unicode_input_still_reaches_the_operator_alert(
    work_unit_ledger: Path, invalid_input: str
) -> None:
    payload: object
    if invalid_input == "cyclic":
        cyclic: dict[str, object] = {}
        cyclic["cycle"] = cyclic
        payload = cyclic
    elif invalid_input == "too_deep":
        nested: dict[str, object] = {}
        for _ in range(sys.getrecursionlimit() + 1):
            nested = {"nested": nested}
        payload = nested
    else:
        payload = "\ud800"
    diagnostic = decode_dispatch_runner_result(
        intent_result=None, approval_payload={"dispatch_result": payload}
    )
    assert isinstance(diagnostic, InvalidDispatchReport)
    if invalid_input != "too_deep":
        assert diagnostic.payload_sha256 is None
    # The JSON encoder may still hash depths the schema validator refuses.
    # Either digest variant must preserve the typed diagnostic and alert.
    with tx() as connection:
        record_dispatch_contract_violation(
            connection, intent_id="unhashable-intent", diagnostic=diagnostic
        )
    report = run_feedback_cycle(
        CoordinationReactorLedger(), load_feedback_rules(_CONFIG), now=now()
    )
    assert report["decisions"]["ESCALATED_DIGEST"] == 1
    assert report["proposed_intent_ids"] == []
    assert list_monitor_feedback_events()[0]["severity"] == "CRITICAL"


def test_public_monitor_cycle_escalates_contract_diagnostic_once(work_unit_ledger: Path) -> None:
    diagnostic = InvalidDispatchReport.from_input(
        DispatchContractCode.MALFORMED_JSON, '{"secret":"do-not-repeat",broken'
    )
    with tx() as connection:
        event_id = record_dispatch_contract_violation(
            connection, intent_id="ledger-owned-intent", diagnostic=diagnostic
        )
    runner = CliRunner()
    first = runner.invoke(app, ["monitor-feedback"])
    assert first.exit_code == 0, first.output
    report = json.loads(first.output)
    assert report["decisions"]["ESCALATED_DIGEST"] == 1
    assert report["proposed_intent_ids"] == []
    events = list_monitor_feedback_events()
    assert len(events) == 1
    assert events[0]["signal_kind"] == "DISPATCH_CONTRACT_VIOLATION"
    assert events[0]["severity"] == "CRITICAL"
    assert events[0]["decision"] == "ESCALATED_DIGEST"
    assert events[0]["evidence"] == {"table": "ledger_events", "row_id": event_id}
    assert events[0]["error_code"] == DispatchContractCode.MALFORMED_JSON.value
    assert "do-not-repeat" not in json.dumps(events)
    second = runner.invoke(app, ["monitor-feedback"])
    assert second.exit_code == 0, second.output
    assert json.loads(second.output)["signals_evaluated"] == 0
    assert len(list_monitor_feedback_events()) == 1


def test_contract_alert_cannot_be_turned_into_automatic_work_by_a_broad_rule(
    work_unit_ledger: Path,
) -> None:
    diagnostic = InvalidDispatchReport.from_input(DispatchContractCode.INVALID_ENVELOPE, "broken")
    with tx() as connection:
        record_dispatch_contract_violation(
            connection, intent_id="ledger-owned-intent", diagnostic=diagnostic
        )
    starter = load_feedback_rules(_CONFIG)
    broad_advisory = replace(starter.rules[0], selector=RuleSelector())
    catalog = FeedbackRuleCatalog((broad_advisory,), 6, "test broad advisory")
    report = run_feedback_cycle(CoordinationReactorLedger(), catalog, now=now())
    assert report["decisions"]["ESCALATED_DIGEST"] == 1
    assert report["proposed_intent_ids"] == []
    with connect() as connection:
        assert not connection.execute("SELECT 1 FROM dispatch_intents").fetchall()


def test_late_diagnostic_commit_is_not_lost_behind_the_monitor_watermark(
    work_unit_ledger: Path,
) -> None:
    catalog = load_feedback_rules(_CONFIG)
    ledger = CoordinationReactorLedger()
    with tx() as connection:
        record_dispatch_contract_violation(
            connection,
            intent_id="newer-intent",
            diagnostic=InvalidDispatchReport.from_input(
                DispatchContractCode.INVALID_ENVELOPE, "newer"
            ),
        )
    assert run_feedback_cycle(ledger, catalog, now=now())["signals_evaluated"] == 1
    with tx() as connection:
        event_id = record_dispatch_contract_violation(
            connection,
            intent_id="late-intent",
            diagnostic=InvalidDispatchReport.from_input(
                DispatchContractCode.INVALID_ENVELOPE, "late"
            ),
        )
        connection.execute("UPDATE ledger_events SET created_at=1 WHERE event_id=?", (event_id,))
    assert run_feedback_cycle(ledger, catalog, now=now())["signals_evaluated"] == 1
    assert len(list_monitor_feedback_events()) == 2


@pytest.mark.parametrize("concurrent", [False, True])
def test_replayed_alert_projection_is_idempotent(work_unit_ledger: Path, concurrent: bool) -> None:
    catalog = load_feedback_rules(_CONFIG)
    ledger = CoordinationReactorLedger()
    with tx() as connection:
        record_dispatch_contract_violation(
            connection,
            intent_id="replayed-intent",
            diagnostic=InvalidDispatchReport.from_input(
                DispatchContractCode.INVALID_ENVELOPE, "broken"
            ),
        )
    snapshot = ledger.read_snapshot((LedgerFactKind.DISPATCH_CONTRACT_VIOLATION,))
    outcomes = decide_cycle(snapshot, catalog, now())
    watermark = {LedgerFactKind.DISPATCH_CONTRACT_VIOLATION: snapshot.signals[0].observed_at}
    if concurrent:
        barrier = Barrier(2)

        def commit() -> None:
            barrier.wait(timeout=10)
            ledger.commit_cycle(outcomes, watermark)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(commit) for _ in range(2)]
            for future in futures:
                future.result(timeout=20)
    else:
        ledger.commit_cycle(outcomes, watermark)
        ledger.commit_cycle(outcomes, watermark)
    assert len(list_monitor_feedback_events()) == 1
