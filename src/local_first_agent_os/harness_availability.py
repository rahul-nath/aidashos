# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Whether a harness can act, counting what it already told us it cannot do.

`harness_readiness` asks this machine whether a CLI is signed in. That is a real
question and it is not the whole one: a harness whose quota is spent answers
`loggedIn: true` and then fails every task it is given. The probe cannot see a
quota without spending a request to find out, which is the cost it exists to
avoid.

The ledger already knows. Every execution lease carries the harness in its
`worker_id` and the outcome in `agent_failure`, so "did codex report a usage
limit recently" is a question the durable record can answer for free. Nothing
asked it. On 2026-08-06 codex reported `USAGE_LIMIT` at 03:04 and a dispatch at
03:44 went to codex anyway, because staffing read a config file and recovery
reacted to one failure at a time, and neither consulted the history sitting
between them.

This module is where those meet. It returns `HarnessReadiness`, deliberately:
`HarnessNotReady` already means "this harness answered and cannot act", and a
spent quota is an instance of that rather than a new kind of thing. So
`plan_tier_staffing` re-staffs a quota-exhausted tier through exactly the path
it already uses for a logged-out one, and no caller learns a second vocabulary.

The window is a decision, not a measurement. A provider that refused an hour ago
will probably refuse now; one that refused two days ago probably will not. There
is no reading of the ledger that settles it, so it is a named constant an
operator can change rather than a number buried in a comparison.

The two halves cost different things and are therefore separable, which is the
only reason a dispatch can have one of them. `check_frontier_readiness` spawns a
subprocess per harness and belongs where a human asked for something and is
waiting. `read_spent_quotas` is one bounded read of rows the run is already
writing, so it can run again on every claimed intent, which is what makes the
answer current instead of fixed at the door. `check_harness_availability`
composes both for the doors; `staffing_around_spent_quotas` consumes the cheap
half alone for a dispatch.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .harness_readiness import (
    HarnessNotReady,
    HarnessReadiness,
    HarnessReady,
    TierRestaffed,
    TierServed,
    TierStaffing,
    TierUnstaffable,
    check_frontier_readiness,
)
from .settings import Settings
from .staffing import (
    Bench,
    BenchSlot,
    FrontierHarness,
    LocalHarness,
    Staffing,
    classify_harness,
)
from .vocabulary import DispatchTier

logger = logging.getLogger(__name__)

USAGE_LIMIT_COOLDOWN: Final = timedelta(minutes=20)
"""How long a usage limit with NO STATED RESET keeps a harness off the bench.

Twenty minutes, and the short value is the point. A provider that states its
own reset outranks this entirely - `parse_quota_reset` reads
`resets 11:30am (America/New_York)` out of the refusal and benches until exactly
then. This constant only covers the case where the provider said it was spent
and said nothing about when it would not be.

That case used to assume five hours, on the theory that it matched the shape of
these providers' rolling windows. The theory was untestable and wrong in the
expensive direction: on 2026-08-30 both vendors sat benched by this timer while
a one-line nonce to each answered `ok`, so the machine refused work it could
have done. An assumption that idles a working provider for hours is worse than
one that spends an attempt finding out.

So this is now a rate limiter rather than an outage model. It is long enough to
stop a blocked milestone hammering a spent provider, and short enough that a
window reopening is noticed in the next scheduling pass instead of the next
afternoon. The dispatch itself is the real availability check; this only decides
how soon that check may be repeated.
"""

_USAGE_LIMIT_FAILURE: Final = "USAGE_LIMIT"


_QUOTA_RESET_PATTERN: Final = re.compile(
    r"resets?\s+(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)\b"
    r"(?:\s*\((?P<zone>[A-Za-z][A-Za-z_]*(?:/[A-Za-z][A-Za-z_+-]*)+)\))?",
    re.IGNORECASE,
)
"""The reset moment a provider states in its own refusal.

`You've hit your session limit - resets 5:30am (America/New_York)` carries the
answer the cooldown constant is guessing at. Both usage-limited reviews in this
ledger recorded that sentence verbatim and nothing read it, so a harness that
came back at its stated time was still benched by a flat five hours measured
from the failure.

The meridiem is required rather than optional. A bare `resets 5` is as likely to
be a duration, a day, or a percentage as a clock time, and a wrong reset is worse
than none: it would put a spent harness back on the bench early and spend an
attempt proving it. No match means fall back to the cooldown, which is the
behaviour this is an improvement on rather than a replacement for.
"""


def parse_quota_reset(text: str, *, now: datetime) -> datetime | None:
    """When the provider said the quota returns, or None if it did not say.

    Resolved against ``now`` because the provider states a wall clock with no
    date: the next occurrence of that time is the only reading that is ever
    right, and a stated time already past today means tomorrow.

    An unknown zone falls back to ``now``'s own, which is the best available
    guess and never worse than the flat cooldown it replaces.
    """

    match = _QUOTA_RESET_PATTERN.search(text)
    if match is None:
        return None
    hour = int(match.group("hour"))
    if not 1 <= hour <= 12:
        return None
    minute = int(match.group("minute") or 0)
    if minute > 59:
        return None
    if match.group("meridiem").lower() == "pm":
        hour = hour if hour == 12 else hour + 12
    elif hour == 12:
        hour = 0
    zone_name = match.group("zone")
    tz = now.tzinfo or UTC
    if zone_name:
        with contextlib.suppress(ZoneInfoNotFoundError, ValueError):
            tz = ZoneInfo(zone_name)
    local_now = now.astimezone(tz)
    reset = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset <= local_now:
        reset += timedelta(days=1)
    return reset


def _lease_failure_text(result: Any) -> str:
    """The provider's own words on a failed lease, wherever the writer put them.

    Both terminal writers stamp `agent_failure`, but only the captured command
    carries the sentence that names the reset. Returns an empty string rather
    than raising on a shape it does not recognise: a lease whose text cannot be
    found is a lease that falls back to the cooldown.
    """

    if not isinstance(result, dict):
        return ""
    capture = result.get("command_capture")
    if not isinstance(capture, dict):
        return ""
    return "\n".join(str(capture.get(field) or "") for field in ("stdout", "stderr")).strip()


def _harness_of(worker_id: str) -> FrontierHarness | None:
    """The harness a lease ran on, read off the worker id it already carries.

    `cli:codex:<uuid>:<dispatch>` - the second field. Returns `None` rather than
    guessing for anything that does not parse, because a worker id this does not
    recognise is a reason to leave a harness alone, never a reason to bench it.
    """

    parts = worker_id.split(":")
    if len(parts) < 2:
        return None
    try:
        return FrontierHarness(parts[1])
    except ValueError:
        return None


def recently_usage_limited(
    leases: Iterable[dict[str, Any]],
    *,
    now: datetime,
    cooldown: timedelta = USAGE_LIMIT_COOLDOWN,
) -> frozenset[FrontierHarness]:
    """Which harnesses reported a spent quota inside the window.

    Takes the leases rather than fetching them, so the rule is testable without a
    ledger and so a caller that already has them does not read twice.
    """

    limited: set[FrontierHarness] = set()
    cutoff = now - cooldown
    for lease in leases:
        result = lease.get("result") or {}
        if result.get("agent_failure") != _USAGE_LIMIT_FAILURE:
            continue
        harness = _harness_of(str(lease.get("worker_id") or ""))
        if harness is None:
            continue
        stamp = lease.get("heartbeat_at") or lease.get("completed_at")
        if not stamp:
            continue
        try:
            when = datetime.fromisoformat(str(stamp))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        # What the provider said outranks what the constant assumes. The stated
        # reset is measured from the window's real start, which the failure
        # timestamp does not reveal: a limit hit two hours into a five-hour
        # window returns in three, and the flat cooldown would bench it for five
        # more. Only a reset the provider actually stated is trusted; anything
        # else falls through to the cooldown below.
        reset = parse_quota_reset(_lease_failure_text(result), now=when)
        if reset is not None:
            if now < reset:
                limited.add(harness)
            continue
        if when >= cutoff:
            limited.add(harness)
    return frozenset(limited)


def narrow_by_reported_failures(
    states: Iterable[HarnessReadiness],
    limited: frozenset[FrontierHarness],
    *,
    cooldown: timedelta = USAGE_LIMIT_COOLDOWN,
) -> tuple[HarnessReadiness, ...]:
    """Turn a ready-but-spent harness into the refusal it actually is.

    Only `HarnessReady` is narrowed. A harness already `HarnessNotReady` has a
    remedy attached that is more specific than this one, and `HarnessUnknown`
    means the probe could not answer - overwriting either with a quota message
    would replace a better answer with a worse one.
    """

    hours = int(cooldown.total_seconds() // 3600)
    narrowed: list[HarnessReadiness] = []
    for state in states:
        if isinstance(state, HarnessReady) and state.harness in limited:
            narrowed.append(
                HarnessNotReady(
                    harness=state.harness,
                    detail=(
                        "signed in but reported a usage limit within the last "
                        f"{hours}h, so every task staffed to it would spend an "
                        "attempt rediscovering that"
                    ),
                    remedy=(
                        "wait for the quota window, or staff this tier to another "
                        "harness in configs/staffing.toml"
                    ),
                )
            )
            continue
        narrowed.append(state)
    return tuple(narrowed)


DISPATCH_QUOTA_READ_TIMEOUT_SECONDS: Final = 2.0
"""How long a dispatch may wait to borrow a connection for the quota read.

The pool's own patience is 30 seconds, which is right for a command that must
happen and wrong for one that answers "empty" on failure anyway. This read sits
on the path of every claimed intent, and it is likeliest to find the pool slow
precisely when the pool is saturated - so left at the default, the optimisation
would cost half a minute of stall per dispatch exactly when the machine can
least afford it, and the swallow below would hide that it was happening.
"""


def read_spent_quotas(
    *,
    now: datetime | None = None,
    cooldown: timedelta = USAGE_LIMIT_COOLDOWN,
    settings: Settings | None = None,
    checkout_timeout_seconds: float | None = None,
) -> frozenset[FrontierHarness]:
    """Ask the durable record which harnesses reported a spent quota.

    The whole ledger half, in one call, so that a caller which cannot afford the
    probe can still have this. `recently_usage_limited` stays the rule and stays
    pure; this is the read that feeds it, and there is one of each. The read
    fetches by the rule's own subject - the `agent_failure` column both terminal
    writers stamp - so the window and a row cap bound it by the width of a
    failure storm rather than by how busy the machine has been.

    `settings` names the ledger, exactly as it does for every coordination
    command: the read runs inside `applied_ledger_selection`, so it holds the
    same barrier the transports hold and cannot race a sibling command onto a
    different database. Left as None it reads whatever this process is already
    pointed at, which is what an operator door means; a dispatch runner must
    pass its own settings, because under the subprocess transport the parent's
    environment names no ledger at all, and a bare read from there would answer
    from whichever database the environment happens to mention - an answer that
    can move a milestone off its configured harness for no reason.

    Best-effort. Every failure answers with the empty set, which narrows nothing,
    which leaves a caller staffing exactly as it did before this function
    existed. A read that could not happen has cost an optimisation, and an
    optimisation that turned into a refusal would be a worse failure than the one
    it prevents. The exception type reaches the log at warning because that is
    what separates an unreachable store, which a caller is right to ride out,
    from a defect in the query itself, which this catch would otherwise hide
    until somebody noticed the optimisation had never once worked.
    """

    moment = now or datetime.now(UTC)
    try:
        from .coordination.execution import agent_failure_leases_since
        from .coordination.ledger_selection import (
            CoordinationLedgerSelection,
            applied_ledger_selection,
        )

        selection = (
            CoordinationLedgerSelection.resolve(settings)
            if settings is not None
            else CoordinationLedgerSelection.of_this_process()
        )
        with applied_ledger_selection(selection):
            leases = agent_failure_leases_since(
                (moment - cooldown).timestamp(),
                failure=_USAGE_LIMIT_FAILURE,
                checkout_timeout_seconds=checkout_timeout_seconds,
            )
    except Exception as exc:
        logger.warning(
            "harness_availability_ledger_read_failed",
            extra={"detail": f"{type(exc).__name__}: {exc}"},
        )
        return frozenset()
    spent = recently_usage_limited(leases, now=moment, cooldown=cooldown)
    if spent:
        logger.warning(
            "harness_recently_usage_limited",
            extra={"detail": ", ".join(sorted(harness.value for harness in spent))},
        )
    return spent


def check_harness_availability(
    bench: Bench | Staffing | None = None,
    *,
    now: datetime | None = None,
    cooldown: timedelta = USAGE_LIMIT_COOLDOWN,
) -> tuple[HarnessReadiness, ...]:
    """Readiness, narrowed by what this ledger says a harness recently could not do.

    Both halves, for a caller that can pay for both: this is what an operator
    door asks, and a door is the one place where spending a second on subprocess
    probes is the cheapest thing in the interaction.

    Handed a `Staffing`, the probe covers every vendor in play - the seated
    pairing and every pairing it can escape to - because a door that probed only
    the seated vendors would leave each escape `HarnessUnknown`, and an unknown
    escape is one the planner correctly declines to move onto. The door would
    then refuse an outage that a declared, working fallback could have absorbed.

    The ledger half being best-effort is what keeps that composition honest. A
    coordination store that cannot be reached is a condition the door already has
    its own answer for, and failing the availability check because the history
    was unavailable would turn a missing optimisation into a refusal.
    """

    return narrow_by_reported_failures(
        check_frontier_readiness(bench),
        read_spent_quotas(now=now, cooldown=cooldown),
        cooldown=cooldown,
    )


def _unspent_frontier_peer(bench: Bench, spent: frozenset[FrontierHarness]) -> BenchSlot | None:
    """A frontier slot on this bench whose quota the record has not written off.

    Deliberately not `harness_readiness._ready_frontier_peer`. That one wants a
    harness a probe found signed in; this one wants a harness the record has not
    just watched fail. Same shape, different evidence, and they will change for
    different reasons, so one body serving both would mean an edit for one
    silently rewriting the other.

    The local harness is not eligible, for the reason `TierRestaffed` gives: the
    question is whether the replacement can do this tier's work, and a served
    local model standing in for a frontier implementer is the silent substitution
    this whole area exists to avoid rather than an instance of recovering from
    one.

    Walks tiers in declaration order rather than the bench's own, so which peer a
    tier moves to is a property of the staffing model instead of how somebody
    happened to order a TOML file.
    """

    for tier in DispatchTier:
        slot = bench.get(tier)
        if slot is None:
            continue
        kind = classify_harness(slot.harness)
        if isinstance(kind, LocalHarness) or kind in spent:
            continue
        return slot
    return None


def _frontier_models_in_use(
    bench: Bench, *, excluding: DispatchTier
) -> frozenset[tuple[FrontierHarness, str | None]]:
    """The (harness, model) pairs other frontier tiers are holding right now.

    The pair rather than the model alone, for the reason `collapsed_cross_checks`
    already gives: `None` is a model - the harness's own default - and not the
    absence of one. A comparison that dropped it could not see two seats running
    one CLI's default.
    """

    in_use: set[tuple[FrontierHarness, str | None]] = set()
    for tier, slot in bench.items():
        if tier is excluding:
            continue
        kind = classify_harness(slot.harness)
        if isinstance(kind, LocalHarness):
            continue
        in_use.add((kind, slot.model))
    return frozenset(in_use)


def _replacement_for(
    bench: Bench, spent: frozenset[FrontierHarness], *, tier: DispatchTier
) -> BenchSlot | None:
    """Where a spent tier goes, and on which model once it gets there.

    Two decisions, and they are asked in the order an operator declared them.
    The tier's own `backup_models` answer both at once, because a backup names
    its harness as well as its model; only when none applies does
    `_unspent_frontier_peer` answer the harness question by handing back another
    tier's whole slot.

    That order is what makes the backup an escape rather than a garnish. It used
    to run the other way: the peer decided the harness and a backup could only
    override the model, so a bench whose every frontier slot named one spent
    vendor found no peer, returned `None` before reading a backup at all, and
    reported `TierUnstaffable` while holding a written-down way out. Which is
    exactly the bench an operator writes during an outage, so the escape hatch
    was unreachable in the one situation it exists for.

    A backup is skipped when its harness is spent, when its harness is local
    (the substitution `_unspent_frontier_peer` refuses, for the reason
    `TierRestaffed` gives), or when its model is one another frontier tier
    already holds. The last of those is the variance rule the peer path also
    serves: the reason to prefer a backup over the peer's own model is that it
    keeps implementer and reviewer apart, and a backup naming the peer's model
    buys nothing.

    Model ids are unvalidated and passed verbatim, as everywhere in this system -
    the operator proves them with the probe `configs/staffing.toml` documents. An
    id that cannot run on its declared harness fails at spawn, exactly as a typo
    in the primary model does. What can no longer happen is a *correct* id
    reaching the wrong CLI.

    Falls back to the peer's own slot when the tier declares no usable backup, so
    a bench that says nothing behaves exactly as it did.
    """

    configured = bench.get(tier)
    if configured is not None:
        in_use = _frontier_models_in_use(bench, excluding=tier)
        for candidate in configured.backup_models:
            kind = classify_harness(candidate.harness)
            if isinstance(kind, LocalHarness) or kind in spent:
                continue
            if (kind, candidate.model) in in_use:
                continue
            # Built from the configured slot rather than from a peer, because
            # this path has no peer to borrow from. Capacity is the tier's own
            # either way - `TierRestaffed.slot` says how many of a tier may run
            # is a statement about the tier - and the effort knob comes from the
            # backup, since it is a word in the replacement provider's
            # vocabulary and not the spent one's.
            return BenchSlot(
                harness=candidate.harness,
                model=candidate.model,
                capacity=configured.capacity,
                reasoning_effort=candidate.reasoning_effort,
            )
    return _unspent_frontier_peer(bench, spent)


def _paired_tier_staffing(
    staffing: Staffing, spent: frozenset[FrontierHarness], *, hours: int
) -> dict[DispatchTier, TierStaffing]:
    """The two frontier seats, staffed as the one decision they are declared as.

    The pair moves together or not at all. That is not a preference, it is the
    operator's matrix (2026-08-23): when codex goes out, staff moves from Opus
    to Fable even though claude is fine, because "who reviews the implementer"
    changed the moment the implementer did. Per-tier restaffing could not say
    that - it only moved seats whose own harness was spent, so the untouched
    seat kept a reviewer chosen against an implementer that was no longer
    there.

    Where the pair goes is `Staffing.pairing_avoiding`: the first declared
    fallback pairing that avoids every spent harness. The landing is therefore
    itself a checked `FrontierPairing` - implementer and reviewer arrive
    together, already proven distinct - rather than whatever two independent
    escapes happened to compose.

    Nothing declared avoiding the outage means both seats report
    `TierUnstaffable` together. Half-staffing the pair - implementing with no
    reviewer standing behind it - is the failure mode the pairing type exists
    to remove, so it is not produced here either.
    """

    seats = staffing.seated.seats()
    touched = sorted(kind.value for kind in staffing.seated.frontier_harnesses() & spent)
    if not touched:
        return {tier: TierServed(tier=tier, configured=slot) for tier, slot in seats.items()}
    detail = (
        f"{', '.join(touched)} reported a usage limit within the last {hours}h"
        f" and the pair staffs together"
    )
    target = staffing.pairing_avoiding(spent)
    if target is None:
        chain = ", ".join(staffing.seated.fallback) or "none declared"
        return {
            tier: TierUnstaffable(
                tier=tier,
                configured=slot,
                detail=f"{detail}; no fallback pairing avoids it (chain: {chain})",
            )
            for tier, slot in seats.items()
        }
    return {
        tier: TierRestaffed(
            tier=tier,
            configured=slot,
            replacement=target.seats()[tier],
            detail=f"{detail}; the pair moves to pairing {target.name!r}",
        )
        for tier, slot in seats.items()
    }


def staffing_around_spent_quotas(
    bench: Bench | Staffing,
    spent: frozenset[FrontierHarness],
    *,
    cooldown: timedelta = USAGE_LIMIT_COOLDOWN,
) -> tuple[TierStaffing, ...]:
    """How this staffing runs one dispatch, given only what the record reported.

    Separate from `plan_tier_staffing` because the evidence is. That one is handed
    `HarnessReadiness` and answers "may this run start on this machine"; this one
    is handed a set of spent quotas and answers "where should the next milestone
    go". Routing this through the readiness planner would mean inventing a
    `HarnessReady` for every harness nobody probed, which is a claim about being
    signed in that no ledger row supports.

    Handed a `Staffing` - which is what the dispatch path loads - the two
    frontier seats are decided as a pair through `_paired_tier_staffing`, and
    only the solo tiers walk the per-tier rule below. Handed a bare `Bench`,
    every tier walks it: a bench carries no pairing declarations, so per-tier is
    all it can honestly support, and callers that hold one (partial synthetic
    benches in tests, mostly) keep the pre-pairing behavior - a spent tier moves
    to its own harness-typed `backup_models` entry first and an unspent peer's
    slot second.

    Total over the tiers it is given and over nothing else. A dispatch holds
    whatever staffing its runner was built with and has no standing to demand
    more of it, so this walks the tiers that exist and invents nothing.

    `TierUnstaffable` is reachable here and is not a refusal. It says the
    configured seating is spent and nothing declared is any better, and
    `effective_bench` keeps the configured slot for exactly that case, so the
    dispatch goes where it would have gone anyway. Refusing mid-run would strand
    work the door already admitted; a caller is expected to say it out loud
    instead.
    """

    hours = int(cooldown.total_seconds() // 3600)
    paired: dict[DispatchTier, TierStaffing] = {}
    if isinstance(bench, Staffing):
        paired = _paired_tier_staffing(bench, spent, hours=hours)
        bench = bench.bench
    plan: list[TierStaffing] = []
    for tier in DispatchTier:
        if tier in paired:
            plan.append(paired[tier])
            continue
        slot = bench.get(tier)
        if slot is None:
            continue
        kind = classify_harness(slot.harness)
        if isinstance(kind, LocalHarness) or kind not in spent:
            plan.append(TierServed(tier=tier, configured=slot))
            continue
        detail = f"{kind.value} reported a usage limit within the last {hours}h"
        replacement = _replacement_for(bench, spent, tier=tier)
        if replacement is None:
            plan.append(
                TierUnstaffable(
                    tier=tier,
                    configured=slot,
                    detail=(
                        f"{detail}, this tier declares no backup on an unspent harness, "
                        "and no other harness on this bench is unspent"
                    ),
                )
            )
            continue
        plan.append(
            TierRestaffed(tier=tier, configured=slot, replacement=replacement, detail=detail)
        )
    return tuple(plan)


QUOTA_GATE_TTL_SECONDS: Final = 60.0
"""How long the claim gate reuses one quota read.

The gate is asked once per tier per dispatcher sweep, and the sweep is two
seconds, so an uncached gate would be roughly thirty ledger reads a minute for
an answer that changes on the scale of hours. A minute of staleness costs at
most one sweep's delay coming back onto the bench and cannot put a spent harness
back early by any amount that matters.
"""


def build_quota_claim_gate(
    bench: Bench | Staffing,
    *,
    settings: Settings | None = None,
    ttl_seconds: float = QUOTA_GATE_TTL_SECONDS,
    cooldown: timedelta = USAGE_LIMIT_COOLDOWN,
    clock: Any = None,
    now_fn: Any = None,
) -> Any:
    """Answer, for one tier, whether the staffing can seat it at this moment.

    This is the "bench answers first" half that `_NO_FAULT_OUTCOMES` names as the
    precondition for exempting `USAGE_LIMIT` from the attempt budget. A tier
    whose seating is spent with nothing declared to move to is `TierUnstaffable`,
    and claiming an intent for it can only produce one outcome: a dispatch into
    a harness that will refuse, charged to the milestone.

    Handed a `Staffing`, the answer is pair-shaped exactly as the restaffing is:
    the two frontier seats become unclaimable together and claimable together,
    because a pair that could claim implementation while review was unstaffable
    would run the half-escape the pairing type exists to remove.

    Returns a callable rather than a value because the answer expires. A local
    harness is always claimable, and so is a tier this staffing does not seat -
    refusing there would strand work over a quota that cannot apply to it.
    """

    import time as _time

    monotonic = clock or _time.monotonic
    resolved: Bench = bench.bench if isinstance(bench, Staffing) else bench
    state: dict[str, Any] = {"read_at": None, "spent": frozenset()}

    def spent_now() -> frozenset[FrontierHarness]:
        stamp = state["read_at"]
        if stamp is not None and monotonic() - stamp < ttl_seconds:
            return state["spent"]
        try:
            spent = read_spent_quotas(
                now=now_fn() if now_fn else None,
                cooldown=cooldown,
                settings=settings,
                checkout_timeout_seconds=DISPATCH_QUOTA_READ_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - an unreadable ledger must not bench the world
            # Fail open. An unknown quota state is not evidence of a spent one,
            # and a gate that closed on a slow read would stop every dispatch on
            # this machine for as long as the ledger was unwell.
            logger.warning("quota claim gate could not read spent quotas: %s", exc)
            return frozenset()
        state["read_at"] = monotonic()
        state["spent"] = spent
        return spent

    def claimable(tier_value: str | None) -> bool:
        if tier_value is None:
            return True
        try:
            tier = DispatchTier(tier_value)
        except ValueError:
            return True
        slot = resolved.get(tier)
        if slot is None:
            return True
        if isinstance(classify_harness(slot.harness), LocalHarness):
            return True
        for item in staffing_around_spent_quotas(bench, spent_now(), cooldown=cooldown):
            if item.tier is tier:
                return not isinstance(item, TierUnstaffable)
        return True

    return claimable


def collapsed_cross_checks(plan: Iterable[TierStaffing]) -> tuple[str, ...]:
    """The notices for restaffings that put two frontier tiers on one provider.

    A bench that staffs senior and staff on different frontier providers is
    buying something specific: the reviewer is not the model that wrote the
    change. Restaffing around a spent quota spends that property by
    construction, because the replacement is always a provider some other tier
    already uses. At a door a human reads the restaffing notice and decides; on
    a dispatch path it fires per milestone for the whole cooldown behind a
    progress line, so the collapsed property has to be named or it is simply
    gone until somebody wonders why a review agreed with its own implementation.
    """

    items = tuple(plan)
    effective: dict[DispatchTier, FrontierHarness] = {}
    effective_models: dict[DispatchTier, str | None] = {}
    for item in items:
        slot = item.replacement if isinstance(item, TierRestaffed) else item.configured
        kind = classify_harness(slot.harness)
        if not isinstance(kind, LocalHarness):
            effective[item.tier] = kind
            effective_models[item.tier] = slot.model
    notices: list[str] = []
    for item in items:
        if not isinstance(item, TierRestaffed):
            continue
        replacement_kind = classify_harness(item.replacement.harness)
        if isinstance(replacement_kind, LocalHarness):
            continue
        sharing = sorted(
            tier.value
            for tier, harness in effective.items()
            if tier is not item.tier and harness is replacement_kind
        )
        if not sharing:
            continue
        # Sharing a vendor and sharing a model are two different losses, and one
        # message for both was a lie in whichever direction it was wrong. A
        # restaffing that keeps a distinct model still has a reviewer that can
        # disagree with the implementer; saying the cross-check is "collapsed"
        # there understates what survived, and saying it is intact when one model
        # holds both seats overstates it by far more.
        replacement_model = item.replacement.model
        # `None` is a model, not the absence of one: it means the harness's own
        # default, so two slots holding `None` on the same harness are the same
        # model and the pairing is as collapsed as two identical names would be.
        # Treating `None` as unknown would understate exactly the loss this
        # notice exists to report.
        collapsed_onto = sorted(
            tier.value
            for tier, model in effective_models.items()
            if tier is not item.tier
            and effective.get(tier) is replacement_kind
            and model == replacement_model
        )
        named = replacement_model or f"the {replacement_kind.value} default model"
        if collapsed_onto:
            notices.append(
                f"{item.tier.value} now runs {named} on {replacement_kind.value}, "
                f"the same model as {', '.join(collapsed_onto)}; the two-model "
                "cross-check is collapsed for this dispatch and one model is "
                "implementing and reviewing its own change"
            )
            continue
        notices.append(
            f"{item.tier.value} now shares {replacement_kind.value} with "
            f"{', '.join(sharing)}; provider diversity is gone for this dispatch, "
            f"and the cross-check rests on {named} differing from the other "
            "seat's model"
        )
    return tuple(notices)


__all__ = [
    "DISPATCH_QUOTA_READ_TIMEOUT_SECONDS",
    "QUOTA_GATE_TTL_SECONDS",
    "build_quota_claim_gate",
    "parse_quota_reset",
    "USAGE_LIMIT_COOLDOWN",
    "check_harness_availability",
    "collapsed_cross_checks",
    "narrow_by_reported_failures",
    "read_spent_quotas",
    "recently_usage_limited",
    "staffing_around_spent_quotas",
]
