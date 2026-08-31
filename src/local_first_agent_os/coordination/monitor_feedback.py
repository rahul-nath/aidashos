# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Ledger access for the monitor feedback reactor.

This module owns every SQL statement the reactor runs, which keeps the decision
core in ``monitor_feedback.reactor`` pure and keeps coordination the single
place that knows the ledger's shape.

Only ``PENDING`` dispatch intents and ``monitor_feedback_events`` rows are ever
written here.  Nothing claims an intent, terminalizes a lease, resolves an
approval, merges, or deploys: the reactor proposes, and the existing gates
decide.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

from ..contracts import DispatchIntentStatus, MilestoneStatus
from ..monitor_feedback.reactor import (
    SECONDS_PER_DAY,
    CycleSnapshot,
    FeedbackDecision,
    FeedbackOutcome,
)
from ..monitor_feedback.rules import FeedbackRule
from ..monitor_feedback.signals import (
    EvidenceRef,
    LedgerFactKind,
    LedgerFactSignal,
    MonitorSignal,
    Severity,
)
from .contracts import DispatchKind
from .dispatch import submit_dispatch_intent
from .outcomes import failure_category
from .store import connect, now, tx

FEEDBACK_SOURCE_PREFIX = "monitor_feedback"

# Live means "an operator has not yet seen this proposal through". A terminal
# intent releases its fingerprint so the same condition may propose again once
# the rule's cooldown has also elapsed.
_LIVE_INTENT_STATUSES = ("PENDING", "CLAIMED", "PAUSED")


def feedback_intent_source(rule_id: str, fingerprint: str) -> str:
    """Build the lineage marker.

    This string is parsed by ``_is_feedback_source`` to bound lineage depth, so
    it is a compatibility surface rather than a log line.  Changing its shape
    silently disables loop breaking.
    """

    return f"{FEEDBACK_SOURCE_PREFIX}:{rule_id}:{fingerprint}"


def _is_feedback_source(source: str | None) -> bool:
    return bool(source) and str(source).startswith(f"{FEEDBACK_SOURCE_PREFIX}:")


def _fingerprint_from_source(source: str | None) -> str | None:
    if not _is_feedback_source(source):
        return None
    parts = str(source).split(":", 2)
    return parts[2] if len(parts) == 3 and parts[2] else None


def _collect_failed_milestones(c: Any, since: float) -> list[MonitorSignal]:
    rows = c.execute(
        f"""
        SELECT m.milestone_id, m.saga_id, m.name, m.outcome, m.updated_at,
               m.dispatch_intent_id, d.source AS intent_source,
               d.target_project_id AS target_project_id
        FROM saga_milestones m
        LEFT JOIN dispatch_intents d ON d.intent_id = m.dispatch_intent_id
        WHERE m.status = '{MilestoneStatus.FAILED}' AND m.updated_at > ?
        ORDER BY m.updated_at
        """,
        (since,),
    ).fetchall()
    signals: list[MonitorSignal] = []
    for raw in rows:
        row = dict(raw)
        error_code = str(row.get("outcome") or "UNKNOWN_FAILURE")
        signals.append(
            LedgerFactSignal(
                kind=LedgerFactKind.MILESTONE_FAILED,
                severity=Severity.WARNING,
                # Identity, not message: the same milestone failing the same
                # way twice must collapse onto one fingerprint.
                identity=(str(row["saga_id"]), str(row["milestone_id"]), error_code),
                observed_at=float(row["updated_at"]),
                evidence=EvidenceRef(table="saga_milestones", row_id=str(row["milestone_id"])),
                target_project_id=row.get("target_project_id"),
                failure_category=failure_category(error_code),
                error_code=error_code,
                summary=f"milestone '{row.get('name') or row['milestone_id']}' failed",
                caused_by_feedback=_is_feedback_source(row.get("intent_source")),
            )
        )
    return signals


def _collect_failed_dispatch_intents(c: Any, since: float) -> list[MonitorSignal]:
    """Collect every terminally failed dispatch intent. One attempt, one signal.

    A milestone and the intent under it are two conditions, not one, and neither
    subsumes the other.  A milestone failure is recoverable through
    ``retry_saga_milestone``; a single failed attempt usually needs nothing at
    all, because the next retry may well fix it.  Emitting both is correct
    collection.  Whether both deserve a *task* is policy, and policy lives in
    ``configs/feedback_rules.toml``, which routes this kind to the digest.

    Note what this is not: exhaustion.  Retries in this ledger are new intent
    rows sharing one ``approved_gawd:...:milestone:...`` source, so exhaustion
    is a property of that attempt sequence and no single-row query can see it.
    That kind needs its own collector and a definition pinned against
    ``DISPATCH_RETRY_POLICY``; it is deliberately not this one wearing its name.
    """

    rows = c.execute(
        f"""
        SELECT intent_id, source, outcome, error, target_project_id,
               COALESCE(completed_at, created_at) AS observed_at
        FROM dispatch_intents
        WHERE status = '{DispatchIntentStatus.FAILED}' AND COALESCE(completed_at, created_at) > ?
        ORDER BY COALESCE(completed_at, created_at)
        """,
        (since,),
    ).fetchall()
    signals: list[MonitorSignal] = []
    for raw in rows:
        row = dict(raw)
        error_code = str(row.get("outcome") or "UNKNOWN_FAILURE")
        signals.append(
            LedgerFactSignal(
                kind=LedgerFactKind.DISPATCH_INTENT_FAILED,
                severity=Severity.WARNING,
                identity=(str(row["intent_id"]), error_code),
                observed_at=float(row["observed_at"]),
                evidence=EvidenceRef(table="dispatch_intents", row_id=str(row["intent_id"])),
                target_project_id=row.get("target_project_id"),
                failure_category=failure_category(error_code),
                error_code=error_code,
                summary=f"dispatch intent failed with {error_code}",
                caused_by_feedback=_is_feedback_source(row.get("source")),
            )
        )
    return signals


# Every kind must have a collector. A kind without one is a rule an operator
# can write that can never fire, which is the "quietly off" failure the design
# rules out. The assertion below makes adding one without the other fail at
# import rather than at 3am.
_COLLECTORS = {
    LedgerFactKind.MILESTONE_FAILED: _collect_failed_milestones,
    LedgerFactKind.DISPATCH_INTENT_FAILED: _collect_failed_dispatch_intents,
}

_missing = set(LedgerFactKind) - set(_COLLECTORS)
if _missing:  # pragma: no cover - import-time contract
    raise RuntimeError(
        f"LedgerFactKind members without a collector: {sorted(k.value for k in _missing)}"
    )


class CoordinationReactorLedger:
    """The real ``ReactorLedger``.

    One read and one write per cycle. Named for the coordination ledger rather
    than for Postgres because it goes through the store rather than a driver.
    """

    def read_snapshot(self, kinds: Sequence[LedgerFactKind]) -> CycleSnapshot:
        with connect() as c:
            watermarks = {kind: self._watermark(c, kind) for kind in kinds}
            signals: list[MonitorSignal] = []
            for kind in kinds:
                signals.extend(_COLLECTORS[kind](c, watermarks[kind]))

            live = [
                dict(row)
                for row in c.execute(
                    "SELECT source FROM dispatch_intents WHERE status IN "
                    f"({', '.join('?' for _ in _LIVE_INTENT_STATUSES)})",
                    _LIVE_INTENT_STATUSES,
                ).fetchall()
            ]
            open_fingerprints = {
                fingerprint
                for fingerprint in (_fingerprint_from_source(row["source"]) for row in live)
                if fingerprint
            }
            open_intent_count = sum(1 for row in live if _is_feedback_source(row["source"]))

            last_proposed = {
                str(dict(row)["fingerprint"]): float(dict(row)["last_at"])
                for row in c.execute(
                    "SELECT fingerprint, MAX(created_at) AS last_at "
                    "FROM monitor_feedback_events WHERE decision = ? "
                    "GROUP BY fingerprint",
                    (FeedbackDecision.PROPOSED.value,),
                ).fetchall()
            }
            day_ago = now() - SECONDS_PER_DAY
            proposals_today = {
                str(dict(row)["rule_id"]): int(dict(row)["n"])
                for row in c.execute(
                    "SELECT rule_id, COUNT(*) AS n FROM monitor_feedback_events "
                    "WHERE decision = ? AND created_at > ? AND rule_id IS NOT NULL "
                    "GROUP BY rule_id",
                    (FeedbackDecision.PROPOSED.value, day_ago),
                ).fetchall()
            }

        return CycleSnapshot(
            signals=tuple(signals),
            watermarks=watermarks,
            open_fingerprints=frozenset(open_fingerprints),
            open_intent_count=open_intent_count,
            last_proposed_at=last_proposed,
            proposals_today=proposals_today,
        )

    @staticmethod
    def _watermark(c: Any, kind: LedgerFactKind) -> float:
        row = c.execute(
            "SELECT observed_at FROM monitor_feedback_watermarks WHERE signal_kind = ?",
            (kind.value,),
        ).fetchone()
        return float(dict(row)["observed_at"]) if row else 0.0

    def submit_advisory_intent(self, rule: FeedbackRule, signal: MonitorSignal) -> str:
        result = submit_dispatch_intent(
            tier=rule.tier.value,
            prompt=rule.render_prompt(signal),
            kind=DispatchKind.ADVISORY,
            target_project_id=signal.target_project_id,
            source=feedback_intent_source(rule.rule_id, signal.fingerprint),
        )
        if not result.get("ok"):
            # A rejected submit is a programmer error here: the tier came from
            # the bench and the kind is an enum member, so both were validated
            # before this call could be reached.
            raise RuntimeError(f"feedback intent submission rejected: {result}")
        return str(result["intent_id"])

    def commit_cycle(
        self,
        outcomes: Sequence[FeedbackOutcome],
        watermarks: Mapping[LedgerFactKind, float],
    ) -> None:
        """Write decisions and advance watermarks in one transaction.

        Atomic together so a crash cannot advance past a fact whose decision
        was never recorded.  A crash before this commits re-evaluates the same
        facts next cycle, and the fingerprint dedup absorbs the repeat.
        """

        t = now()
        with tx() as c:
            for outcome in outcomes:
                signal = outcome.signal
                c.execute(
                    """
                    INSERT INTO monitor_feedback_events(
                        feedback_event_id, fingerprint, signal_source, signal_kind,
                        severity, target_project_id, rule_id, decision, intent_id,
                        approval_id, evidence_json, observed_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        signal.fingerprint,
                        signal.source.value,
                        signal.kind.value,
                        signal.severity.value,
                        signal.target_project_id,
                        outcome.rule_id,
                        outcome.decision.value,
                        outcome.intent_id,
                        None,
                        json.dumps(
                            {**signal.to_payload(), "reason": outcome.reason},
                            sort_keys=True,
                        ),
                        signal.observed_at,
                        t,
                    ),
                )
            for kind, observed_at in watermarks.items():
                self._upsert_watermark(c, kind, observed_at, t)

    @staticmethod
    def _upsert_watermark(c: Any, kind: LedgerFactKind, observed_at: float, t: float) -> None:
        # Update-then-insert rather than ON CONFLICT: the watermark only ever
        # moves forward, and the `observed_at <` predicate on the update is what
        # says so. An upsert would need the same guard written twice.
        updated = c.execute(
            "UPDATE monitor_feedback_watermarks SET observed_at = ?, updated_at = ? "
            "WHERE signal_kind = ? AND observed_at < ?",
            (observed_at, t, kind.value, observed_at),
        ).rowcount
        if updated:
            return
        existing = c.execute(
            "SELECT 1 FROM monitor_feedback_watermarks WHERE signal_kind = ?",
            (kind.value,),
        ).fetchone()
        if existing is None:
            c.execute(
                "INSERT INTO monitor_feedback_watermarks(signal_kind, observed_at, updated_at) "
                "VALUES (?, ?, ?)",
                (kind.value, observed_at, t),
            )


def list_monitor_feedback_events(limit: int = 50) -> list[dict[str, Any]]:
    """Read recent decisions, newest first. The operator's view of the loop."""

    with connect() as c:
        return [
            dict(row)
            for row in c.execute(
                "SELECT feedback_event_id, fingerprint, signal_kind, severity, "
                "target_project_id, rule_id, decision, intent_id, observed_at, created_at "
                "FROM monitor_feedback_events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ]
