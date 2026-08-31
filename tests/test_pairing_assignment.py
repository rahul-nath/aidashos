# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from local_first_agent_os.coordination.dispatch import submit_dispatch_intent
from local_first_agent_os.pairing_assignment import (
    PairingAssignment,
    assignment_for_intent,
    bench_for_assignment,
)
from local_first_agent_os.pairing_lattice import Pairing, ScoredModel
from local_first_agent_os.staffing import Harness, load_bench
from local_first_agent_os.vocabulary import DispatchTier


def _assignment() -> PairingAssignment:
    senior = ScoredModel(
        harness=Harness.CLAUDE,
        model="claude-opus-5",
        quality=63,
        vendor="anthropic",
        reasoning_effort="xhigh",
    )
    staff = ScoredModel(
        harness=Harness.CODEX,
        model="gpt-5.6-sol",
        quality=65,
        vendor="openai",
        reasoning_effort="max",
    )
    return PairingAssignment(
        assignment_id="pa_test",
        work_unit_id="wu-test",
        milestone_key="implement",
        attempt=1,
        chart_hash="chart-hash",
        pairing=Pairing(senior=senior, staff=staff, score=131, cross_vendor=True),
        probed=(senior.label, staff.label),
    )


def test_intent_and_assignment_are_one_transaction(work_unit_ledger: Path) -> None:
    assignment = _assignment()

    submitted = submit_dispatch_intent(
        "senior",
        "implement",
        kind="code",
        target_project_id="local_first_agent_os",
        source="work_unit:wu-test:milestone_execution:implement",
        idempotency_key="wu-test:implement:1",
        pairing_assignment=assignment.to_payload(),
    )

    assert submitted["ok"] is True
    assert assignment_for_intent(str(submitted["intent_id"])) == assignment


def test_replaying_an_intent_cannot_change_its_pair(work_unit_ledger: Path) -> None:
    assignment = _assignment()
    first = submit_dispatch_intent(
        "senior",
        "implement",
        kind="code",
        target_project_id="local_first_agent_os",
        idempotency_key="wu-test:implement:replay",
        pairing_assignment=assignment.to_payload(),
    )

    changed = replace(assignment, assignment_id="pa_conflict", chart_hash="other")
    try:
        submit_dispatch_intent(
            "senior",
            "implement",
            kind="code",
            target_project_id="local_first_agent_os",
            idempotency_key="wu-test:implement:replay",
            pairing_assignment=changed.to_payload(),
        )
    except RuntimeError as exc:
        assert "different pairing assignment" in str(exc)
    else:
        raise AssertionError("a replay changed the pair attached to the incumbent intent")
    assert assignment_for_intent(str(first["intent_id"])) == assignment


def test_assignment_binds_both_judgment_seats_exactly() -> None:
    configured = load_bench(Path(__file__).parent.parent / "configs" / "staffing.toml")
    assignment = _assignment()

    bound = bench_for_assignment(configured, assignment)

    assert bound[DispatchTier.SENIOR].harness is Harness.CLAUDE
    assert bound[DispatchTier.SENIOR].model == "claude-opus-5"
    assert bound[DispatchTier.SENIOR].reasoning_effort == "xhigh"
    assert bound[DispatchTier.STAFF].harness is Harness.CODEX
    assert bound[DispatchTier.STAFF].model == "gpt-5.6-sol"
    assert bound[DispatchTier.STAFF].reasoning_effort == "max"
    assert bound[DispatchTier.JUNIOR] == configured[DispatchTier.JUNIOR]
