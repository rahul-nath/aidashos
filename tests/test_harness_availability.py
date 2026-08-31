# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Staffing reading what recovery already learned.

Staffing was a config file read once at a door; recovery was a reaction to one
failure at a time, after the attempt was spent. Neither consulted the history
between them, so on 2026-08-06 codex reported `USAGE_LIMIT` at 03:04 and the
dispatch at 03:44 went to codex anyway. The loop was deterministic because its
inputs never changed no matter what happened.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from local_first_agent_os import harness_availability
from local_first_agent_os.coordination import execution as ledger_execution
from local_first_agent_os.coordination.availability import LedgerUnavailable
from local_first_agent_os.dispatcher_runner import DispatcherIntentRunner
from local_first_agent_os.harness_availability import (
    USAGE_LIMIT_COOLDOWN,
    check_harness_availability,
    collapsed_cross_checks,
    narrow_by_reported_failures,
    read_spent_quotas,
    recently_usage_limited,
    staffing_around_spent_quotas,
)
from local_first_agent_os.harness_readiness import (
    HarnessNotReady,
    HarnessReady,
    HarnessUnknown,
    TierRestaffed,
    TierServed,
    TierUnstaffable,
    effective_bench,
    plan_tier_staffing,
    restaffings,
)
from local_first_agent_os.runtime import AppRuntime
from local_first_agent_os.staffing import (
    DEFAULT_BENCH,
    Bench,
    BenchSlot,
    FrontierHarness,
    Harness,
)
from local_first_agent_os.vocabulary import DispatchTier

_NOW = datetime(2026, 8, 6, 8, 0, tzinfo=UTC)


def _lease(harness: str, *, ago: timedelta, failure: str = "USAGE_LIMIT") -> dict:
    return {
        "worker_id": f"cli:{harness}:b8b4eb83-7b5c-4b05-86a1-9d638cf8e834:dispatch_x_senior",
        "heartbeat_at": (_NOW - ago).isoformat(),
        "result": {"agent_failure": failure},
    }


def test_a_recent_usage_limit_benches_that_harness() -> None:
    limited = recently_usage_limited([_lease("codex", ago=USAGE_LIMIT_COOLDOWN / 2)], now=_NOW)

    assert limited == frozenset({FrontierHarness.CODEX})


def test_an_old_usage_limit_does_not() -> None:
    """A quota spent two days ago says nothing about now, and benching on it
    would take a working provider off the board indefinitely."""

    limited = recently_usage_limited([_lease("codex", ago=timedelta(days=2))], now=_NOW)

    assert limited == frozenset()


def test_the_window_boundary_is_the_cooldown() -> None:
    inside = recently_usage_limited(
        [_lease("claude", ago=USAGE_LIMIT_COOLDOWN - timedelta(minutes=1))], now=_NOW
    )
    outside = recently_usage_limited(
        [_lease("claude", ago=USAGE_LIMIT_COOLDOWN + timedelta(minutes=1))], now=_NOW
    )

    assert inside == frozenset({FrontierHarness.CLAUDE})
    assert outside == frozenset()


def test_only_usage_limits_bench_a_harness() -> None:
    """A harness that failed for its own reasons is not a harness that cannot act."""

    limited = recently_usage_limited(
        [_lease("codex", ago=timedelta(minutes=5), failure="VERIFICATION_FAILED")], now=_NOW
    )

    assert limited == frozenset()


def test_an_unparseable_worker_id_benches_nothing() -> None:
    """Not recognising a worker id is a reason to leave a harness alone."""

    limited = recently_usage_limited(
        [
            {
                "worker_id": "something-else",
                "heartbeat_at": _NOW.isoformat(),
                "result": {"agent_failure": "USAGE_LIMIT"},
            }
        ],
        now=_NOW,
    )

    assert limited == frozenset()


def test_a_spent_harness_becomes_the_refusal_it_already_was() -> None:
    """Expressed as `HarnessNotReady` so staffing needs no second vocabulary."""

    narrowed = narrow_by_reported_failures(
        (HarnessReady(harness=FrontierHarness.CODEX),),
        frozenset({FrontierHarness.CODEX}),
    )

    assert isinstance(narrowed[0], HarnessNotReady)
    assert "usage limit" in narrowed[0].detail
    assert "staffing.toml" in narrowed[0].remedy


def test_a_better_answer_is_never_overwritten() -> None:
    """`HarnessNotReady` carries a more specific remedy and `HarnessUnknown`
    means the probe could not answer. Replacing either with a quota message
    would trade a good answer for a worse one."""

    logged_out = HarnessNotReady(
        harness=FrontierHarness.CODEX, detail="not signed in", remedy="codex login"
    )
    unknown = HarnessUnknown(harness=FrontierHarness.CLAUDE, detail="did not run")

    narrowed = narrow_by_reported_failures(
        (logged_out, unknown),
        frozenset({FrontierHarness.CODEX, FrontierHarness.CLAUDE}),
    )

    assert narrowed == (logged_out, unknown)


def _spent(tier: DispatchTier) -> frozenset[FrontierHarness]:
    """The quota set that puts the vendor seated at `tier` out of action.

    Derived from the bench rather than named, because these scenarios are about
    a tier moving off a spent provider, not about claude or codex. Naming the
    vendor made them assert an outcome that was already true once the bench was
    reseated, which is a test that passes without checking anything.
    """

    return frozenset({FrontierHarness(DEFAULT_BENCH[tier].harness.value)})


def _other_vendor(tier: DispatchTier) -> Harness:
    """The vendor a tier moves to when its own provider is spent."""

    other = DispatchTier.STAFF if tier is DispatchTier.SENIOR else DispatchTier.SENIOR
    return DEFAULT_BENCH[other].harness


def test_the_two_features_meet(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point, stated end to end.

    A quota the staff seat's provider reported an hour ago now re-staffs that
    tier instead of being rediscovered by spending an attempt on it.
    """

    states = narrow_by_reported_failures(
        (HarnessReady(harness=FrontierHarness.CODEX), HarnessReady(harness=FrontierHarness.CLAUDE)),
        _spent(DispatchTier.STAFF),
    )

    plan = plan_tier_staffing(states=states)

    staff = next(item for item in plan if item.tier is DispatchTier.STAFF)
    assert isinstance(staff, TierRestaffed)
    assert staff.replacement.harness is DEFAULT_BENCH[DispatchTier.SENIOR].harness


def test_both_providers_spent_refuses_instead_of_burning_attempts() -> None:
    """Where the run actually was. Refusing is the correct answer, and it is a
    different one from silently trying both and failing twice."""

    states = narrow_by_reported_failures(
        (HarnessReady(harness=FrontierHarness.CODEX), HarnessReady(harness=FrontierHarness.CLAUDE)),
        frozenset({FrontierHarness.CODEX, FrontierHarness.CLAUDE}),
    )

    plan = plan_tier_staffing(states=states)

    assert any(isinstance(item, TierUnstaffable) for item in plan)


# --- The ledger half on its own, which is the half a dispatch can afford -------


def test_a_dispatch_moves_a_tier_off_a_harness_that_reported_a_spent_quota() -> None:
    """The junior tier is untouched, because a local model has no quota to spend."""

    plan = staffing_around_spent_quotas(dict(DEFAULT_BENCH), _spent(DispatchTier.SENIOR))

    bench = effective_bench(plan)
    assert bench[DispatchTier.SENIOR].harness is _other_vendor(DispatchTier.SENIOR)
    assert bench[DispatchTier.STAFF].harness is DEFAULT_BENCH[DispatchTier.STAFF].harness
    assert bench[DispatchTier.JUNIOR] == DEFAULT_BENCH[DispatchTier.JUNIOR]
    assert restaffings(plan) != ()


def test_a_restaffed_tier_keeps_its_own_capacity() -> None:
    """Which provider answers is the replacement's; how many of this tier may run
    at once is a statement about the tier's role, so it stays the configured one."""

    plan = staffing_around_spent_quotas(dict(DEFAULT_BENCH), _spent(DispatchTier.SENIOR))

    bench = effective_bench(plan)
    assert bench[DispatchTier.SENIOR].capacity == DEFAULT_BENCH[DispatchTier.SENIOR].capacity
    assert (
        bench[DispatchTier.SENIOR].reasoning_effort
        == DEFAULT_BENCH[DispatchTier.STAFF].reasoning_effort
    )


def test_nowhere_to_move_still_dispatches() -> None:
    """Losing the optimisation must not become a refusal.

    A bench with one frontier provider and a spent quota has no better answer
    than the one it already had, and mid-run the honest response is to dispatch
    there and say so, not to strand a run the operator door already admitted.
    """

    bench: Bench = {
        DispatchTier.STAFF: DEFAULT_BENCH[DispatchTier.STAFF],
        DispatchTier.JUNIOR: DEFAULT_BENCH[DispatchTier.JUNIOR],
    }

    plan = staffing_around_spent_quotas(bench, _spent(DispatchTier.STAFF))

    assert any(isinstance(item, TierUnstaffable) for item in plan)
    assert effective_bench(plan) == bench


def test_a_tier_the_operator_left_unstaffed_is_not_invented() -> None:
    """A partial bench stays partial: the plan covers its tiers and nothing else.

    `plan_tier_staffing` would raise on this bench - `resolve_bench` demands a
    slot for every tier it is asked about and falls back to the defaults only
    when handed no bench at all. A dispatch holds whatever bench its runner was
    built with, so the dispatch-time planner walks the tiers that exist rather
    than demanding the rest."""

    bench: Bench = {DispatchTier.SENIOR: DEFAULT_BENCH[DispatchTier.SENIOR]}

    plan = staffing_around_spent_quotas(bench, frozenset())

    assert [item.tier for item in plan] == [DispatchTier.SENIOR]
    assert effective_bench(plan) == bench


def test_a_local_tier_is_never_the_replacement() -> None:
    """A served local model standing in for a frontier implementer is the silent
    substitution this area exists to avoid, not a recovery from one."""

    bench: Bench = {
        DispatchTier.SENIOR: BenchSlot(harness=Harness.CLAUDE),
        DispatchTier.JUNIOR: DEFAULT_BENCH[DispatchTier.JUNIOR],
    }

    plan = staffing_around_spent_quotas(bench, frozenset({FrontierHarness.CLAUDE}))

    by_tier = {item.tier: item for item in plan}
    assert isinstance(by_tier[DispatchTier.SENIOR], TierUnstaffable)
    assert isinstance(by_tier[DispatchTier.JUNIOR], TierServed)
    assert effective_bench(plan) == bench


def test_the_dispatch_read_asks_by_the_rule_s_own_subject() -> None:
    """Windowed by the cooldown and filtered to the failure the rule reads.

    Bounding by time keeps the cost a property of the window rather than of how
    long this ledger has been running. Bounding by subject is what a plain row
    cap got wrong: live leases heartbeat continuously, so any cap over all
    recent rows let a busy machine push the terminal usage-limit rows past the
    limit and the read came back empty exactly when it mattered.
    """

    asked: list[tuple[float, str]] = []

    def _record(cutoff: float, *, failure: str, **_: Any) -> list[dict[str, Any]]:
        asked.append((cutoff, failure))
        return []

    original = ledger_execution.agent_failure_leases_since
    ledger_execution.agent_failure_leases_since = _record  # type: ignore[assignment]
    try:
        read_spent_quotas(now=_NOW)
    finally:
        ledger_execution.agent_failure_leases_since = original  # type: ignore[assignment]

    assert asked == [((_NOW - USAGE_LIMIT_COOLDOWN).timestamp(), "USAGE_LIMIT")]


def test_a_dispatch_pays_a_bounded_wait_for_the_quota_read() -> None:
    """The checkout budget reaches the pool, and only the dispatch tightens it.

    The pool waits 30 seconds by default, which is right for a command that must
    happen and wrong for a best-effort read on the path of every claimed intent.
    A door passes nothing and inherits the pool's patience, exactly as before.
    """

    budgets: list[float | None] = []

    def _record(
        cutoff: float, *, checkout_timeout_seconds: float | None = None, **_: Any
    ) -> list[dict[str, Any]]:
        budgets.append(checkout_timeout_seconds)
        return []

    original = ledger_execution.agent_failure_leases_since
    ledger_execution.agent_failure_leases_since = _record  # type: ignore[assignment]
    try:
        read_spent_quotas(now=_NOW)
        read_spent_quotas(
            now=_NOW,
            checkout_timeout_seconds=harness_availability.DISPATCH_QUOTA_READ_TIMEOUT_SECONDS,
        )
    finally:
        ledger_execution.agent_failure_leases_since = original  # type: ignore[assignment]

    assert budgets == [None, harness_availability.DISPATCH_QUOTA_READ_TIMEOUT_SECONDS]


def test_an_unreachable_ledger_costs_the_optimisation_and_not_the_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read that could not happen answers with nothing, which narrows nothing,
    which staffs exactly as this system did before the read existed."""

    def _down(cutoff: float, **_: Any) -> list[dict[str, Any]]:
        raise LedgerUnavailable("coordination store is not reachable")

    monkeypatch.setattr(ledger_execution, "agent_failure_leases_since", _down)

    spent = read_spent_quotas(now=_NOW)

    assert spent == frozenset()
    assert effective_bench(staffing_around_spent_quotas(dict(DEFAULT_BENCH), spent)) == (
        DEFAULT_BENCH
    )


def test_the_door_and_a_dispatch_decide_a_spent_quota_by_one_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The door pays for a subprocess probe on top of this; the dispatch does not.

    A second copy of the rule would let a run be admitted on one reading of the
    ledger and staffed on another, and the disagreement would only ever show up
    as an attempt spent rediscovering a quota somebody already knew about.
    """

    monkeypatch.setattr(
        ledger_execution,
        "agent_failure_leases_since",
        lambda cutoff, **_: [
            _lease(DEFAULT_BENCH[DispatchTier.STAFF].harness.value, ago=timedelta(hours=1))
        ],
    )
    monkeypatch.setattr(
        harness_availability,
        "check_frontier_readiness",
        lambda bench=None: (
            HarnessReady(harness=FrontierHarness.CODEX),
            HarnessReady(harness=FrontierHarness.CLAUDE),
        ),
    )

    at_the_door = check_harness_availability(now=_NOW)
    at_a_dispatch = read_spent_quotas(now=_NOW)

    assert {
        state.harness for state in at_the_door if isinstance(state, HarnessNotReady)
    } == at_a_dispatch


def test_a_milestone_dispatched_after_a_usage_limit_avoids_that_harness(
    runtime: AppRuntime,
) -> None:
    """The whole point, against a real ledger.

    The staff seat's provider reports a spent quota; the next intent this runner
    dispatches resolves the staff tier onto the other vendor. Before this, the
    bench was read once at construction and every later milestone in the run went
    to the spent provider anyway.
    """

    spent_vendor = DEFAULT_BENCH[DispatchTier.STAFF].harness

    opened = ledger_execution.open_execution_lease(
        idempotency_key="dispatch-quota-evidence",
        worker_id=(
            f"cli:{spent_vendor.value}:6f6f1a0e-2f5f-4a1e-9f34-6b5f0c2a77aa:dispatch_x_staff"
        ),
        agent_tier="staff",
        agent_name=spent_vendor.value,
    )
    ledger_execution.complete_execution_lease(
        opened["lease"]["lease_id"],
        "FAILED",
        result_json=json.dumps({"agent_failure": "USAGE_LIMIT"}),
    )
    runner = DispatcherIntentRunner(runtime, bench=dict(DEFAULT_BENCH))

    bench = runner.bench_for_dispatch("intent-after-the-limit")

    assert runner.bench[DispatchTier.STAFF].harness is spent_vendor
    assert bench[DispatchTier.STAFF].harness is DEFAULT_BENCH[DispatchTier.SENIOR].harness


def test_a_quiet_ledger_leaves_the_operators_bench_exactly_as_written(
    runtime: AppRuntime,
) -> None:
    """No reported quota is no reason to deviate from a decision an operator made."""

    runner = DispatcherIntentRunner(runtime, bench=dict(DEFAULT_BENCH))

    assert runner.bench_for_dispatch("intent-on-a-quiet-ledger") == DEFAULT_BENCH


def test_live_leases_cannot_starve_the_spent_quota_read(runtime: AppRuntime) -> None:
    """The reproduction that killed the first row cap, kept as a regression.

    One terminal USAGE_LIMIT lease and a crowd of live leases with fresher
    heartbeats, read with a cap smaller than the crowd. A cap over all recent
    leases returns only the live ones - they heartbeat continuously and always
    sort above a terminal row frozen at its completion - so the quota is
    forgotten exactly when the machine is busiest. A cap over failure rows has
    nothing to starve it with.
    """

    opened = ledger_execution.open_execution_lease(
        idempotency_key="quota-starve-terminal",
        worker_id="cli:codex:aa11aa11-0000-4000-8000-000000000001:starve_staff",
        agent_tier="staff",
        agent_name="codex",
    )
    ledger_execution.complete_execution_lease(
        opened["lease"]["lease_id"],
        "FAILED",
        result_json=json.dumps({"agent_failure": "USAGE_LIMIT"}),
    )
    for index in range(5):
        ledger_execution.open_execution_lease(
            idempotency_key=f"quota-starve-live-{index}",
            worker_id=f"cli:claude:aa11aa11-0000-4000-8000-00000000001{index}:starve_live",
            agent_tier="senior",
            agent_name="claude",
        )

    rows = ledger_execution.agent_failure_leases_since(
        (datetime.now(UTC) - USAGE_LIMIT_COOLDOWN).timestamp(),
        failure="USAGE_LIMIT",
        limit=3,
    )

    assert [row["result"]["agent_failure"] for row in rows] == ["USAGE_LIMIT"]
    assert read_spent_quotas() == frozenset({FrontierHarness.CODEX})


def test_a_truncated_evidence_read_says_so(
    runtime: AppRuntime, caplog: pytest.LogCaptureFixture
) -> None:
    """Hitting the cap is loud, because silent truncation is how the previous
    read reintroduced the bug it existed to fix."""

    for index in range(2):
        opened = ledger_execution.open_execution_lease(
            idempotency_key=f"quota-truncate-{index}",
            worker_id=f"cli:codex:bb22bb22-0000-4000-8000-00000000000{index}:truncate",
            agent_tier="staff",
            agent_name="codex",
        )
        ledger_execution.complete_execution_lease(
            opened["lease"]["lease_id"],
            "FAILED",
            result_json=json.dumps({"agent_failure": "USAGE_LIMIT"}),
        )

    with caplog.at_level("WARNING"):
        rows = ledger_execution.agent_failure_leases_since(
            (datetime.now(UTC) - USAGE_LIMIT_COOLDOWN).timestamp(),
            failure="USAGE_LIMIT",
            limit=1,
        )

    assert len(rows) == 1
    assert any("agent_failure_leases_truncated" in record.message for record in caplog.records)


def test_a_restaffing_names_the_cross_check_it_collapses() -> None:
    """Staff moving onto senior's provider is not just a substitution.

    The bench staffs the reviewer and the implementer on different frontier
    providers on purpose - two models checking each other. A dispatch that quietly
    collapses that pairing behind a progress line spends the property without
    anyone deciding to, so the plan names it and the dispatch path says it.
    """

    plan = staffing_around_spent_quotas(dict(DEFAULT_BENCH), _spent(DispatchTier.STAFF))

    notices = collapsed_cross_checks(plan)

    assert len(notices) == 1
    assert "staff" in notices[0]
    assert DEFAULT_BENCH[DispatchTier.SENIOR].harness.value in notices[0]
    assert "cross-check is collapsed" in notices[0]


def test_a_restaffing_between_distinct_providers_collapses_nothing() -> None:
    """The notice is for a collapsed pairing, not for restaffing itself."""

    bench: Bench = {
        DispatchTier.STAFF: DEFAULT_BENCH[DispatchTier.STAFF],
        DispatchTier.JUNIOR: DEFAULT_BENCH[DispatchTier.JUNIOR],
    }

    plan = staffing_around_spent_quotas(bench, _spent(DispatchTier.STAFF))

    assert collapsed_cross_checks(plan) == ()
