# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Staffing model: tiers, the harness bench, and per-stage rosters.

This is the role model that replaces the ad-hoc ``role`` string + the
``"implement" in role.lower()`` substring dispatch. Two ideas compose here:

* Ouroboros' *judgment-vs-deterministic* split — a stage role is EITHER a
  ``JudgmentRole`` (needs a thinking model) OR a ``CheckRole`` (a deterministic
  shell check). Modeled as a sum type so illegal states (asking a check for a
  tier, or a judge for a shell command) are unrepresentable.
* The contractor-tier idea — a ``Tier`` (junior/senior/staff) resolves through
  the ``Bench`` to a concrete harness + model + capacity, which is the
  executable binding Ouroboros never had.

The single source of truth for "which model plays which tier" is the ``Bench``;
nothing else should hardcode a harness. See
``docs/completed/role_model_and_staffing_design.md``.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, assert_never

logger = logging.getLogger(__name__)


class Tier(StrEnum):
    """Seniority axis — an engineer persona's level IS its tier."""

    JUNIOR = "junior"  # local, cheap, high-count
    SENIOR = "senior"  # strong implementer
    STAFF = "staff"  # strongest; reviewer / finisher


class Harness(StrEnum):
    """Concrete agent harnesses a tier can resolve to."""

    CLAUDE = "claude"
    CODEX = "codex"
    PI = "pi"


class FrontierHarness(StrEnum):
    """The harnesses that are an external CLI process with a command line.

    ``Harness`` is the whole vocabulary a bench slot may name. This is the part
    of it that can be spawned, and it is a separate type rather than a runtime
    check because that is what makes the mistake unwritable: a command builder
    typed on ``FrontierHarness`` cannot be handed ``PI`` at all.

    It was a runtime check that did not exist. The command builder branched on
    ``codex`` and treated everything else as claude, so a junior slot naming
    ``pi`` and ``gemma4`` produced ``claude --model gemma4`` and a ``401 Not
    logged in`` from an account that has no such model.
    """

    CLAUDE = "claude"
    CODEX = "codex"


class JudgmentWorkload(StrEnum):
    """Why a judgment model is being launched, independent of seniority."""

    STANDARD = "standard"
    INDEPENDENT_READING = "independent_reading"


@dataclass(frozen=True)
class LocalHarness:
    """A harness that answers in-process from a served local model.

    It carries the ``Harness`` it came from so a caller refusing it can name the
    thing it refused, and so a second local harness does not turn this back into
    a boolean.
    """

    harness: Harness

    def describe(self) -> str:
        return (
            f"harness {self.harness.value!r} answers from the local model through "
            "the delegate callback and has no CLI to spawn"
        )


# Which of the two a bench harness is, as a sum rather than a boolean, because
# the local case carries the harness name and the frontier case carries a
# command builder's argument.
type HarnessKind = FrontierHarness | LocalHarness


def classify_harness(harness: Harness) -> HarnessKind:
    """Split a bench harness into "spawn a CLI" and "call the local delegate".

    Total by construction. ``assert_never`` makes a fourth ``Harness`` member a
    type error here rather than a silent fall-through to claude, which is the
    exact shape of the defect this replaced.
    """

    match harness:
        case Harness.CLAUDE:
            return FrontierHarness.CLAUDE
        case Harness.CODEX:
            return FrontierHarness.CODEX
        case Harness.PI:
            return LocalHarness(harness)
    assert_never(harness)


@dataclass(frozen=True)
class WorkloadModelProfile:
    """A bounded model override for one workload within a tier's seat."""

    workload: JudgmentWorkload
    model: str
    reasoning_effort: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
        }


@dataclass(frozen=True)
class BackupModel:
    """One alternate a seat may move to: a harness and a model, together.

    The harness is required, and that requirement is the entire type. A backup
    used to be a bare model id whose harness was whatever the replacement
    happened to land on, which made two different things unsayable and one
    thing sayable that is never true.

    Unsayable: "this seat's way out is the other vendor". A bare id can only
    ride along on a harness some other tier is already holding, so a bench with
    one vendor in both frontier seats had no way to declare an escape from that
    vendor at all - `configs/staffing.toml` claimed to declare one and could
    not, because nothing in `gpt-5.6-sol` says codex.

    Never true: the id lands on whichever CLI the peer search returned, so a
    codex id could be handed to `claude --model`. Nothing validates model ids,
    so that surfaces as a spawn failure attributed to the wrong thing.

    `reasoning_effort` is here rather than inherited for the same reason. The
    effort vocabulary belongs to a provider, not to a seat: `max` is a claude
    word. A backup that changes harness must state its own or take that CLI's
    default, which is what None means.

    `model` is optional for the same reason `BenchSlot.model` is: a seat may run
    whatever the subscription's CLI defaults to. A hand-written entry must still
    state it - `_read_backup_model` demands the key - because a bare
    `{ harness = "codex" }` reads as a typo.
    """

    harness: Harness
    model: str | None = None
    reasoning_effort: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "harness": self.harness.value,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
        }


@dataclass(frozen=True)
class BenchSlot:
    """Who is available at one tier: a harness, an optional model, a capacity."""

    harness: Harness
    model: str | None = None  # None => harness/CLI default (e.g. subscription)
    capacity: int = 1  # max concurrent instances of this tier
    backup_models: tuple[BackupModel, ...] = ()  # where this seat goes when its harness cannot
    reasoning_effort: str | None = None  # harness-specific effort knob (e.g. codex high)
    workload_profiles: tuple[WorkloadModelProfile, ...] = ()

    def __post_init__(self) -> None:
        workloads = [profile.workload for profile in self.workload_profiles]
        if len(workloads) != len(set(workloads)):
            raise ValueError("a bench slot cannot define the same workload profile twice")
        if JudgmentWorkload.STANDARD in workloads:
            raise ValueError("the standard workload is the bench slot itself, not an override")

    def to_payload(self) -> dict[str, object]:
        return {
            "harness": self.harness.value,
            "model": self.model,
            "capacity": self.capacity,
            "reasoning_effort": self.reasoning_effort,
            "backup_models": [backup.to_payload() for backup in self.backup_models],
            "workload_profiles": {
                profile.workload.value: profile.to_payload() for profile in self.workload_profiles
            },
        }


# The Bench is the ONE place that knows tier -> runtime.
Bench = dict[Tier, BenchSlot]


class SharedSeatRefused(ValueError):
    """A pairing named one model for both frontier seats.

    Unconditional. There used to be a `same_model_review_accepted` escape flag,
    justified by a machine with only one model installed - and its first use in
    anger was an agent reaching for it in a bench that had a second model
    sitting in `configs/model_registry.toml` the whole time, which is exactly
    how an acknowledgement decays into a loophole. The operator's ruling
    (2026-08-23) is that the one-model machine is not a configuration this
    system serves: claude and codex each carry multiple model tiers, and the
    local registry carries three, so a second model always exists. A shared
    seat is therefore unrepresentable, not acknowledgeable.
    """


@dataclass(frozen=True)
class FrontierPairing:
    """The two frontier seats as one declaration: who implements, who reviews.

    Senior and staff were two independent tables, and picking each was two
    decisions. It is one. "Who reviews Opus" has an answer that depends on what
    is implementing, and splitting the declaration meant an operator could
    change half of it - which is not a hypothetical here. Every swap of this
    seating has cost something: the 2026-08-09 swap left four prose claims
    behind, and `POLICIES.md` twice denied the implementer a write its own plan
    had granted, because two files disagreed about who was seated.

    So a pairing is chosen by name and both seats arrive together. Editing the
    implementer without answering "then who reviews it" is no longer a thing the
    file can express, and the property that review is worth running - that the
    reviewer can disagree for reasons the implementer did not already have - is
    enforced once, here, at the moment the seating is declared. One model in
    both seats raises `SharedSeatRefused`, with no override; see that type for
    why the override it briefly had is gone.

    The predicate is `(harness, model)`. Two models from one vendor pass: that
    is the sanctioned outage fallback. One seat pinning a model while the other
    takes the CLI default also passes, because whether they coincide depends on
    a default this cannot see. Two unpinned seats on one harness do not: the
    same CLI default is the same model.

    `fallback` names other pairings, in order. It is how the escape hatch stays
    a pair: restaffing moves BOTH seats to the first fallback pairing that
    avoids every unavailable harness, so the moved bench is itself a declared,
    checked pairing rather than whatever two per-seat escapes happened to
    compose. A per-seat escape was the previous shape and it was half a way
    out - implementation could move to a live vendor while review kept
    dispatching into the spent one, and the seat that did move landed beside a
    reviewer nobody had checked it against.
    """

    name: str
    senior: BenchSlot
    staff: BenchSlot
    fallback: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.senior.harness is not self.staff.harness or self.senior.model != self.staff.model:
            return
        named = self.senior.model or f"the {self.senior.harness.value} CLI default"
        raise SharedSeatRefused(
            f"pairing {self.name!r} seats {named} as both implementer and reviewer, so "
            "review would be the model that wrote the change re-reading itself. Name a "
            "different model for one seat; every harness this system staffs offers "
            "more than one"
        )

    def seats(self) -> dict[Tier, BenchSlot]:
        """The two slots, keyed by the tier each one holds."""

        return {Tier.SENIOR: self.senior, Tier.STAFF: self.staff}

    def frontier_harnesses(self) -> frozenset[FrontierHarness]:
        """The spawnable vendors this pairing depends on.

        A local seat contributes nothing: it has no quota to spend and no login
        to lose, so it can never be the reason a pairing is not viable.
        """

        found: set[FrontierHarness] = set()
        for slot in (self.senior, self.staff):
            kind = classify_harness(slot.harness)
            if not isinstance(kind, LocalHarness):
                found.add(kind)
        return frozenset(found)


@dataclass(frozen=True)
class Staffing:
    """Everything `configs/staffing.toml` declares, resolved and checked.

    `Bench` remains the runtime currency - a spawner needs tier -> slot and
    nothing else - but a bench cannot answer "where does this seating go when a
    vendor dies", because that answer is pairing-shaped: the two frontier seats
    move together or the invariant they exist for does not survive the move.
    This is the type that holds both: the declared pairings, which one is
    seated, and the tiers that are seated alone.

    `bench` is derived, never stored beside, so the two cannot disagree.
    """

    pairings: dict[str, FrontierPairing]
    seated: FrontierPairing
    solo: Bench

    def __post_init__(self) -> None:
        if self.pairings.get(self.seated.name) is not self.seated:
            raise ValueError(f"seated pairing {self.seated.name!r} is not among the declared ones")
        for pairing in self.pairings.values():
            for target in pairing.fallback:
                if target == pairing.name:
                    raise ValueError(
                        f"pairing {pairing.name!r} falls back to itself, which is no escape"
                    )
                if target not in self.pairings:
                    raise ValueError(
                        f"pairing {pairing.name!r} falls back to {target!r}, which is not "
                        f"declared; the file has {sorted(self.pairings)}"
                    )

    @property
    def bench(self) -> Bench:
        """The tier -> slot mapping the seated pairing and solo tiers compose."""

        return {**self.solo, **self.seated.seats()}

    def __getitem__(self, tier: Tier) -> BenchSlot:
        """Resolve a tier exactly as the derived bench would.

        Kept deliberately read-only: a caller that could assign through this
        would be editing a seat out of its pairing, which is the half-edit the
        type exists to refuse.
        """

        return self.bench[tier]

    def escape_pairings(self) -> tuple[FrontierPairing, ...]:
        """The seated pairing's fallback chain, resolved, in declared order.

        One hop: a fallback's own `fallback` is not followed, so where a seating
        can end up is exactly the list its operator wrote on it. Both restaffing
        paths walk this same tuple - the ledger-evidence path and the
        probe-evidence path differ only in which candidates they accept, never
        in which candidates exist.
        """

        return tuple(self.pairings[name] for name in self.seated.fallback)

    def pairing_avoiding(self, unavailable: frozenset[FrontierHarness]) -> FrontierPairing | None:
        """The pairing to run on, given which vendors cannot act right now.

        The seated pairing wins whenever it is untouched, because the bench is
        an operator decision and an available seat is never moved. Otherwise the
        `escape_pairings` chain is walked in declared order and the first
        pairing that avoids every unavailable harness takes both seats.

        None means nothing declared avoids the outage, which a caller reports as
        the pair being unstaffable - both seats together, since staffing half a
        pair is the failure mode this type exists to remove.
        """

        if not self.seated.frontier_harnesses() & unavailable:
            return self.seated
        for candidate in self.escape_pairings():
            if not candidate.frontier_harnesses() & unavailable:
                return candidate
        return None

    def frontier_harnesses_in_play(self) -> frozenset[FrontierHarness]:
        """Every vendor a run under this staffing could be asked to spawn.

        The seated pairing's vendors, every escape pairing's vendors, and any
        solo frontier tier. This is what a readiness door probes: probing only
        the seated vendors would leave every escape `HarnessUnknown`, and an
        unknown escape is one the door correctly refuses to move onto - so the
        door would report an outage unrecoverable while a proven way out sat
        one probe away.
        """

        found: set[FrontierHarness] = set(self.seated.frontier_harnesses())
        for pairing in self.escape_pairings():
            found |= pairing.frontier_harnesses()
        for slot in self.solo.values():
            kind = classify_harness(slot.harness)
            if not isinstance(kind, LocalHarness):
                found.add(kind)
        return frozenset(found)


@dataclass(frozen=True)
class JudgmentRole:
    """A role that needs a thinking model (Ouroboros Character, unified w/ tier)."""

    name: str  # "implementer", "reviewer", "realist", "qa"
    tier: Tier
    stance: str | None = None  # optional Ouroboros cognitive stance (deferred layer)

    kind: str = field(default="judgment", init=False)

    def to_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "tier": self.tier.value,
            "stance": self.stance,
        }


@dataclass(frozen=True)
class CheckRole:
    """A deterministic tool-with-a-hat (Ouroboros FunctionalRole): the check IS a command."""

    name: str  # "test_runner", "linter", "typecheck"
    command: str

    kind: str = field(default="check", init=False)

    def to_payload(self) -> dict[str, object]:
        return {"kind": self.kind, "name": self.name, "command": self.command}


# A stage role is one or the other — a sum type.
StageRole = JudgmentRole | CheckRole


@dataclass(frozen=True)
class Roster:
    """The crew that staffs one stage."""

    judgment: tuple[JudgmentRole, ...] = ()
    checks: tuple[CheckRole, ...] = ()
    consensus: tuple[JudgmentRole, ...] = ()  # optional majority-vote panel

    def to_payload(self) -> dict[str, object]:
        return {
            "judgment": [role.to_payload() for role in self.judgment],
            "checks": [role.to_payload() for role in self.checks],
            "consensus": [role.to_payload() for role in self.consensus],
        }


# --- Defaults (used when no configs/staffing.toml is present) ------------------
# Reached only when no configs/staffing.toml exists, which in practice means a
# fresh clone and most tests. It is allowed to differ from the repo config and
# currently does: the config seats one vendor in both frontier seats while its
# quota outage lasts, and this keeps two. Nothing has to reconcile them, because
# `capability_gate.policy_principal` resolves a caller to its *seat* through
# whichever bench is in use, so the gate and the spawner agree either way. Before
# that, `POLICIES.md` was keyed on the vendor and the two benches disagreeing
# meant the implementer was denied the write its own plan had granted.
#
# What holds here is the seating this file would choose left alone: two vendors,
# so review is not the author re-reading itself. It holds by construction rather
# than by assertion now, because the two frontier seats are declared as a
# `FrontierPairing` - the same type `load_bench` resolves the config through, so
# the fallback bench and a configured one are checked by one rule and not two.
# No test compares this pairing to the config, deliberately: an equality would
# forbid the outage seating the config is entitled to declare.
DEFAULT_PAIRING = FrontierPairing(
    name="default",
    senior=BenchSlot(
        harness=Harness.CLAUDE,
        model=None,
        capacity=3,
        reasoning_effort="max",
    ),
    staff=BenchSlot(harness=Harness.CODEX, model=None, capacity=1),
)

DEFAULT_JUNIOR = BenchSlot(
    harness=Harness.PI,
    model="gemma4",
    capacity=4,
    backup_models=(BackupModel(harness=Harness.PI, model="qwen3.8-27b-mtp"),),
)

DEFAULT_STAFFING = Staffing(
    pairings={DEFAULT_PAIRING.name: DEFAULT_PAIRING},
    seated=DEFAULT_PAIRING,
    solo={Tier.JUNIOR: DEFAULT_JUNIOR},
)

DEFAULT_BENCH: Bench = DEFAULT_STAFFING.bench

# Stage rosters: the implementer is senior; the reviewer is staff and checks the
# implementer's work - two models checking each other. Which vendor plays either
# tier is `configs/staffing.toml`'s business and has changed once; naming one
# here is how a comment goes stale without anything failing.
IMPLEMENTER = JudgmentRole(name="implementer", tier=Tier.SENIOR)
REVIEWER = JudgmentRole(name="reviewer", tier=Tier.STAFF, stance="evaluator")

DEFAULT_ROSTERS: dict[str, Roster] = {
    "IMPLEMENTATION": Roster(judgment=(IMPLEMENTER,)),
    "REVIEW": Roster(
        judgment=(REVIEWER,),
        consensus=(
            REVIEWER,
            JudgmentRole(name="qa", tier=Tier.SENIOR),
            JudgmentRole(name="realist", tier=Tier.SENIOR),
        ),
    ),
}


def resolve_bench(tier: Tier, bench: Bench | None = None) -> BenchSlot:
    """Resolve a tier to its concrete harness slot, raising on an unstaffed tier."""
    bench = bench if bench is not None else DEFAULT_BENCH
    try:
        return bench[tier]
    except KeyError as exc:
        raise KeyError(f"no bench slot configured for tier {tier!r}") from exc


def resolve_bench_for_workload(
    tier: Tier,
    workload: JudgmentWorkload,
    bench: Bench | None = None,
) -> BenchSlot:
    """Resolve a tier, then apply the exact model profile for its workload."""

    slot = resolve_bench(tier, bench)
    if workload is JudgmentWorkload.STANDARD:
        return slot
    profile = next(
        (profile for profile in slot.workload_profiles if profile.workload is workload),
        None,
    )
    if profile is None:
        return slot
    return replace(
        slot,
        model=profile.model,
        reasoning_effort=profile.reasoning_effort,
    )


@dataclass(frozen=True)
class SpawnableModel:
    """One model id a dispatch could actually hand to a CLI, and why.

    A tier is not one model. `resolve_bench_for_workload` swaps in a workload
    profile's id, so a seat with an `independent_reading` profile can spawn two
    different models depending on the task, and a probe that reads `slot.model`
    proves exactly one of them.
    """

    tier: Tier
    workload: JudgmentWorkload
    harness: Harness
    model: str | None
    reasoning_effort: str | None

    @property
    def label(self) -> str:
        """Names the workload only when it is not the seat's ordinary one."""

        seat = self.tier.value
        if self.workload is not JudgmentWorkload.STANDARD:
            seat = f"{seat}[{self.workload.value}]"
        return f"{seat} ({self.harness.value} {self.model or 'CLI default'})"


def spawnable_models(bench: Bench | None = None) -> tuple[SpawnableModel, ...]:
    """Every distinct model a dispatch could launch, across tiers and workloads.

    Both readiness probes ask this rather than reading the bench themselves. They
    used to iterate `bench.items()` and probe `slot.model`, which meant workload
    profiles were never proved: `gpt-5.6-terra` sat in `configs/staffing.toml`
    from 2026-08-16 as the senior reader's model, and on 2026-08-17 nothing had
    ever asked whether that id exists. Nothing validates ids at load either, so
    the first thing to find out would have been a dispatch, mid-run, on the
    operator's quota. That is the exact failure `frontier_probe.py` was written
    to prevent, one level down from where it was looking.

    Resolved *through* `resolve_bench_for_workload` on purpose, rather than by
    reading `workload_profiles` directly. That function is what dispatch calls,
    so enumerating any other way would be a second opinion about which model runs
    - and a probe that proves a model dispatch would not have used is worth
    nothing. A new `JudgmentWorkload` member is covered here the day it is added.

    Duplicates are dropped by (harness, model): a profile that only changes
    reasoning effort names the same id, and proving it twice buys nothing while
    costing a completion.

    `backup_models` is deliberately not enumerated, and that is a known gap
    rather than a claim of totality. A backup is spawnable - since
    `_replacement_for` reads one before it looks for a peer, an outage bench
    dispatches straight onto it - so by this function's own standard it should be
    proved. What stops it is that a backup names the vendor a seat runs to when
    its own is spent, which is usually the vendor that is itself out of quota:
    probing it at every runtime start would spend a completion to produce a
    startup alarm about a provider nobody is asking to run yet. Making that
    tradeoff is a separate decision from being able to reach the backup at all.
    """

    bench = bench if bench is not None else DEFAULT_BENCH
    found: list[SpawnableModel] = []
    seen: set[tuple[Harness, str | None]] = set()
    for tier in sorted(bench, key=lambda item: item.value):
        for workload in JudgmentWorkload:
            slot = resolve_bench_for_workload(tier, workload, bench)
            key = (slot.harness, slot.model)
            if key in seen:
                continue
            seen.add(key)
            found.append(
                SpawnableModel(
                    tier=tier,
                    workload=workload,
                    harness=slot.harness,
                    model=slot.model,
                    reasoning_effort=slot.reasoning_effort,
                )
            )
    return tuple(found)


def dispatch_seat_counts(bench: Bench | None = None) -> dict[str, int]:
    """Per-tier concurrent dispatch seats: a tier's seat count IS its capacity.

    The dispatcher claims one intent per free seat and runs each claimed
    pipeline concurrently, so "how many senior pipelines at once" is a tier
    semantic and lives here, not in the loop. The keys are tier values (plain
    strings) because the dispatcher speaks the coordination ledger's tier
    vocabulary, not this module's ``Tier`` - the two enums share values by
    construction.

    A tier absent from the bench gets no key and therefore no seats: a
    dispatcher cannot staff an intent for an unstaffed tier, and claiming it
    just to fail it would take the intent away from a dispatcher that could.
    """

    resolved = bench if bench is not None else DEFAULT_BENCH
    return {tier.value: slot.capacity for tier, slot in resolved.items()}


def _read_backup_model(entry: object, *, where: str) -> BackupModel:
    """One `backup_models` entry, which must say which CLI runs it.

    Raises rather than assuming a harness. The assumption is what this replaced:
    an entry that names only a model gets whichever harness the restaffing search
    landed on, so a config could declare an escape to another vendor that the
    code had no way to read. A refusal at load is where an operator can still fix
    it; the alternative surfaces as a spawn failure during an outage, which is
    the worst moment to be reading a staffing file for the first time.
    """

    if not isinstance(entry, dict) or "harness" not in entry or "model" not in entry:
        raise ValueError(
            f"{where}.backup_models entries must name the harness that runs them: "
            f'write {{ harness = "codex", model = "..." }}, not {entry!r}'
        )
    return BackupModel(
        harness=Harness(entry["harness"]),
        model=str(entry["model"]),
        reasoning_effort=entry.get("reasoning_effort"),
    )


def _read_slot(table: dict[str, Any], *, where: str) -> BenchSlot:
    """One seat, from the table an operator wrote for it."""

    workload_table = table.get("workloads", {})
    return BenchSlot(
        harness=Harness(table["harness"]),
        model=table.get("model"),
        capacity=int(table.get("capacity", 1)),
        backup_models=tuple(
            _read_backup_model(entry, where=where) for entry in table.get("backup_models", [])
        ),
        reasoning_effort=table.get("reasoning_effort"),
        workload_profiles=tuple(
            WorkloadModelProfile(
                workload=JudgmentWorkload(workload_name),
                model=str(profile["model"]),
                reasoning_effort=profile.get("reasoning_effort"),
            )
            for workload_name, profile in workload_table.items()
        ),
    )


def _read_pairings(data: dict[str, Any]) -> dict[str, FrontierPairing]:
    """Every pairing the file declares, each checked as it is built."""

    pairings: dict[str, FrontierPairing] = {}
    for name, table in data.get("pairings", {}).items():
        if "same_model_review_accepted" in table:
            # A stale key from the flag's ten-day life is refused by name rather
            # than ignored, because ignoring it would tell an operator their
            # acknowledgement was on record when nothing reads it.
            raise ValueError(
                f"pairing {name!r} sets same_model_review_accepted, which no longer "
                "exists: a shared seat is unrepresentable now. Name a different "
                "model for one seat"
            )
        for seat in ("senior", "staff"):
            if seat not in table:
                raise ValueError(
                    f"pairing {name!r} declares no {seat} seat; a pairing is both seats "
                    "or it is not a pairing"
                )
            if "backup_models" in table[seat]:
                # Refused rather than merged or overridden. A paired seat's escape
                # is the pairing's `fallback`, which moves both seats at once; a
                # per-seat backup beside it is a second way to say the same thing,
                # and the two would disagree the first time somebody edited one.
                raise ValueError(
                    f"pairing {name!r} declares backup_models on its {seat} seat. A "
                    "paired seat escapes with its pair: name another pairing in this "
                    "one's `fallback` instead"
                )
        declared = table.get("fallback", ())
        fallback = (declared,) if isinstance(declared, str) else tuple(declared)
        pairings[name] = FrontierPairing(
            name=name,
            senior=_read_slot(table["senior"], where=f"pairings.{name}.senior"),
            staff=_read_slot(table["staff"], where=f"pairings.{name}.staff"),
            fallback=fallback,
        )
    return pairings


def _seated_pairing(data: dict[str, Any], pairings: dict[str, FrontierPairing]) -> FrontierPairing:
    """Which declared pairing is holding the frontier seats right now.

    Named explicitly even when the file declares exactly one, because the point
    of writing several is that swapping between them is the edit an operator
    makes under pressure, and "the only one" stops being a rule the moment a
    second is added.
    """

    name = data.get("seated_pairing")
    if name is None:
        raise ValueError(
            "configs/staffing.toml declares pairings but names none as seated; add a "
            f'top-level `seated_pairing = "..."` naming one of {sorted(pairings)}'
        )
    try:
        return pairings[str(name)]
    except KeyError:
        raise ValueError(
            f"seated_pairing names {name!r}, which is not declared; the file has "
            f"{sorted(pairings) or 'no pairings'}"
        ) from None


def load_staffing(config_path: Path) -> Staffing:
    """Load the whole staffing declaration from a staffing.toml.

    The two frontier seats come from a `[pairings.<name>]` table and are resolved
    together; `[bench.<tier>]` declares the seats that are chosen alone, which
    today is junior. A `[bench.senior]` or `[bench.staff]` table is refused
    rather than accepted alongside, because two ways to seat the pair is the
    drift the pairing exists to remove - and the file that disagreed with itself
    is the failure this repository has actually had.

    A file with no pairing tables at all also refuses, unless it is entirely
    absent (a fresh clone falls back to `DEFAULT_STAFFING`): a staffing file
    that seats no frontier pair has not declared a staffing.
    """

    if not config_path.exists():
        return DEFAULT_STAFFING
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    bench_table = data.get("bench", {})
    paired = {tier.value for tier in (Tier.SENIOR, Tier.STAFF)}
    named_alone = sorted(paired & set(bench_table))
    if named_alone:
        raise ValueError(
            f"{', '.join(f'[bench.{name}]' for name in named_alone)} seats half of a "
            "pair. The frontier seats are declared together in a [pairings.<name>] "
            "table and selected with a top-level `seated_pairing`"
        )
    solo: Bench = {
        Tier(tier_name): _read_slot(slot, where=f"bench.{tier_name}")
        for tier_name, slot in bench_table.items()
    }
    pairings = _read_pairings(data)
    if not pairings:
        raise ValueError(
            f"{config_path} declares no [pairings.<name>] table, so it seats no "
            "frontier pair; declare one, or remove the file to run on the defaults"
        )
    return Staffing(
        pairings=pairings,
        seated=_seated_pairing(data, pairings),
        solo=solo,
    )


def load_bench(config_path: Path) -> Bench:
    """The tier -> slot mapping a spawner needs, from the same declaration.

    Kept beside `load_staffing` because most consumers - the capability gate,
    the probes, the executors - resolve tiers and never restaff; handing them
    the pairing structure would be handing them a decision that is not theirs.
    The one consumer that does restaff, the dispatch path, loads the full
    `Staffing`.
    """

    return load_staffing(config_path).bench
