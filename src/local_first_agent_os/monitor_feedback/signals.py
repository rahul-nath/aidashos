# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Typed monitor signals and their stable fingerprints.

A signal is a sum over sources, not a stringly-typed event bag.  Phase 1 carries
one variant, ``LedgerFactSignal``; ``SignalSource`` names the axis that
``PrometheusAlertSignal`` will join in Phase 2 without reshaping anything here.

The fingerprint is the identity of a *condition*, never of a message.  Two
retries of the same failing milestone must collapse onto one fingerprint or the
reactor proposes one diagnosis task per retry, which is the alert-storm failure
``docs/monitor_feedback_loop_design.md`` exists to exclude structurally.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..coordination.outcomes import FailureCategory

FINGERPRINT_SCHEMA_VERSION = "monitor_signal_fingerprint.v1"


class SignalSource(StrEnum):
    """Where a signal came from. Closed: an unknown source is a crash."""

    LEDGER_FACT = "LEDGER_FACT"


class LedgerFactKind(StrEnum):
    """Ledger conditions the reactor can observe.

    Deliberately only the kinds Phase 1 actually collects.  Declaring a kind
    with no collector would let an operator write a rule that can never fire,
    and a feedback loop that is quietly off is worse than one that is loudly
    broken.  ``collectors.py`` asserts this enum and its collector registry
    stay in lockstep, so adding a member without a collector fails at import.
    """

    MILESTONE_FAILED = "MILESTONE_FAILED"
    DISPATCH_INTENT_FAILED = "DISPATCH_INTENT_FAILED"


class Severity(StrEnum):
    """How much operator attention the condition deserves.

    ``WARNING`` means the system may still recover on its own: another retry,
    or a ``retry_saga_milestone``.  ``CRITICAL`` means automatic recovery is
    already exhausted, so nothing improves without a decision.

    Phase 1 produces only ``WARNING``.  Nothing it collects can prove
    exhaustion: retries are separate intent rows sharing one milestone source,
    so exhaustion is a property of an attempt sequence that no Phase 1 collector
    reads.  ``CRITICAL`` arrives with the kind that can establish it.  Unlike a
    ``LedgerFactKind`` with no collector, this gap is safe, because a rule
    selecting an unproduced severity simply never matches, and an unmatched
    signal is still recorded as ``ESCALATED_DIGEST`` rather than vanishing.
    """

    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """A typed pointer to the durable row that justifies a signal.

    Carrying the reference rather than a copy keeps the reactor's evidence
    artifact bounded, and keeps Postgres the single authority for the fact.
    """

    table: str
    row_id: str

    def to_payload(self) -> dict[str, str]:
        return {"table": self.table, "row_id": self.row_id}


@dataclass(frozen=True, slots=True)
class LedgerFactSignal:
    """One observed ledger condition, normalized.

    ``identity`` is what the fingerprint hashes.  It is a tuple of typed
    condition fields chosen per kind, never the error message, because message
    text varies across retries of one condition and would defeat dedup.
    """

    kind: LedgerFactKind
    severity: Severity
    identity: tuple[str, ...]
    observed_at: float
    evidence: EvidenceRef
    target_project_id: str | None = None
    failure_category: FailureCategory | None = None
    error_code: str = ""
    summary: str = ""
    # Set when the fact's causal chain terminates in feedback-dispatched work.
    # Lineage is established at collection time, where the intent source is in
    # hand; deciding it later would mean re-querying what we already read.
    caused_by_feedback: bool = False

    source: SignalSource = SignalSource.LEDGER_FACT

    @property
    def fingerprint(self) -> str:
        return fingerprint_of(self.kind, self.identity)

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": self.source.value,
            "kind": self.kind.value,
            "severity": self.severity.value,
            "fingerprint": self.fingerprint,
            "identity": list(self.identity),
            "observed_at": self.observed_at,
            "evidence": self.evidence.to_payload(),
            "target_project_id": self.target_project_id,
            "failure_category": (
                self.failure_category.value if self.failure_category is not None else None
            ),
            "error_code": self.error_code,
            "summary": self.summary,
            "caused_by_feedback": self.caused_by_feedback,
        }


# Phase 1 has one variant. The alias is the seam Phase 2 widens, so call sites
# already name the sum rather than the single member.
MonitorSignal = LedgerFactSignal


def fingerprint_of(kind: LedgerFactKind, identity: tuple[str, ...]) -> str:
    """Hash a condition's identity into a stable, collision-checkable key.

    The kind is part of the hash so that two different kinds sharing an id
    space (a milestone id and an intent id are both UUIDs) cannot collide.
    """

    if not identity:
        raise ValueError(f"{kind.value} signal identity must not be empty")
    if any(not part for part in identity):
        raise ValueError(f"{kind.value} signal identity must not contain empty parts")
    digest = hashlib.sha256()
    digest.update(FINGERPRINT_SCHEMA_VERSION.encode("utf-8"))
    for part in (kind.value, *identity):
        # Length-prefixed so ("ab", "c") and ("a", "bc") cannot hash alike.
        digest.update(f"{len(part)}:{part}".encode())
    return digest.hexdigest()[:32]
