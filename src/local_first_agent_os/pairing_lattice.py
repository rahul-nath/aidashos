# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Every legal frontier pairing, ordered by quality, walked by a live probe.

This replaces "which pairing did someone write down, and is its vendor inside a
five-hour timer" with "which of every pairing the declared models admit is
actually answering right now". Two independent changes, and both were forced by
the same day's evidence:

The ordering is derived rather than declared. `configs/staffing.toml` carried a
hand-maintained `fallback = [...]` chain that could only name pairings a person
had thought of, went stale twice in August 2026, and grew one entry per vendor
combination. Here every (senior, staff) pair the quality chart admits is
generated, filtered by the rule that a reviewer may not be weaker than the
implementer it reviews, and scored.

Availability is measured rather than remembered. The flat usage-limit cooldown
is a guess about a provider's internal state; on 2026-08-30 it benched both
vendors for hours while a one-line nonce to each answered `ok`. So there is no
belief to keep here: the walk asks, and the first pairing whose two models both
answer is the one that runs. A short TTL is the only memory, and it exists to
stop re-asking within one scheduling pass rather than to model a quota.

Deliberately no per-model availability bookkeeping. Tracking which model is
spent would be a second source of truth beside the probe, and the operator's
ruling on 2026-08-30 was that exceptions to "just ask" are how a simple rule
becomes a pile of minutiae. The probe is the availability check.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .staffing import Harness
from .vocabulary import DispatchTier


@dataclass(frozen=True)
class ScoredModel:
    """One model AT ONE EFFORT LEVEL, and how good the operator rates it.

    Keyed on the pair because effort moves quality as much as the model choice
    does. `claude-opus-5` at low effort and `qwen3.8-27b-mtp` at xhigh both
    score 52 on the leaderboard the operator read on 2026-08-30, and
    `gpt-5.6-sol` at low scores 51 - so a local 27B running hard outranks two
    frontier models running lazily. A chart keyed on the model alone cannot say
    that, and staffing that cannot say it will pay frontier prices for a lazy
    turn.

    `harness` is how this model is spawned; `vendor` is whose training run it
    came from. Diversity keys on the vendor, because independent error modes are
    a property of the training run rather than of the process that launches it -
    Gemma and Qwen are both `pi` here and are Google and Alibaba models, so an
    all-local pairing of the two is genuinely cross-vendor.

    `effort` is passed to the harness verbatim (`--effort` for claude,
    `-c model_reasoning_effort=` for codex). ``None`` is a local model, which by
    the operator's ruling always runs at its top setting: there is no per-token
    bill to economise against.
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
        """What makes two rows the same model, ignoring effort.

        A pairing may not seat one model twice even at two different efforts:
        the author would be re-reading its own work with a bigger thinking
        budget, which is not the independent second opinion a review is.
        """

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

    data = tomllib.loads(path.read_text(encoding="utf-8"))
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
        raise ValueError(f"{path} declares no models, so no pairing can be ranked")
    seen = {(item.harness, item.model, item.reasoning_effort) for item in models}
    if len(seen) != len(models):
        raise ValueError(
            f"{path} scores the same model at the same effort twice, so its ranking is ambiguous"
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
    """Every legal pairing, best first.

    Legal means two distinct models whose staff seat is not weaker than its
    senior seat. Both halves are load-bearing. A pairing naming one model twice
    is not a review at all - the author re-reads itself - which
    `FrontierPairing` already refuses at load. A staff seat weaker than its
    senior is the failure the 2026-08-09 assessment named: the better critic
    reviews, or the review is theatre.

    Equal quality is permitted. Two different models rated the same still give
    the independent second opinion the review exists for.

    Ties in score are broken by the cross-vendor pairing first and then by label,
    so the order is total and stable across runs. An unstable order would make
    the probe walk ask different models on identical inputs, which is the kind of
    nondeterminism this system exists to remove.
    """

    candidates: list[Pairing] = []
    for senior in chart.models:
        for staff in chart.models:
            # Same model at two efforts is still one model, and one model cannot
            # review itself however hard the second pass thinks.
            if senior.seat == staff.seat:
                continue
            if staff.quality < senior.quality:
                continue
            cross_vendor = senior.vendor != staff.vendor
            score = senior.quality + staff.quality + (chart.diversity_bonus if cross_vendor else 0)
            candidates.append(
                Pairing(senior=senior, staff=staff, score=score, cross_vendor=cross_vendor)
            )
    candidates.sort(key=lambda item: (-item.score, not item.cross_vendor, item.label))
    return tuple(candidates)


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
    "iter_model_labels",
    "load_quality_chart",
    "ordered_pairings",
    "select_live_pairing",
]
