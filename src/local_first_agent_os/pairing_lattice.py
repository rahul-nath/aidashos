# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Rank model/effort pairs and check eligibility through an injected probe.

Scores and the diversity bonus are operator preferences, not calibrated review
accuracy. The probe's caller owns its evidence: WorkUnit selection uses recent
real dispatch outcomes without making a paid model call. Fixed selection
bypasses ranking, not eligibility, and supplies exactly one candidate.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

from .staffing import (
    AutoRanked,
    ExplicitPairs,
    FrontierPairing,
    Harness,
    PairingSelection,
    PreferredPair,
    RankedAny,
    SameProviderOnly,
    StrictPair,
)
from .vocabulary import DispatchTier


@dataclass(frozen=True)
class ScoredModel:
    """One model at one effort level with its operator-declared quality score.

    Harness controls launching; vendor tags control the diversity bonus.
    Neither tag establishes independent errors. Effort is passed verbatim to
    the harness; None means no effort override, not a measured quality level.
    """

    harness: Harness
    model: str
    quality: int
    vendor: str
    reasoning_effort: str | None = None
    anchor: bool = False
    """True when this row was read off a published chart rather than estimated."""

    @property
    def label(self) -> str:
        effort = f"@{self.reasoning_effort}" if self.reasoning_effort else ""
        return f"{self.harness.value}:{self.model}{effort}"

    @property
    def seat(self) -> tuple[Harness, str]:
        """The model identity shared by availability checks across effort levels."""

        return (self.harness, self.model)


@dataclass(frozen=True)
class QualityChart:
    """The declared models and the one knob that trades quality for diversity."""

    models: tuple[ScoredModel, ...]
    diversity_bonus: int
    source: Mapping[str, str]

    def quality_of(self, harness: Harness, model: str, effort: str | None = None) -> int | None:
        for item in self.models:
            if (
                item.harness is harness
                and item.model == model
                and (effort is None or item.reasoning_effort == effort)
            ):
                return item.quality
        return None


@dataclass(frozen=True)
class Pairing:
    """One candidate seating, with the arithmetic that ranked it.

    `score` is `q(senior) + q(staff) + diversity_bonus if cross_vendor`. It is
    carried rather than recomputed so an operator reading a staffing decision
    sees the number that produced it, and so a test can assert on the trade
    rather than on the ordering it happens to produce.
    """

    senior: ScoredModel
    staff: ScoredModel
    score: int
    cross_vendor: bool

    @property
    def label(self) -> str:
        return f"senior {self.senior.label} + staff {self.staff.label}"

    def models(self) -> tuple[ScoredModel, ScoredModel]:
        return (self.senior, self.staff)

    def slot_for(self, tier: DispatchTier) -> ScoredModel | None:
        match tier:
            case DispatchTier.SENIOR:
                return self.senior
            case DispatchTier.STAFF:
                return self.staff
            case DispatchTier.JUNIOR:
                return None


def load_quality_chart(path: Path) -> QualityChart:
    """Read the operator's declared scores, refusing a chart that cannot rank."""

    return parse_quality_chart(path.read_text(encoding="utf-8"), source=str(path))


def parse_quality_chart(text: str, *, source: str = "quality chart") -> QualityChart:
    """Parse the same snapshot whose hash identifies the selection evidence."""

    data = tomllib.loads(text)
    models: list[ScoredModel] = []
    for entry in data.get("models", []):
        harness = Harness(str(entry["harness"]))
        models.append(
            ScoredModel(
                harness=harness,
                model=str(entry["model"]),
                quality=int(entry["quality"]),
                vendor=str(entry.get("vendor", harness.value)),
                reasoning_effort=(
                    str(entry["effort"]) if entry.get("effort") is not None else None
                ),
                anchor=bool(entry.get("anchor", False)),
            )
        )
    if not models:
        raise ValueError(f"{source} declares no models, so no pairing can be ranked")
    seen = {(item.harness, item.model, item.reasoning_effort) for item in models}
    if len(seen) != len(models):
        raise ValueError(
            f"{source} scores the same model at the same effort twice, so its ranking is ambiguous"
        )
    scoring = data.get("scoring", {})
    bonus = int(scoring.get("diversity_bonus", 0))
    if bonus < 0:
        raise ValueError("diversity_bonus cannot be negative; 0 ranks on raw quality alone")
    return QualityChart(
        models=tuple(models),
        diversity_bonus=bonus,
        source={str(k): str(v) for k, v in data.get("source", {}).items()},
    )


def ordered_pairings(chart: QualityChart) -> tuple[Pairing, ...]:
    """Rank pairs whose reviewer scores at least as highly as the implementer.

    Scores and the diversity bonus are operator heuristics, not measured bug
    detection rates. Same-model pairs are valid; session isolation is enforced
    by the executor. Fixed selection bypasses these ranking preferences.
    Ties favor cross-vendor pairs, then label, for a total stable order.
    """

    candidates: list[Pairing] = []
    for senior in chart.models:
        for staff in chart.models:
            if staff.quality < senior.quality:
                continue
            candidates.append(_score_pair(chart, senior, staff))
    candidates.sort(key=lambda item: (-item.score, not item.cross_vendor, item.label))
    return tuple(candidates)


def _score_pair(chart: QualityChart, senior: ScoredModel, staff: ScoredModel) -> Pairing:
    cross_vendor = senior.vendor != staff.vendor
    return Pairing(
        senior=senior,
        staff=staff,
        score=senior.quality + staff.quality + (chart.diversity_bonus if cross_vendor else 0),
        cross_vendor=cross_vendor,
    )


def fixed_pair(chart: QualityChart, declaration: FrontierPairing) -> Pairing:
    """Resolve exact model/effort pins; a typo cannot fall through to another row."""

    def resolve(tier: DispatchTier) -> ScoredModel:
        slot = declaration.seats()[tier]
        for model in chart.models:
            if (model.harness, model.model, model.reasoning_effort) == (
                slot.harness,
                slot.model,
                slot.reasoning_effort,
            ):
                return model
        raise ValueError(
            f"fixed pairing {declaration.name!r} {tier.value} has no exact quality chart "
            f"entry for {slot.harness.value}:{slot.model}@{slot.reasoning_effort}"
        )

    return _score_pair(chart, resolve(DispatchTier.SENIOR), resolve(DispatchTier.STAFF))


def policy_candidates(
    chart: QualityChart, selection: PairingSelection, declarations: Mapping[str, FrontierPairing]
) -> tuple[Pairing, ...]:
    """Policy defines the search space; availability cannot expand it."""

    match selection:
        case AutoRanked():
            return ordered_pairings(chart)
        case StrictPair(pairing=name):
            return (fixed_pair(chart, declarations[name]),)
        case PreferredPair(pairing=name, fallback=fallback):
            preferred = fixed_pair(chart, declarations[name])
            match fallback:
                case ExplicitPairs(pairings=names):
                    alternatives = tuple(fixed_pair(chart, declarations[item]) for item in names)
                case RankedAny():
                    alternatives = ordered_pairings(chart)
                case SameProviderOnly():
                    alternatives = tuple(
                        pair
                        for pair in ordered_pairings(chart)
                        if all(
                            (actual.harness, actual.vendor) == (requested.harness, requested.vendor)
                            for actual, requested in zip(
                                pair.models(), preferred.models(), strict=True
                            )
                        )
                    )
                case _:
                    assert_never(fallback)
            return tuple(dict.fromkeys((preferred, *alternatives)))
        case _:
            assert_never(selection)


@dataclass(frozen=True)
class ProbeResult:
    """What one model answered, and when the answer stops being trusted."""

    alive: bool
    expires_at: float
    detail: str | None = None


ProbeFn = Callable[[Harness, str], tuple[bool, str | None]]
"""Ask one model whether it can accept work. Returns (alive, detail)."""


# How long a probe's answer is trusted. Minutes, not hours: the point of probing
# is that a provider's state changes on its own schedule and nobody here knows
# it. This is short enough that a window opening is noticed within one
# scheduling pass, and long enough that walking a lattice does not re-ask the
# same model once per pairing that contains it.
DEFAULT_PROBE_TTL_SECONDS: float = 300.0


class ProbeCache:
    """Per-model probe answers with a short TTL.

    Keyed on the model rather than the pairing on purpose. One model appears in
    many pairings, and asking it once per pairing would multiply the walk's cost
    by the lattice's width for no new information. This is not an exception to
    "always probe" - it is not asking the same question twice inside one pass.
    """

    def __init__(self, ttl_seconds: float = DEFAULT_PROBE_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._answers: dict[tuple[Harness, str], ProbeResult] = {}

    def get(self, harness: Harness, model: str, *, now: float) -> ProbeResult | None:
        answer = self._answers.get((harness, model))
        if answer is None or answer.expires_at <= now:
            return None
        return answer

    def put(
        self,
        harness: Harness,
        model: str,
        *,
        alive: bool,
        now: float,
        detail: str | None = None,
    ) -> ProbeResult:
        answer = ProbeResult(alive=alive, expires_at=now + self.ttl_seconds, detail=detail)
        self._answers[(harness, model)] = answer
        return answer

    def invalidate(self, harness: Harness, model: str) -> None:
        """Forget one model's answer, so the next walk asks it again.

        Called when a real dispatch fails on a model the cache called alive. A
        one-line nonce answering does not prove a long implementation turn will,
        so the cache holds a hypothesis that live work is entitled to refute.
        """

        self._answers.pop((harness, model), None)


@dataclass(frozen=True)
class PairingSelected:
    """The first pairing in quality order whose two models both answered."""

    pairing: Pairing
    probed: tuple[str, ...]
    """Model labels this walk actually asked, in order, for the operator's log."""


@dataclass(frozen=True)
class NoPairingAnswered:
    """Every legal pairing was walked and none had two live models.

    Distinct from "nothing is declared": the chart had candidates and each was
    asked. `refusals` carries what each dead model said, because the operator's
    next move differs between a spent window, a logged-out CLI, and a model id
    that no longer exists.
    """

    refusals: tuple[str, ...]
    probed: tuple[str, ...]


type PairingOutcome = PairingSelected | NoPairingAnswered


def select_live_pairing(
    chart: QualityChart,
    probe: ProbeFn,
    *,
    cache: ProbeCache,
    now: float,
    candidates: Sequence[Pairing] | None = None,
    on_rejection: Callable[[ScoredModel, ProbeResult], None] | None = None,
) -> PairingOutcome:
    """Walk the quality order and take the first pairing that answers.

    No cooldown is consulted and no spent-quota state is read. The walk stops at
    the first live pairing, so the common case - the best pairing is up - costs
    exactly two probes, and a fully spent machine costs one probe per declared
    model rather than one per pairing.
    """

    ordered = tuple(candidates) if candidates is not None else ordered_pairings(chart)
    probed: list[str] = []
    refusals: dict[str, str] = {}
    rejected: set[tuple[Harness, str]] = set()

    def alive(model: ScoredModel) -> bool:
        cached = cache.get(model.harness, model.model, now=now)
        if cached is None:
            answered, detail = probe(model.harness, model.model)
            cached = cache.put(
                model.harness,
                model.model,
                alive=answered,
                now=now,
                detail=detail,
            )
            probed.append(model.label)
        if not cached.alive:
            refusals.setdefault(model.label, cached.detail or "did not answer")
            if model.seat not in rejected:
                rejected.add(model.seat)
                if on_rejection is not None:
                    on_rejection(model, cached)
        return cached.alive

    for pairing in ordered:
        # Both asked through `alive`, and deliberately not short-circuited with
        # `and`: a dead staff model is worth recording even when the senior seat
        # already failed, because the refusal list is what the operator reads.
        senior_ok = alive(pairing.senior)
        staff_ok = alive(pairing.staff)
        if senior_ok and staff_ok:
            return PairingSelected(pairing=pairing, probed=tuple(probed))
    return NoPairingAnswered(
        refusals=tuple(f"{label}: {reason}" for label, reason in sorted(refusals.items())),
        probed=tuple(probed),
    )


def describe(outcome: PairingOutcome) -> str:
    """One line an operator can read in a log or a refusal payload."""

    match outcome:
        case PairingSelected(pairing=pairing, probed=probed):
            diversity = "cross-vendor" if pairing.cross_vendor else "same-vendor"
            return (
                f"staffed {pairing.label} (score {pairing.score}, {diversity}) "
                f"after probing {len(probed)} model(s)"
            )
        case NoPairingAnswered(refusals=refusals, probed=probed):
            return f"no declared pairing answered; probed {len(probed)} model(s): " + "; ".join(
                refusals
            )


def iter_model_labels(pairings: Iterable[Pairing]) -> tuple[str, ...]:
    """Every distinct model label across these pairings, in first-seen order."""

    seen: dict[str, None] = {}
    for pairing in pairings:
        for model in pairing.models():
            seen.setdefault(model.label, None)
    return tuple(seen)


__all__ = [
    "DEFAULT_PROBE_TTL_SECONDS",
    "NoPairingAnswered",
    "Pairing",
    "PairingOutcome",
    "PairingSelected",
    "ProbeCache",
    "ProbeFn",
    "ProbeResult",
    "QualityChart",
    "ScoredModel",
    "describe",
    "fixed_pair",
    "iter_model_labels",
    "load_quality_chart",
    "parse_quality_chart",
    "policy_candidates",
    "ordered_pairings",
    "select_live_pairing",
]
