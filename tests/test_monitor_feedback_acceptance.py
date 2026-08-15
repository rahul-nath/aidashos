# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Acceptance tests for the monitor feedback reactor.

These drive the real ``CoordinationReactorLedger`` against a disposable SQLite
ledger, not a fake.  The point of these scenarios is the SQL and the ordering
between submitting an intent and committing a decision, and a fake ledger would
assert neither.

Unit-level behavior of the pure decision core lives in
``test_monitor_feedback_decisions.py``; catalog validation lives in
``test_monitor_feedback_rules.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from local_first_agent_os.coordination import store
from local_first_agent_os.coordination.monitor_feedback import (
    CoordinationReactorLedger,
    feedback_intent_source,
    list_monitor_feedback_events,
)
from local_first_agent_os.coordination.store import connect, now, set_root, tx
from local_first_agent_os.monitor_feedback import load_feedback_rules, run_feedback_cycle
from local_first_agent_os.monitor_feedback.reactor import DryRunReactorLedger

REPO_ROOT = Path(__file__).resolve().parent.parent

scenarios("features/monitor_feedback_reactor.feature")


@dataclass
class ReactorWorld:
    """Scenario state. One per scenario, never shared."""

    catalog: Any = None
    ledger: CoordinationReactorLedger = field(default_factory=CoordinationReactorLedger)
    report: dict[str, Any] = field(default_factory=dict)
    saga_id: str = ""
    milestone_ids: list[str] = field(default_factory=list)


@pytest.fixture()
def world(tmp_path: Path) -> ReactorWorld:
    return ReactorWorld()


@pytest.fixture(autouse=True)
def _disposable_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the coordination store at a throwaway root for each scenario.

    The operator ledger has been polluted by test writes before; this fixture
    is the reason it cannot happen from here.
    """

    root = tmp_path / "coordination"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store, "ROOT_OVERRIDE", None, raising=False)
    set_root(str(root))
    store._SCHEMA_READY.clear()
    yield root
    set_root(None)
    store._SCHEMA_READY.clear()


def _insert_saga(saga_id: str, project_id: str) -> None:
    t = now()
    with tx() as c:
        c.execute(
            "INSERT INTO sagas(saga_id, goal, current_stage, status, created_at, updated_at) "
            "VALUES (?, ?, 'IDEA_INTAKE', 'PLANNING', ?, ?)",
            (saga_id, f"acceptance saga for {project_id}", t, t),
        )


def _insert_failed_milestone(
    saga_id: str,
    project_id: str,
    outcome: str,
    *,
    intent_source: str | None = None,
) -> str:
    """Create a failed milestone and the dispatch intent it came from.

    The intent matters: the milestone alone carries no project id and no
    lineage, and both reach the signal through that join.
    """

    milestone_id = str(uuid4())
    intent_id = str(uuid4())
    t = now()
    with tx() as c:
        c.execute(
            "INSERT INTO dispatch_intents(intent_id, tier, kind, prompt, target_project_id, "
            "source, status, created_at, completed_at, outcome) "
            "VALUES (?, 'senior', 'code', 'build it', ?, ?, 'FAILED', ?, ?, ?)",
            (
                intent_id,
                project_id,
                intent_source or f"approved_gawd:{uuid4().hex}",
                t,
                t,
                outcome,
            ),
        )
        c.execute(
            "INSERT INTO saga_milestones(milestone_id, saga_id, sequence, name, "
            "approval_required, dispatch_intent_id, status, created_at, updated_at, outcome) "
            "VALUES (?, ?, 1, 'build the thing', 0, ?, 'FAILED', ?, ?, ?)",
            (milestone_id, saga_id, intent_id, t, t, outcome),
        )
    return milestone_id


def _run_cycle(world: ReactorWorld, ledger: Any) -> dict[str, Any]:
    world.report = run_feedback_cycle(ledger, world.catalog, now=now())
    return world.report


def _pending_advisory_intents() -> list[dict[str, Any]]:
    with connect() as c:
        return [
            dict(row)
            for row in c.execute(
                "SELECT intent_id, source, prompt, tier FROM dispatch_intents "
                "WHERE status = 'PENDING' AND kind = 'advisory'"
            ).fetchall()
        ]


def _feedback_events(decision: str | None = None) -> list[dict[str, Any]]:
    with connect() as c:
        if decision is None:
            rows = c.execute("SELECT * FROM monitor_feedback_events").fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM monitor_feedback_events WHERE decision = ?", (decision,)
            ).fetchall()
    return [dict(row) for row in rows]


# --- Given -------------------------------------------------------------------


@given("a disposable coordination ledger")
def _given_ledger() -> None:
    # Touch the store so the schema is applied before any scenario step runs.
    with connect() as c:
        c.execute("SELECT 1").fetchone()


@given("the starter feedback rule catalog")
def _given_catalog(world: ReactorWorld) -> None:
    world.catalog = load_feedback_rules(REPO_ROOT / "configs" / "feedback_rules.toml")


@given("a catalog that proposes advisory work for dispatch intent failures")
def _given_advisory_intent_catalog(world: ReactorWorld, tmp_path: Path) -> None:
    """Override the shipped catalog so the lineage gate is reachable.

    The shipped catalog routes DISPATCH_INTENT_FAILED to digest_only, and
    digest_only bypasses every gate because it creates no work. Lineage only
    bites when an operator points *advisory* work at a kind the reactor's own
    dispatches can produce, so that is the catalog this scenario needs.
    """

    path = tmp_path / "lineage_rules.toml"
    path.write_text(
        """
schema_version = "feedback_rules.v1"
global_open_intent_cap = 10

[[rule]]
rule_id = "intent_failure_diagnosis"
response = "advisory"
tier = "junior"
daily_cap = 5
prompt_template = "diagnose {evidence_table} {evidence_row_id}"

  [rule.selector]
  signal_kind = "DISPATCH_INTENT_FAILED"
""",
        encoding="utf-8",
    )
    world.catalog = load_feedback_rules(path)


@given(parsers.parse('a saga "{project_id}" with a milestone that failed with "{outcome}"'))
def _given_failed_milestone(world: ReactorWorld, project_id: str, outcome: str) -> None:
    world.saga_id = str(uuid4())
    _insert_saga(world.saga_id, project_id)
    world.milestone_ids = [_insert_failed_milestone(world.saga_id, project_id, outcome)]


@given(
    parsers.parse('a saga "{project_id}" with {count:d} milestones that failed with "{outcome}"')
)
def _given_many_failed_milestones(
    world: ReactorWorld, project_id: str, count: int, outcome: str
) -> None:
    world.saga_id = str(uuid4())
    _insert_saga(world.saga_id, project_id)
    world.milestone_ids = [
        _insert_failed_milestone(world.saga_id, project_id, outcome) for _ in range(count)
    ]


@given("the reactor has run one cycle")
def _given_one_cycle(world: ReactorWorld) -> None:
    _run_cycle(world, world.ledger)


@given("the reactor crashed after submitting its intent but before committing")
def _given_crashed_cycle(world: ReactorWorld) -> None:
    """Submit the intent, then abandon the cycle before any row is written.

    This is the ordering the design chose deliberately: an intent with no
    decision row is recoverable because dedup sees it, while a decision row
    with no intent would be a proposal that never became work.
    """

    class _CrashingLedger(CoordinationReactorLedger):
        def commit_cycle(self, outcomes: Any, watermarks: Any) -> None:
            raise RuntimeError("ledger went away mid-cycle")

    with pytest.raises(RuntimeError, match="ledger went away"):
        run_feedback_cycle(_CrashingLedger(), world.catalog, now=now())


# --- When --------------------------------------------------------------------


@when("the reactor runs one cycle")
def _when_cycle(world: ReactorWorld) -> None:
    _run_cycle(world, world.ledger)


@when("the reactor runs one dry-run cycle")
def _when_dry_run(world: ReactorWorld) -> None:
    _run_cycle(world, DryRunReactorLedger(world.ledger))


@when(parsers.parse('the milestone fails again with "{outcome}"'))
def _when_fails_again(world: ReactorWorld, outcome: str) -> None:
    """Re-fail the same milestone row, advancing only its timestamp.

    Updating in place rather than inserting a new row is what makes this a
    retry of one condition rather than a second condition.
    """

    with tx() as c:
        c.execute(
            "UPDATE saga_milestones SET outcome = ?, updated_at = ? WHERE milestone_id = ?",
            (outcome, now() + 1, world.milestone_ids[0]),
        )


@when(parsers.parse('the proposed intent itself fails with "{outcome}"'))
def _when_feedback_intent_fails(world: ReactorWorld, outcome: str) -> None:
    """Fail the reactor's own dispatched diagnosis task.

    Its failure is a genuine ledger fact and a rule matches it, so only the
    lineage gate stops the loop from diagnosing its own diagnosis.
    """

    proposed = _pending_advisory_intents()
    assert proposed, "expected the previous cycle to have proposed an intent"
    with tx() as c:
        c.execute(
            "UPDATE dispatch_intents SET status = 'FAILED', outcome = ?, completed_at = ? "
            "WHERE intent_id = ?",
            (outcome, now() + 1, proposed[0]["intent_id"]),
        )


# --- Then --------------------------------------------------------------------


@then(parsers.parse("exactly {count:d} advisory dispatch intent is PENDING"))
@then(parsers.parse("exactly {count:d} advisory dispatch intents are PENDING"))
def _then_pending_intents(count: int) -> None:
    pending = _pending_advisory_intents()
    assert len(pending) == count, f"expected {count} pending advisory intents, got {pending}"


@then(parsers.parse("exactly {count:d} advisory dispatch intent exists in total"))
@then(parsers.parse("exactly {count:d} advisory dispatch intents exist in total"))
def _then_total_advisory_intents(count: int) -> None:
    """Terminal state is irrelevant here; creation is the thing being bounded."""

    with connect() as c:
        total = len(
            c.execute("SELECT intent_id FROM dispatch_intents WHERE kind = 'advisory'").fetchall()
        )
    assert total == count, f"expected {count} advisory intents in total, got {total}"


@then("the proposed intent carries the feedback source for that milestone")
def _then_source(world: ReactorWorld) -> None:
    pending = _pending_advisory_intents()
    assert len(pending) == 1
    events = _feedback_events("PROPOSED")
    assert len(events) == 1
    expected = feedback_intent_source(str(events[0]["rule_id"]), str(events[0]["fingerprint"]))
    assert pending[0]["source"] == expected


@then("the proposed intent names the failing evidence row in its prompt")
def _then_prompt_evidence(world: ReactorWorld) -> None:
    pending = _pending_advisory_intents()
    assert len(pending) == 1
    prompt = str(pending[0]["prompt"])
    assert world.milestone_ids[0] in prompt
    assert "saga_milestones" in prompt


@then(parsers.parse('exactly {count:d} feedback event is recorded with decision "{decision}"'))
@then(parsers.parse('exactly {count:d} feedback events are recorded with decision "{decision}"'))
def _then_events(count: int, decision: str) -> None:
    events = _feedback_events(decision)
    assert len(events) == count, (
        f"expected {count} {decision} events, got {[e['decision'] for e in _feedback_events()]}"
    )


@then(parsers.parse("exactly {count:d} feedback event is recorded"))
@then(parsers.parse("exactly {count:d} feedback events are recorded"))
def _then_event_total(count: int) -> None:
    assert len(_feedback_events()) == count


@then(parsers.parse('the report counted {count:d} decision of "{decision}"'))
def _then_report(world: ReactorWorld, count: int, decision: str) -> None:
    assert world.report["decisions"][decision] == count


def test_the_operator_view_lists_decisions_newest_first(world: ReactorWorld) -> None:
    """`list_monitor_feedback_events` is what a human reads to audit the loop.

    Every other assertion here inspects the tables directly, so the reader the
    operator actually uses was the one surface no test exercised.
    """

    world.catalog = load_feedback_rules(REPO_ROOT / "configs" / "feedback_rules.toml")
    world.saga_id = str(uuid4())
    _insert_saga(world.saga_id, "pest_site_factory")
    _insert_failed_milestone(world.saga_id, "pest_site_factory", "PROCESS_FAILED")
    _run_cycle(world, world.ledger)

    events = list_monitor_feedback_events()

    assert events, "a cycle that proposed work recorded no operator-visible event"
    assert {"fingerprint", "signal_kind", "decision", "rule_id"} <= set(events[0])
    created = [event["created_at"] for event in events]
    assert created == sorted(created, reverse=True)


def test_the_operator_view_honors_its_limit(world: ReactorWorld) -> None:
    world.catalog = load_feedback_rules(REPO_ROOT / "configs" / "feedback_rules.toml")
    world.saga_id = str(uuid4())
    _insert_saga(world.saga_id, "pest_site_factory")
    for _ in range(3):
        _insert_failed_milestone(world.saga_id, "pest_site_factory", "PROCESS_FAILED")
    _run_cycle(world, world.ledger)

    assert len(list_monitor_feedback_events(limit=1)) <= 1
