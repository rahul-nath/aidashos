# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Every legal pairing, ordered by quality, walked by a live probe.

Three properties carry this module and each is pinned here: the ordering obeys
the staff-not-weaker rule, the diversity bonus is a term in the score rather
than a tiebreak, and the walk measures availability instead of remembering it.

The repository's own seating history is used as the ordering's acceptance test.
Every pairing this operator has ever declared must still be legal under the
scores in `configs/model_quality.toml`, so a re-derivation that contradicts the
history fails here rather than landing quietly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_first_agent_os.pairing_lattice import (
    NoPairingAnswered,
    PairingSelected,
    ProbeCache,
    QualityChart,
    ScoredModel,
    describe,
    load_quality_chart,
    ordered_pairings,
    select_live_pairing,
)
from local_first_agent_os.staffing import Harness

REPO_CHART = Path(__file__).resolve().parents[1] / "configs" / "model_quality.toml"

CLAUDE = Harness.CLAUDE
CODEX = Harness.CODEX
PI = Harness.PI


def _chart(*models: ScoredModel, diversity_bonus: int = 8) -> QualityChart:
    return QualityChart(models=models, diversity_bonus=diversity_bonus, source={})


def _model(harness: Harness, name: str, quality: int, *, vendor: str | None = None) -> ScoredModel:
    return ScoredModel(
        harness=harness,
        model=name,
        quality=quality,
        vendor=vendor if vendor is not None else harness.value,
    )


# --- the ordering ------------------------------------------------------------


def test_a_reviewer_is_never_weaker_than_the_implementer_it_reviews() -> None:
    """The 2026-08-09 assessment as a filter: the better critic reviews."""

    chart = _chart(
        _model(CLAUDE, "strong", 90),
        _model(CODEX, "weak", 70),
    )

    pairings = ordered_pairings(chart)

    assert all(item.staff.quality >= item.senior.quality for item in pairings)
    assert not any(
        item.senior.model == "strong" and item.staff.model == "weak" for item in pairings
    )


def test_a_pairing_never_names_one_model_for_both_seats() -> None:
    """A seat reviewing itself is not a review, whatever it scores."""

    chart = _chart(_model(CLAUDE, "only", 90))

    assert ordered_pairings(chart) == ()


def test_equal_quality_still_pairs() -> None:
    """Two different models rated the same still give a second opinion."""

    chart = _chart(_model(CLAUDE, "a", 80), _model(CODEX, "b", 80), diversity_bonus=0)

    pairings = ordered_pairings(chart)

    assert {(item.senior.model, item.staff.model) for item in pairings} == {("a", "b"), ("b", "a")}


def test_the_diversity_bonus_is_a_term_in_the_score_not_a_tiebreak() -> None:
    """The trade the operator asked to be visible.

    A same-vendor pair with better raw quality must be able to lose to a
    cross-vendor pair, and must win again when the bonus is removed. That is the
    whole reason the bonus is a number in the file rather than a sort key.
    """

    models = (
        _model(CLAUDE, "best", 92),
        _model(CLAUDE, "good", 88),
        _model(CODEX, "other", 85),
    )
    # The best same-vendor pair is good(88) + best(92) = 180. The best
    # cross-vendor pair is other(85) + best(92) = 177 on raw quality, which
    # loses - until the bonus puts it at 185 and it wins.
    with_bonus = ordered_pairings(_chart(*models, diversity_bonus=8))
    without_bonus = ordered_pairings(_chart(*models, diversity_bonus=0))

    assert with_bonus[0].cross_vendor is True
    assert with_bonus[0].score == 185
    assert without_bonus[0].cross_vendor is False
    assert without_bonus[0].score == 180


def test_the_order_is_total_and_stable() -> None:
    """Identical inputs must ask identical questions in identical order."""

    chart = load_quality_chart(REPO_CHART)

    first = ordered_pairings(chart)
    second = ordered_pairings(chart)

    assert first == second
    scores = [item.score for item in first]
    assert scores == sorted(scores, reverse=True)


# --- the repository's own history as the chart's acceptance test --------------


@pytest.mark.parametrize(
    "senior,staff,seating",
    [
        ("gpt-5.6-sol", "claude-opus-5", "cross-vendor, 2026-08-23"),
        ("claude-opus-5", "claude-fable-5", "claude-only, 2026-08-18"),
        ("claude-sonnet-5", "claude-opus-5", "claude-only, 2026-08-30"),
        ("gpt-5.6-terra", "gpt-5.6-sol", "codex-only, 2026-08-23"),
    ],
)
def test_every_seating_this_operator_declared_is_legal_under_the_chart(
    senior: str, staff: str, seating: str
) -> None:
    """A re-derivation that contradicts the seating history fails here.

    The scores were seeded from these four rulings, so this is the check that
    keeps a future edit honest: change a number in a way that would have made one
    of the operator's own seatings illegal, and this says which one.
    """

    chart = load_quality_chart(REPO_CHART)
    legal = {(item.senior.model, item.staff.model) for item in ordered_pairings(chart)}

    assert (senior, staff) in legal, f"the {seating} seating is no longer legal under the chart"


def test_the_repo_chart_puts_the_best_critic_in_the_critic_seat() -> None:
    """Top of the lattice is cross-vendor with the strongest measured reviewer.

    Opus at xhigh and at max both score 63 against Fable's 62, so the critic
    seat is Opus and not Fable. The staffing file assumed the opposite for a
    fortnight, on the reasoning that Fable is the tier above Opus; the
    leaderboard says running Opus hard beats it.
    """

    best = ordered_pairings(load_quality_chart(REPO_CHART))[0]

    assert best.senior.model == "gpt-5.6-sol"
    assert best.staff.model == "claude-opus-5"
    assert best.cross_vendor is True


def test_a_chart_that_cannot_rank_is_refused(tmp_path: Path) -> None:
    empty = tmp_path / "empty.toml"
    empty.write_text("[scoring]\ndiversity_bonus = 1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="declares no models"):
        load_quality_chart(empty)

    duplicate = tmp_path / "duplicate.toml"
    duplicate.write_text(
        '[[models]]\nharness = "claude"\nmodel = "x"\nquality = 1\n'
        '[[models]]\nharness = "claude"\nmodel = "x"\nquality = 2\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="same effort twice"):
        load_quality_chart(duplicate)


# --- the walk ----------------------------------------------------------------


def _recording_probe(dead: set[str]):
    asked: list[str] = []

    def probe(harness: Harness, model: str) -> tuple[bool, str | None]:
        asked.append(f"{harness.value}:{model}")
        if model in dead:
            return False, "spent window"
        return True, None

    return probe, asked


def test_the_walk_takes_the_best_pairing_that_answers() -> None:
    chart = load_quality_chart(REPO_CHART)
    probe, asked = _recording_probe(set())

    outcome = select_live_pairing(chart, probe, cache=ProbeCache(), now=0.0)

    assert isinstance(outcome, PairingSelected)
    assert outcome.pairing.senior.model == "gpt-5.6-sol"
    assert outcome.pairing.staff.model == "claude-opus-5"
    # Exactly two probes for the happy path: the walk stops at the first pairing.
    assert asked == ["codex:gpt-5.6-sol", "claude:claude-opus-5"]


def test_the_walk_falls_past_a_dead_model_to_the_next_legal_pairing() -> None:
    """The Fable case: one model out must not eliminate its whole vendor."""

    chart = load_quality_chart(REPO_CHART)
    probe, _asked = _recording_probe({"gpt-5.6-sol"})

    outcome = select_live_pairing(chart, probe, cache=ProbeCache(), now=0.0)

    assert isinstance(outcome, PairingSelected)
    assert "gpt-5.6-sol" not in (outcome.pairing.senior.model, outcome.pairing.staff.model)
    assert outcome.pairing.staff.quality >= outcome.pairing.senior.quality


def test_one_dead_model_does_not_bench_its_vendor() -> None:
    """Fable out of credits must still leave Opus and Sonnet pairable.

    This is the case that stalled work unit c88ff4167c66 for hours under the
    old provider-wide cooldown.
    """

    chart = load_quality_chart(REPO_CHART)
    probe, _asked = _recording_probe({"claude-opus-5"})

    outcome = select_live_pairing(chart, probe, cache=ProbeCache(), now=0.0)

    assert isinstance(outcome, PairingSelected)
    assert "claude-opus-5" not in (outcome.pairing.senior.model, outcome.pairing.staff.model)
    # Falls to Fable, the next measured critic down, rather than abandoning the
    # vendor: one dead model is not a dead provider.
    assert outcome.pairing.staff.model == "claude-fable-5"


def test_a_model_is_asked_once_per_walk_however_many_pairings_hold_it() -> None:
    """Not an exception to always-probe: it is not asking twice in one pass."""

    chart = load_quality_chart(REPO_CHART)
    probe, asked = _recording_probe({"gpt-5.6-sol", "claude-opus-5", "claude-fable-5"})

    select_live_pairing(chart, probe, cache=ProbeCache(), now=0.0)

    assert len(asked) == len(set(asked)), f"a model was asked more than once: {asked}"


def test_a_fully_spent_machine_reports_what_each_model_said() -> None:
    chart = load_quality_chart(REPO_CHART)
    dead = {item.model for item in chart.models}
    probe, asked = _recording_probe(dead)

    outcome = select_live_pairing(chart, probe, cache=ProbeCache(), now=0.0)

    assert isinstance(outcome, NoPairingAnswered)
    # Distinct models, not chart rows. A model appears once per effort level it
    # is scored at, and effort does not change whether the provider will answer,
    # so the walk asks each model once however many rows hold it.
    assert len(asked) == len({item.seat for item in chart.models}), (
        "every declared model is asked exactly once"
    )
    assert all("spent window" in item for item in outcome.refusals)
    assert "no declared pairing answered" in describe(outcome)


# --- the cache ---------------------------------------------------------------


def test_the_cached_answer_expires_so_a_window_reopening_is_noticed() -> None:
    """Minutes, not hours. The whole point is that provider state moves."""

    chart = _chart(_model(CLAUDE, "a", 90), _model(CODEX, "b", 90))
    cache = ProbeCache(ttl_seconds=300.0)
    calls: list[float] = []

    def probe(_harness: Harness, _model: str) -> tuple[bool, str | None]:
        calls.append(1.0)
        return True, None

    select_live_pairing(chart, probe, cache=cache, now=0.0)
    select_live_pairing(chart, probe, cache=cache, now=120.0)
    assert len(calls) == 2, "inside the TTL the walk reuses the answer"

    select_live_pairing(chart, probe, cache=cache, now=301.0)
    assert len(calls) == 4, "past the TTL the walk asks again"


def test_a_real_failure_can_refute_the_probe_that_called_a_model_alive() -> None:
    """A one-line nonce answering does not prove a long turn will."""

    cache = ProbeCache()
    cache.put(CLAUDE, "claude-opus-5", alive=True, now=0.0)
    assert cache.get(CLAUDE, "claude-opus-5", now=1.0) is not None

    cache.invalidate(CLAUDE, "claude-opus-5")

    assert cache.get(CLAUDE, "claude-opus-5", now=1.0) is None


def test_the_selection_line_names_the_score_and_the_trade() -> None:
    chart = load_quality_chart(REPO_CHART)
    probe, _asked = _recording_probe(set())

    line = describe(select_live_pairing(chart, probe, cache=ProbeCache(), now=0.0))

    assert "cross-vendor" in line
    assert "score" in line
