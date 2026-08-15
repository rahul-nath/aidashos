# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Monitor-to-Plan feedback: durable signals become proposed work, gated.

Phase 1 of ``docs/monitor_feedback_loop_design.md``.  The reactor proposes; it
never claims an intent, merges, deploys, or resolves an approval.
"""

from .reactor import (
    CycleSnapshot,
    FeedbackDecision,
    FeedbackOutcome,
    ReactorLedger,
    decide_cycle,
    run_feedback_cycle,
)
from .rules import (
    FeedbackResponse,
    FeedbackRule,
    FeedbackRuleCatalog,
    FeedbackRuleError,
    RuleSelector,
    load_feedback_rules,
)
from .signals import (
    EvidenceRef,
    LedgerFactKind,
    LedgerFactSignal,
    MonitorSignal,
    Severity,
    SignalSource,
    fingerprint_of,
)

__all__ = [
    "CycleSnapshot",
    "EvidenceRef",
    "FeedbackDecision",
    "FeedbackOutcome",
    "FeedbackResponse",
    "FeedbackRule",
    "FeedbackRuleCatalog",
    "FeedbackRuleError",
    "LedgerFactKind",
    "LedgerFactSignal",
    "MonitorSignal",
    "ReactorLedger",
    "RuleSelector",
    "Severity",
    "SignalSource",
    "decide_cycle",
    "fingerprint_of",
    "load_feedback_rules",
    "run_feedback_cycle",
]
