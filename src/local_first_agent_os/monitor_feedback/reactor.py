# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The reactor cycle: ledger facts in, durable decisions out.

Shaped like ``run_lifecycle_maintenance``: one bounded pass, a pure core over
injected state, and one overwritten report.  The two are deliberately disjoint
boundaries.  The janitor repairs state and creates no work; the reactor creates
work and repairs no state.  Keeping that line sharp is what lets either one be
audited without reasoning about the other.

The cycle is a fold, not a map.  Budgets, dedup, and the global cap all depend
on decisions made earlier in the same cycle, so five signals matching a rule
with one remaining daily slot must yield one proposal and four suppressions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from .rules import FeedbackResponse, FeedbackRule, FeedbackRuleCatalog
from .signals import LedgerFactKind, MonitorSignal

CYCLE_SCHEMA_VERSION = "monitor_feedback_cycle_result.v1"
SECONDS_PER_DAY = 24 * 60 * 60


class FeedbackDecision(StrEnum):
    """The durable outcome of evaluating one signal.

    A suppression is recorded with the same fidelity as a proposal, because
    the first question anyone asks a feedback loop is why it did nothing.
    """

    PROPOSED = "PROPOSED"
    DEDUPLICATED = "DEDUPLICATED"
    COOLING_DOWN = "COOLING_DOWN"
    SUPPRESSED_BUDGET = "SUPPRESSED_BUDGET"
    SUPPRESSED_LINEAGE = "SUPPRESSED_LINEAGE"
    ESCALATED_DIGEST = "ESCALATED_DIGEST"


@dataclass(frozen=True, slots=True)
class FeedbackOutcome:
    """One evaluated signal and what the reactor decided about it."""

    signal: MonitorSignal
    decision: FeedbackDecision
    reason: str
    rule_id: str | None = None
    # Filled by the apply step; a pure decision cannot know an id the ledger
    # has not minted yet.
    intent_id: str | None = None

    def with_intent(self, intent_id: str) -> FeedbackOutcome:
        return FeedbackOutcome(
            signal=self.signal,
            decision=self.decision,
            reason=self.reason,
            rule_id=self.rule_id,
            intent_id=intent_id,
        )

    @property
    def prompt_rule(self) -> str:
        return self.rule_id or ""


@dataclass(frozen=True, slots=True)
class CycleSnapshot:
    """Everything the decision core is allowed to know.

    Read once at the top of a cycle so the core stays pure and so a decision
    cannot depend on a row that changed halfway through the fold.
    """

    signals: tuple[MonitorSignal, ...]
    watermarks: Mapping[LedgerFactKind, float]
    open_fingerprints: frozenset[str]
    open_intent_count: int
    last_proposed_at: Mapping[str, float]
    proposals_today: Mapping[str, int]


@dataclass
class _CycleBudget:
    """Mutable in-cycle accounting. Never escapes ``decide_cycle``."""

    open_fingerprints: set[str]
    open_intent_count: int
    proposals_today: dict[str, int]
    global_cap: int
    _reserved: list[str] = field(default_factory=list)

    def at_global_cap(self) -> bool:
        return self.open_intent_count >= self.global_cap

    def at_rule_cap(self, rule: FeedbackRule) -> bool:
        return self.proposals_today.get(rule.rule_id, 0) >= rule.daily_cap

    def reserve(self, rule: FeedbackRule, fingerprint: str) -> None:
        self.open_fingerprints.add(fingerprint)
        self.open_intent_count += 1
        self.proposals_today[rule.rule_id] = self.proposals_today.get(rule.rule_id, 0) + 1


def decide_cycle(
    snapshot: CycleSnapshot,
    catalog: FeedbackRuleCatalog,
    now: float,
) -> tuple[FeedbackOutcome, ...]:
    """Fold signals into decisions. Pure: no clock, no ledger, no I/O.

    Gate order is dedup, cooldown, lineage, budget, recording the first
    suppression that applies, per ``docs/monitor_feedback_loop_design.md``.
    """

    budget = _CycleBudget(
        open_fingerprints=set(snapshot.open_fingerprints),
        open_intent_count=snapshot.open_intent_count,
        proposals_today=dict(snapshot.proposals_today),
        global_cap=catalog.global_open_intent_cap,
    )
    outcomes: list[FeedbackOutcome] = []
    for signal in sorted(snapshot.signals, key=lambda item: (item.observed_at, item.fingerprint)):
        outcomes.append(_decide_one(signal, catalog, budget, snapshot, now))
    return tuple(outcomes)


def _decide_one(
    signal: MonitorSignal,
    catalog: FeedbackRuleCatalog,
    budget: _CycleBudget,
    snapshot: CycleSnapshot,
    now: float,
) -> FeedbackOutcome:
    rule = catalog.match(signal)
    if rule is None:
        return FeedbackOutcome(
            signal=signal,
            decision=FeedbackDecision.ESCALATED_DIGEST,
            reason="no enabled rule matched this signal",
        )
    if rule.response is FeedbackResponse.DIGEST_ONLY:
        # A digest entry creates no work, so it is not gated. The row itself is
        # bounded by the fact stream, which is what the watermark bounds.
        return FeedbackOutcome(
            signal=signal,
            decision=FeedbackDecision.ESCALATED_DIGEST,
            reason=f"rule '{rule.rule_id}' is digest_only",
            rule_id=rule.rule_id,
        )

    if signal.fingerprint in budget.open_fingerprints:
        return FeedbackOutcome(
            signal=signal,
            decision=FeedbackDecision.DEDUPLICATED,
            reason="a live intent already exists for this fingerprint",
            rule_id=rule.rule_id,
        )

    last_proposed = snapshot.last_proposed_at.get(signal.fingerprint)
    if last_proposed is not None and now - last_proposed < rule.cooldown_seconds:
        remaining = rule.cooldown_seconds - (now - last_proposed)
        return FeedbackOutcome(
            signal=signal,
            decision=FeedbackDecision.COOLING_DOWN,
            reason=f"rule '{rule.rule_id}' cooldown has {remaining:.0f}s remaining",
            rule_id=rule.rule_id,
        )

    if signal.caused_by_feedback:
        # Lineage depth is bounded at one. The system may react to the world
        # and may tell the operator its reaction failed; it may not react to
        # its own reactions. This makes runaway amplification unrepresentable
        # rather than merely rate limited.
        return FeedbackOutcome(
            signal=signal,
            decision=FeedbackDecision.SUPPRESSED_LINEAGE,
            reason="signal originates from feedback-dispatched work",
            rule_id=rule.rule_id,
        )

    if budget.at_global_cap():
        return FeedbackOutcome(
            signal=signal,
            decision=FeedbackDecision.SUPPRESSED_BUDGET,
            reason=(
                f"global open-intent cap {budget.global_cap} reached; "
                "escalating to the digest instead of proposing"
            ),
            rule_id=rule.rule_id,
        )
    if budget.at_rule_cap(rule):
        return FeedbackOutcome(
            signal=signal,
            decision=FeedbackDecision.SUPPRESSED_BUDGET,
            reason=f"rule '{rule.rule_id}' daily cap {rule.daily_cap} reached",
            rule_id=rule.rule_id,
        )

    budget.reserve(rule, signal.fingerprint)
    return FeedbackOutcome(
        signal=signal,
        decision=FeedbackDecision.PROPOSED,
        reason=f"rule '{rule.rule_id}' proposed an advisory diagnosis",
        rule_id=rule.rule_id,
    )


class ReactorLedger(Protocol):
    """The reactor's whole dependency on durable state.

    Narrow on purpose: a port this small is trivially faked in tests, and it
    names exactly what the reactor is allowed to touch.  Nothing here claims an
    intent, resolves an approval, merges, or deploys.
    """

    def read_snapshot(self, kinds: Sequence[LedgerFactKind]) -> CycleSnapshot: ...

    def submit_advisory_intent(self, rule: FeedbackRule, signal: MonitorSignal) -> str: ...

    def commit_cycle(
        self,
        outcomes: Sequence[FeedbackOutcome],
        watermarks: Mapping[LedgerFactKind, float],
    ) -> None: ...


class DryRunReactorLedger:
    """Reads through to a real ledger and drops every write.

    An implementation rather than a ``dry_run`` boolean threaded through the
    cycle: the flag would have to be re-checked at each write site, and every
    such check is a place a future write can forget to ask.  Here, a dry run
    cannot write because it has no code that writes.
    """

    def __init__(self, inner: ReactorLedger) -> None:
        self._inner = inner

    def read_snapshot(self, kinds: Sequence[LedgerFactKind]) -> CycleSnapshot:
        return self._inner.read_snapshot(kinds)

    def submit_advisory_intent(self, rule: FeedbackRule, signal: MonitorSignal) -> str:
        return f"dry-run:{rule.rule_id}:{signal.fingerprint}"

    def commit_cycle(
        self,
        outcomes: Sequence[FeedbackOutcome],
        watermarks: Mapping[LedgerFactKind, float],
    ) -> None:
        return None


def run_feedback_cycle(
    ledger: ReactorLedger,
    catalog: FeedbackRuleCatalog,
    *,
    now: float,
    kinds: Sequence[LedgerFactKind] | None = None,
) -> dict[str, Any]:
    """Run one cycle and return a bounded report.

    Ordering is load bearing.  Intents are submitted before the decision rows
    commit, so a crash between the two re-proposes and the fingerprint dedup
    absorbs it.  The reverse order would record a proposal that never became
    work, which no later cycle could detect.
    """

    kinds = tuple(kinds if kinds is not None else LedgerFactKind)
    snapshot = ledger.read_snapshot(kinds)
    outcomes = decide_cycle(snapshot, catalog, now)

    applied: list[FeedbackOutcome] = []
    rules_by_id = {rule.rule_id: rule for rule in catalog.rules}
    for outcome in outcomes:
        if outcome.decision is not FeedbackDecision.PROPOSED:
            applied.append(outcome)
            continue
        rule = rules_by_id[outcome.prompt_rule]
        intent_id = ledger.submit_advisory_intent(rule, outcome.signal)
        applied.append(outcome.with_intent(intent_id))

    watermarks = _advanced_watermarks(snapshot, kinds)
    ledger.commit_cycle(applied, watermarks)
    return _report(applied, watermarks, catalog, now)


def _advanced_watermarks(
    snapshot: CycleSnapshot,
    kinds: Sequence[LedgerFactKind],
) -> dict[LedgerFactKind, float]:
    """Advance each kind to the newest fact this cycle actually evaluated.

    A kind with no new facts keeps its mark rather than jumping to ``now``,
    so a fact written with a slightly older timestamp is still collected.
    """

    advanced: dict[LedgerFactKind, float] = {}
    for kind in kinds:
        observed = [signal.observed_at for signal in snapshot.signals if signal.kind is kind]
        if observed:
            advanced[kind] = max(observed)
    return advanced


def _report(
    outcomes: Sequence[FeedbackOutcome],
    watermarks: Mapping[LedgerFactKind, float],
    catalog: FeedbackRuleCatalog,
    now: float,
) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.decision.value] = counts.get(outcome.decision.value, 0) + 1
    return {
        "schema_version": CYCLE_SCHEMA_VERSION,
        "ran_at": datetime.fromtimestamp(now, tz=UTC).isoformat(),
        "status": "COMPLETED",
        "catalog_path": catalog.source_path,
        "rules_enabled": sum(1 for rule in catalog.rules if rule.enabled),
        "signals_evaluated": len(outcomes),
        # Present even at zero, so "proposed nothing" is distinguishable from
        # "was never going to". The same reason the maintenance report carries
        # retention_seconds.
        "decisions": {
            decision.value: counts.get(decision.value, 0) for decision in FeedbackDecision
        },
        "proposed_intent_ids": [
            outcome.intent_id
            for outcome in outcomes
            if outcome.decision is FeedbackDecision.PROPOSED and outcome.intent_id
        ],
        "watermarks": {kind.value: mark for kind, mark in watermarks.items()},
    }
