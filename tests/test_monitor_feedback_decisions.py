# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The pure decision core: fingerprints, gate order, and the in-cycle fold.

No database here on purpose.  ``decide_cycle`` takes a snapshot and a clock and
returns decisions, which is what makes suppression *ordering* testable at all:
a signal can be simultaneously a duplicate, cooling down, feedback-caused, and
over budget, and exactly one of those must be recorded.
"""

from __future__ import annotations

import pytest

from local_first_agent_os.coordination.outcomes import FailureCategory
from local_first_agent_os.monitor_feedback.reactor import (
    CycleSnapshot,
    FeedbackDecision,
    decide_cycle,
)
from local_first_agent_os.monitor_feedback.rules import (
    FeedbackResponse,
    FeedbackRule,
    FeedbackRuleCatalog,
    RuleSelector,
)
from local_first_agent_os.monitor_feedback.signals import (
    EvidenceRef,
    LedgerFactKind,
    LedgerFactSignal,
    Severity,
    fingerprint_of,
)
from local_first_agent_os.vocabulary import DispatchTier

NOW = 10_000.0


def _signal(
    *,
    identity: tuple[str, ...] = ("saga-1", "milestone-1", "PROCESS_FAILED"),
    kind: LedgerFactKind = LedgerFactKind.MILESTONE_FAILED,
    observed_at: float = 900.0,
    caused_by_feedback: bool = False,
) -> LedgerFactSignal:
    return LedgerFactSignal(
        kind=kind,
        severity=Severity.WARNING,
        identity=identity,
        observed_at=observed_at,
        evidence=EvidenceRef(table="saga_milestones", row_id=identity[-2]),
        target_project_id="pest_site_factory",
        failure_category=FailureCategory.INFRASTRUCTURE,
        error_code=identity[-1],
        caused_by_feedback=caused_by_feedback,
    )


def _catalog(
    *,
    cooldown: float = 0.0,
    daily_cap: int = 10,
    global_cap: int = 10,
    response: FeedbackResponse = FeedbackResponse.ADVISORY,
) -> FeedbackRuleCatalog:
    return FeedbackRuleCatalog(
        rules=(
            FeedbackRule(
                rule_id="diagnose",
                selector=RuleSelector(signal_kind=LedgerFactKind.MILESTONE_FAILED),
                response=response,
                tier=DispatchTier.JUNIOR,
                cooldown_seconds=cooldown,
                daily_cap=daily_cap,
                prompt_template="diagnose {error_code}",
            ),
        ),
        global_open_intent_cap=global_cap,
        source_path="<test>",
    )


def _snapshot(
    signals: tuple[LedgerFactSignal, ...],
    *,
    open_fingerprints: frozenset[str] = frozenset(),
    open_intent_count: int = 0,
    last_proposed_at: dict[str, float] | None = None,
    proposals_today: dict[str, int] | None = None,
) -> CycleSnapshot:
    return CycleSnapshot(
        signals=signals,
        watermarks={},
        open_fingerprints=open_fingerprints,
        open_intent_count=open_intent_count,
        last_proposed_at=last_proposed_at or {},
        proposals_today=proposals_today or {},
    )


def _decisions(outcomes) -> list[FeedbackDecision]:
    return [outcome.decision for outcome in outcomes]


# --- Fingerprints ------------------------------------------------------------


def test_the_same_condition_always_hashes_the_same() -> None:
    assert _signal().fingerprint == _signal().fingerprint


def test_different_kinds_sharing_an_id_do_not_collide() -> None:
    """Milestone ids and intent ids are both UUIDs from the same generator."""

    shared = ("abc123", "PROCESS_FAILED")
    assert fingerprint_of(LedgerFactKind.MILESTONE_FAILED, shared) != fingerprint_of(
        LedgerFactKind.DISPATCH_INTENT_FAILED, shared
    )


def test_identity_parts_are_length_prefixed() -> None:
    """Without length prefixing, ("ab","c") and ("a","bc") would hash alike."""

    assert fingerprint_of(LedgerFactKind.MILESTONE_FAILED, ("ab", "c")) != fingerprint_of(
        LedgerFactKind.MILESTONE_FAILED, ("a", "bc")
    )


def test_a_different_error_code_is_a_different_condition() -> None:
    first = _signal(identity=("saga-1", "milestone-1", "PROCESS_FAILED"))
    second = _signal(identity=("saga-1", "milestone-1", "ARGUMENT_LIST_TOO_LONG"))
    assert first.fingerprint != second.fingerprint


@pytest.mark.parametrize("identity", [(), ("saga-1", "")])
def test_an_unusable_identity_raises_rather_than_hashing_nothing(identity: tuple) -> None:
    with pytest.raises(ValueError, match="identity"):
        fingerprint_of(LedgerFactKind.MILESTONE_FAILED, identity)


# --- Gate order --------------------------------------------------------------


def test_a_matching_signal_with_room_is_proposed() -> None:
    outcomes = decide_cycle(_snapshot((_signal(),)), _catalog(), NOW)
    assert _decisions(outcomes) == [FeedbackDecision.PROPOSED]
    assert outcomes[0].rule_id == "diagnose"


def test_dedup_beats_cooldown_lineage_and_budget() -> None:
    """All four gates apply; only the first in order may be recorded."""

    signal = _signal(caused_by_feedback=True)
    outcomes = decide_cycle(
        _snapshot(
            (signal,),
            open_fingerprints=frozenset({signal.fingerprint}),
            open_intent_count=99,
            last_proposed_at={signal.fingerprint: NOW - 1},
            proposals_today={"diagnose": 99},
        ),
        _catalog(cooldown=3600, daily_cap=1, global_cap=1),
        NOW,
    )
    assert _decisions(outcomes) == [FeedbackDecision.DEDUPLICATED]


def test_cooldown_beats_lineage_and_budget() -> None:
    signal = _signal(caused_by_feedback=True)
    outcomes = decide_cycle(
        _snapshot(
            (signal,),
            open_intent_count=99,
            last_proposed_at={signal.fingerprint: NOW - 10},
            proposals_today={"diagnose": 99},
        ),
        _catalog(cooldown=3600, daily_cap=1, global_cap=1),
        NOW,
    )
    assert _decisions(outcomes) == [FeedbackDecision.COOLING_DOWN]


def test_lineage_beats_budget() -> None:
    outcomes = decide_cycle(
        _snapshot((_signal(caused_by_feedback=True),), open_intent_count=99),
        _catalog(global_cap=1),
        NOW,
    )
    assert _decisions(outcomes) == [FeedbackDecision.SUPPRESSED_LINEAGE]


def test_an_elapsed_cooldown_lets_the_condition_propose_again() -> None:
    signal = _signal()
    outcomes = decide_cycle(
        _snapshot((signal,), last_proposed_at={signal.fingerprint: NOW - 3601}),
        _catalog(cooldown=3600),
        NOW,
    )
    assert _decisions(outcomes) == [FeedbackDecision.PROPOSED]


def test_an_unmatched_signal_reaches_the_digest_rather_than_vanishing() -> None:
    outcomes = decide_cycle(
        _snapshot((_signal(kind=LedgerFactKind.DISPATCH_INTENT_FAILED),)),
        _catalog(),
        NOW,
    )
    assert _decisions(outcomes) == [FeedbackDecision.ESCALATED_DIGEST]
    assert outcomes[0].rule_id is None


def test_digest_only_rules_are_not_gated_because_they_create_no_work() -> None:
    signal = _signal()
    outcomes = decide_cycle(
        _snapshot(
            (signal,),
            open_fingerprints=frozenset({signal.fingerprint}),
            open_intent_count=99,
        ),
        _catalog(response=FeedbackResponse.DIGEST_ONLY, global_cap=1),
        NOW,
    )
    assert _decisions(outcomes) == [FeedbackDecision.ESCALATED_DIGEST]
    assert outcomes[0].rule_id == "diagnose"


# --- The in-cycle fold -------------------------------------------------------


def test_a_daily_cap_is_spent_within_one_cycle() -> None:
    """Five signals and one remaining slot must not become five proposals.

    This is why the cycle is a fold: a map over signals would read the same
    starting count five times and propose five times.
    """

    signals = tuple(
        _signal(identity=("saga-1", f"milestone-{index}", "PROCESS_FAILED")) for index in range(5)
    )
    outcomes = decide_cycle(
        _snapshot(signals, proposals_today={"diagnose": 2}),
        _catalog(daily_cap=3),
        NOW,
    )
    assert _decisions(outcomes) == [
        FeedbackDecision.PROPOSED,
        FeedbackDecision.SUPPRESSED_BUDGET,
        FeedbackDecision.SUPPRESSED_BUDGET,
        FeedbackDecision.SUPPRESSED_BUDGET,
        FeedbackDecision.SUPPRESSED_BUDGET,
    ]


def test_the_global_cap_bounds_every_rule_together() -> None:
    signals = tuple(
        _signal(identity=("saga-1", f"milestone-{index}", "PROCESS_FAILED")) for index in range(3)
    )
    outcomes = decide_cycle(
        _snapshot(signals, open_intent_count=1),
        _catalog(daily_cap=99, global_cap=2),
        NOW,
    )
    assert _decisions(outcomes) == [
        FeedbackDecision.PROPOSED,
        FeedbackDecision.SUPPRESSED_BUDGET,
        FeedbackDecision.SUPPRESSED_BUDGET,
    ]


def test_two_signals_for_one_condition_collapse_inside_a_cycle() -> None:
    """Dedup must consider proposals made earlier in this same cycle."""

    signal = _signal()
    twin = _signal(observed_at=signal.observed_at + 1)
    outcomes = decide_cycle(_snapshot((signal, twin)), _catalog(), NOW)
    assert _decisions(outcomes) == [
        FeedbackDecision.PROPOSED,
        FeedbackDecision.DEDUPLICATED,
    ]


def test_decisions_are_ordered_by_observation_not_by_ledger_order() -> None:
    """The oldest fact wins a contested budget slot, deterministically."""

    older = _signal(identity=("saga-1", "m-old", "PROCESS_FAILED"), observed_at=100.0)
    newer = _signal(identity=("saga-1", "m-new", "PROCESS_FAILED"), observed_at=200.0)
    outcomes = decide_cycle(_snapshot((newer, older)), _catalog(daily_cap=1), NOW)
    assert [outcome.signal.evidence.row_id for outcome in outcomes] == ["m-old", "m-new"]
    assert _decisions(outcomes) == [
        FeedbackDecision.PROPOSED,
        FeedbackDecision.SUPPRESSED_BUDGET,
    ]


def test_every_outcome_carries_a_reason() -> None:
    """A suppression with no reason is an operator asking why and getting silence."""

    signals = (_signal(), _signal(caused_by_feedback=True, identity=("s", "m2", "X")))
    outcomes = decide_cycle(_snapshot(signals), _catalog(), NOW)
    assert all(outcome.reason for outcome in outcomes)
