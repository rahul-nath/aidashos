# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A spent quota must queue the work, not spend the milestone's attempts.

The case these pin was live on 2026-08-12. Both frontier seats were staffed to
claude, the subscription window was spent, and the dispatch path's answer was to
claim anyway - "dispatching there anyway rather than stranding a run the operator
door already admitted". Each claim reached a harness that refused, recorded
`USAGE_LIMIT`, and was classed `CORRECTABLE`, which spends one of a milestone's
three attempts. Three sweeps exhausted a budget on a provider outage, and
clearing that needs an operator override.

`_NO_FAULT_OUTCOMES` names the precondition for fixing it: "that one wants the
bench to answer first". These cover the bench answering first - an unstaffable
tier is not claimed from, so the intent keeps its place and the dispatcher's own
sweep interval is the retry.

The second half is the reset time. Providers state when the quota returns, both
usage-limited rows in the ledger recorded that sentence verbatim, and nothing
read it: a limit hit two hours into a five-hour window was benched for five more
from the failure rather than three to the stated reset.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from local_first_agent_os import harness_availability as availability
from local_first_agent_os.harness_availability import (
    build_quota_claim_gate,
    parse_quota_reset,
    recently_usage_limited,
)
from local_first_agent_os.staffing import (
    BackupModel,
    Bench,
    BenchSlot,
    FrontierHarness,
    Harness,
    Tier,
)

_NY = ZoneInfo("America/New_York")

# The refusal exactly as the ledger recorded it, interpunct and all.
_OBSERVED = "You've hit your session limit · resets 5:30am (America/New_York)"


def _bench() -> Bench:
    """The bench as staffed on 2026-08-12: both frontier seats on claude."""

    return {
        Tier.SENIOR: BenchSlot(harness=Harness.CLAUDE, model="claude-opus-5", capacity=2),
        Tier.STAFF: BenchSlot(harness=Harness.CLAUDE, model="claude-fable-5", capacity=1),
        Tier.JUNIOR: BenchSlot(harness=Harness.PI, model="gemma4", capacity=4),
    }


def _usage_limited_lease(*, at: datetime, text: str) -> dict[str, object]:
    return {
        "worker_id": "cli:claude:5f2a:dispatch_x",
        "heartbeat_at": at.isoformat(),
        "result": {
            "agent_failure": "USAGE_LIMIT",
            "command_capture": {"stdout": "", "stderr": text},
        },
    }


def test_the_observed_refusal_yields_the_time_the_provider_stated() -> None:
    """The answer was in the text the whole time."""

    now = datetime(2026, 8, 12, 13, 27, tzinfo=_NY)

    assert parse_quota_reset(_OBSERVED, now=now) == datetime(2026, 8, 13, 5, 30, tzinfo=_NY)


@pytest.mark.parametrize(
    ("text", "expected_hour", "expected_minute"),
    [
        ("resets 3:10pm (America/New_York)", 15, 10),
        ("resets 4:19pm (America/New_York)", 16, 19),
        ("resets 12:00am (America/New_York)", 0, 0),
        ("resets 12:30pm (America/New_York)", 12, 30),
        ("resets 9pm (America/New_York)", 21, 0),
    ],
)
def test_the_shapes_a_stated_reset_arrives_in(
    text: str, expected_hour: int, expected_minute: int
) -> None:
    now = datetime(2026, 8, 12, 13, 27, tzinfo=_NY)
    reset = parse_quota_reset(text, now=now)

    assert reset is not None
    assert (reset.hour, reset.minute) == (expected_hour, expected_minute)


@pytest.mark.parametrize(
    "text",
    [
        "usage limit reached",
        "You've hit your session limit",
        # A bare number is as likely to be a duration or a percentage as a
        # clock time, and guessing wrong puts a spent harness back early.
        "resets 5",
        "resets in 5 hours",
        "resets 25:00pm",
    ],
)
def test_a_reset_that_was_not_stated_is_not_invented(text: str) -> None:
    now = datetime(2026, 8, 12, 13, 27, tzinfo=_NY)

    assert parse_quota_reset(text, now=now) is None


def test_a_stated_reset_beats_the_flat_cooldown_in_both_directions() -> None:
    """The whole point: the provider's own answer outranks the constant.

    A limit hit at 13:27 that the provider says clears at 16:19 must free the
    harness at 16:19. The flat five hours would hold it until 18:27, benching a
    live subscription for two hours it did not owe.
    """

    hit = datetime(2026, 8, 12, 13, 27, tzinfo=_NY)
    lease = _usage_limited_lease(at=hit, text="resets 4:19pm (America/New_York)")

    def spent_at(moment: datetime) -> frozenset[FrontierHarness]:
        return recently_usage_limited([lease], now=moment)

    still_spent = frozenset({FrontierHarness.CLAUDE})
    assert spent_at(datetime(2026, 8, 12, 16, 18, tzinfo=_NY)) == still_spent
    assert spent_at(datetime(2026, 8, 12, 16, 20, tzinfo=_NY)) == frozenset()
    # The flat cooldown would still have called this spent.
    assert hit + timedelta(hours=5) > datetime(2026, 8, 12, 16, 20, tzinfo=_NY)


def test_a_lease_that_states_no_reset_keeps_the_flat_cooldown() -> None:
    """The parser is an improvement on the constant, not a replacement for it."""

    hit = datetime(2026, 8, 12, 13, 27, tzinfo=_NY)
    lease = _usage_limited_lease(at=hit, text="usage limit reached")

    assert recently_usage_limited([lease], now=hit + timedelta(hours=1)) == frozenset(
        {FrontierHarness.CLAUDE}
    )
    assert recently_usage_limited([lease], now=hit + timedelta(hours=6)) == frozenset()


def _gate(monkeypatch: pytest.MonkeyPatch, spent: set[FrontierHarness]):
    monkeypatch.setattr(availability, "read_spent_quotas", lambda **_: frozenset(spent))
    return build_quota_claim_gate(_bench(), ttl_seconds=0)


def test_a_tier_whose_harness_is_spent_is_not_claimed_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bench answers first, which is what makes the queued intent safe."""

    gate = _gate(monkeypatch, {FrontierHarness.CLAUDE})

    assert gate("senior") is False
    assert gate("staff") is False
    # The local tier never depended on that subscription and must keep running.
    assert gate("junior") is True


def test_a_spent_harness_this_bench_does_not_staff_gates_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex being spent is not a fact about a bench that staffs neither seat to it."""

    gate = _gate(monkeypatch, {FrontierHarness.CODEX})

    assert gate("senior") is True
    assert gate("staff") is True


def test_an_unspent_bench_claims_exactly_as_it_always_did(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _gate(monkeypatch, set())

    assert all(gate(tier) for tier in ("senior", "staff", "junior"))


def test_an_unreadable_ledger_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown is not evidence of a spent quota.

    A gate that closed on a slow or unreachable ledger would stop every dispatch
    on the machine for as long as the database was unwell, which is a far larger
    outage than the one it exists to prevent.
    """

    def unreachable(**_: object) -> frozenset[FrontierHarness]:
        raise RuntimeError("ledger down")

    monkeypatch.setattr(availability, "read_spent_quotas", unreachable)
    gate = build_quota_claim_gate(_bench(), ttl_seconds=0)

    assert all(gate(tier) for tier in ("senior", "staff", "junior"))


def test_an_unknown_or_unscoped_tier_is_never_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refusing here would strand work over a quota that cannot apply to it."""

    gate = _gate(monkeypatch, {FrontierHarness.CLAUDE})

    assert gate(None) is True
    assert gate("not_a_tier") is True


def test_the_gate_reuses_one_read_inside_its_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sweep is two seconds; the answer changes on the scale of hours."""

    reads = 0

    def counted(**_: object) -> frozenset[FrontierHarness]:
        nonlocal reads
        reads += 1
        return frozenset({FrontierHarness.CLAUDE})

    monkeypatch.setattr(availability, "read_spent_quotas", counted)
    ticks = iter([0.0, 1.0, 2.0, 100.0, 101.0])
    gate = build_quota_claim_gate(_bench(), ttl_seconds=60.0, clock=lambda: next(ticks))

    assert gate("senior") is False
    assert gate("senior") is False
    assert gate("senior") is False
    assert reads == 1, "three sweeps inside the TTL must not be three ledger reads"

    assert gate("senior") is False
    assert reads == 2, "a sweep past the TTL must re-read"


def _spent_codex_plan(bench: Bench):
    from local_first_agent_os.harness_availability import staffing_around_spent_quotas

    return staffing_around_spent_quotas(bench, frozenset({FrontierHarness.CODEX}))


def _two_vendor_bench() -> Bench:
    """The bench as staffed on 2026-08-13: codex implements, claude reviews."""

    return {
        Tier.SENIOR: BenchSlot(
            harness=Harness.CODEX,
            model="gpt-5.6-sol",
            capacity=2,
            backup_models=(BackupModel(harness=Harness.CLAUDE, model="claude-sonnet-5"),),
        ),
        Tier.STAFF: BenchSlot(harness=Harness.CLAUDE, model="claude-opus-5", capacity=1),
        Tier.JUNIOR: BenchSlot(harness=Harness.PI, model="gemma4", capacity=4),
    }


def test_a_spent_senior_keeps_a_model_the_reviewer_is_not_using() -> None:
    """The point of `backup_models`, which until now nothing read.

    Restaffing moved a spent senior onto another tier's whole slot, model and
    all, so a spent codex put `claude-opus-5` in both seats and the reviewer
    became the model that wrote the change. The backup is what keeps the two
    apart.
    """

    from local_first_agent_os.harness_readiness import effective_bench

    effective = effective_bench(_spent_codex_plan(_two_vendor_bench()))

    assert effective[Tier.SENIOR].harness is Harness.CLAUDE
    assert effective[Tier.SENIOR].model == "claude-sonnet-5"
    assert effective[Tier.STAFF].model == "claude-opus-5"
    assert effective[Tier.SENIOR].model != effective[Tier.STAFF].model


def test_a_bench_that_declares_no_backup_behaves_exactly_as_before() -> None:
    """The change is an improvement on silence, never a new requirement."""

    from local_first_agent_os.harness_readiness import effective_bench

    bench = _two_vendor_bench()
    bench[Tier.SENIOR] = replace(bench[Tier.SENIOR], backup_models=())
    effective = effective_bench(_spent_codex_plan(bench))

    assert effective[Tier.SENIOR].model == effective[Tier.STAFF].model == "claude-opus-5"


def test_a_backup_naming_the_reviewer_s_own_model_is_skipped() -> None:
    """A backup that buys no variance is not a backup for this purpose."""

    from local_first_agent_os.harness_readiness import effective_bench

    bench = _two_vendor_bench()
    bench[Tier.SENIOR] = replace(
        bench[Tier.SENIOR],
        backup_models=(
            BackupModel(harness=Harness.CLAUDE, model="claude-opus-5"),
            BackupModel(harness=Harness.CLAUDE, model="claude-sonnet-5"),
        ),
    )
    effective = effective_bench(_spent_codex_plan(bench))

    assert effective[Tier.SENIOR].model == "claude-sonnet-5"


def test_the_notice_tells_the_two_losses_apart() -> None:
    """Losing the vendor and losing the second opinion are not one event.

    One sentence for both understated a surviving cross-check in one direction
    and overstated a vanished one in the other, and the second mistake is the
    expensive one.
    """

    from local_first_agent_os.harness_availability import collapsed_cross_checks

    kept = collapsed_cross_checks(_spent_codex_plan(_two_vendor_bench()))
    assert len(kept) == 1
    assert "provider diversity is gone" in kept[0]
    assert "claude-sonnet-5" in kept[0]

    bench = _two_vendor_bench()
    bench[Tier.SENIOR] = replace(bench[Tier.SENIOR], backup_models=())
    lost = collapsed_cross_checks(_spent_codex_plan(bench))
    assert len(lost) == 1
    assert "implementing and reviewing its own change" in lost[0]


# --- The escape hatch on a bare bench, which needs no peer ---------------------
#
# These exercise the Bench form of `staffing_around_spent_quotas`: per-tier
# moves through harness-typed `backup_models`, the behavior a bench without
# pairing declarations can honestly support. The production path loads a
# `Staffing` and moves the frontier pair together; that is covered below and in
# `test_staffing.py`.


def _spent_claude_plan(bench: Bench):
    from local_first_agent_os.harness_availability import staffing_around_spent_quotas

    return staffing_around_spent_quotas(bench, frozenset({FrontierHarness.CLAUDE}))


def _escape_hatch_bench(*backups: BackupModel) -> Bench:
    """The 2026-08-12 seating again, with a way out declared on the senior seat."""

    bench = _bench()
    bench[Tier.SENIOR] = replace(bench[Tier.SENIOR], backup_models=backups)
    return bench


def test_a_backup_names_the_harness_a_bench_with_no_unspent_peer_moves_to() -> None:
    """The escape hatch, in the only seating that needs one.

    Both frontier seats on one vendor is what an operator writes during that
    vendor's outage, and it is exactly the bench with no unspent peer. The peer
    search answered `None`, `_replacement_for` returned before reading a backup,
    and the tier was reported unstaffable while the config carried a written-down
    way out - so the hatch was closed in the one situation it exists for.
    """

    from local_first_agent_os.harness_readiness import TierRestaffed

    plan = _spent_claude_plan(
        _escape_hatch_bench(
            BackupModel(harness=Harness.CODEX, model="gpt-5.6-sol", reasoning_effort="high")
        )
    )

    senior = next(item for item in plan if item.tier is Tier.SENIOR)
    assert isinstance(senior, TierRestaffed)
    assert senior.slot.harness is Harness.CODEX
    assert senior.slot.model == "gpt-5.6-sol"
    assert senior.slot.reasoning_effort == "high"
    # How many of a tier may run at once is a statement about the tier, so it
    # survives a move that has no peer slot to borrow anything from.
    assert senior.slot.capacity == _bench()[Tier.SENIOR].capacity


def test_a_backup_on_the_spent_harness_is_not_a_way_out() -> None:
    """Another model from the vendor that just refused is still that vendor."""

    from local_first_agent_os.harness_readiness import TierUnstaffable

    plan = _spent_claude_plan(
        _escape_hatch_bench(BackupModel(harness=Harness.CLAUDE, model="claude-sonnet-5"))
    )

    senior = next(item for item in plan if item.tier is Tier.SENIOR)
    assert isinstance(senior, TierUnstaffable)


def test_a_backup_on_the_local_harness_is_not_a_way_out() -> None:
    """The substitution the peer search already refuses, refused on this path too.

    A served local model standing in for a frontier implementer is the silent
    substitution this area exists to avoid. A backup entry is a second door into
    the same decision, and it would have been an unguarded one.
    """

    from local_first_agent_os.harness_readiness import TierUnstaffable

    plan = _spent_claude_plan(_escape_hatch_bench(BackupModel(harness=Harness.PI, model="gemma4")))

    senior = next(item for item in plan if item.tier is Tier.SENIOR)
    assert isinstance(senior, TierUnstaffable)
    assert "declares no backup on an unspent harness" in senior.detail


def test_the_escape_hatch_is_what_the_claim_gate_asks_about(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate refuses a seat with no way out and admits one with a hatch.

    The two halves have to agree or the fix is cosmetic: a tier that can now be
    restaffed must also become claimable, or the intent keeps waiting for a
    quota window it no longer needs.
    """

    monkeypatch.setattr(
        availability, "read_spent_quotas", lambda **_: frozenset({FrontierHarness.CLAUDE})
    )
    hatch = _escape_hatch_bench(BackupModel(harness=Harness.CODEX, model="gpt-5.6-sol"))

    assert build_quota_claim_gate(_bench(), ttl_seconds=0)("senior") is False
    assert build_quota_claim_gate(hatch, ttl_seconds=0)("senior") is True
    # The seat that declares nothing still waits, which is the queueing this
    # gate exists for rather than a hole the hatch opened.
    assert build_quota_claim_gate(hatch, ttl_seconds=0)("staff") is False


# --- The gate on the real thing: a Staffing, where the pair answers as one -----


def _staffing(*, with_fallback: bool):
    from local_first_agent_os.staffing import FrontierPairing, Staffing

    seated = FrontierPairing(
        name="claude-only",
        senior=BenchSlot(harness=Harness.CLAUDE, model="claude-opus-5", capacity=2),
        staff=BenchSlot(harness=Harness.CLAUDE, model="claude-fable-5", capacity=1),
        fallback=("codex-only",) if with_fallback else (),
    )
    codex = FrontierPairing(
        name="codex-only",
        senior=BenchSlot(harness=Harness.CODEX, model="gpt-5.6-terra", capacity=2),
        staff=BenchSlot(harness=Harness.CODEX, model="gpt-5.6-sol", capacity=1),
    )
    return Staffing(
        pairings={seated.name: seated, codex.name: codex},
        seated=seated,
        solo={Tier.JUNIOR: BenchSlot(harness=Harness.PI, model="gemma4", capacity=4)},
    )


def test_the_pair_is_claimable_together_or_not_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate's answer is pair-shaped because the restaffing is.

    A gate that admitted senior while staff queued would run the half-escape
    the pairing type exists to remove: an implementation dispatched with no
    reviewer able to stand behind it. So a viable fallback pairing opens both
    seats and a missing one closes both, while the local tier - which never
    depended on either subscription - keeps running either way.
    """

    monkeypatch.setattr(
        availability, "read_spent_quotas", lambda **_: frozenset({FrontierHarness.CLAUDE})
    )

    open_gate = build_quota_claim_gate(_staffing(with_fallback=True), ttl_seconds=0)
    assert open_gate("senior") is True
    assert open_gate("staff") is True

    closed_gate = build_quota_claim_gate(_staffing(with_fallback=False), ttl_seconds=0)
    assert closed_gate("senior") is False
    assert closed_gate("staff") is False
    assert closed_gate("junior") is True
