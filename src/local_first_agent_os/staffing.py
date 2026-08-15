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
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import assert_never

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
class BenchSlot:
    """Who is available at one tier: a harness, an optional model, a capacity."""

    harness: Harness
    model: str | None = None  # None => harness/CLI default (e.g. subscription)
    capacity: int = 1  # max concurrent instances of this tier
    backup_models: tuple[str, ...] = ()  # alternates for the same tier (e.g. eval comparison)
    reasoning_effort: str | None = None  # harness-specific effort knob (e.g. codex high)

    def to_payload(self) -> dict[str, object]:
        return {
            "harness": self.harness.value,
            "model": self.model,
            "capacity": self.capacity,
            "reasoning_effort": self.reasoning_effort,
            "backup_models": list(self.backup_models),
        }


# The Bench is the ONE place that knows tier -> runtime.
Bench = dict[Tier, BenchSlot]


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
# so review is not the author re-reading itself. The repo config's own copy of
# that property is enforced by
# `test_staffing.py::test_the_repo_bench_never_lets_the_reviewer_be_the_author`,
# and `_warn_when_review_is_self_review` says it out loud at load for any bench.
# No test compares this table to the config, deliberately: an equality would
# forbid the outage seating the config is entitled to declare.
DEFAULT_BENCH: Bench = {
    Tier.STAFF: BenchSlot(harness=Harness.CODEX, model=None, capacity=1),
    Tier.SENIOR: BenchSlot(
        harness=Harness.CLAUDE,
        model=None,
        capacity=3,
        reasoning_effort="max",
    ),
    Tier.JUNIOR: BenchSlot(
        harness=Harness.PI,
        model="gemma4",
        capacity=4,
        backup_models=("qwen3.8-27b-mtp",),
    ),
}

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


def load_bench(config_path: Path) -> Bench:
    """Load the tier->harness bench from a staffing.toml, falling back to defaults."""
    if not config_path.exists():
        return dict(DEFAULT_BENCH)
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    bench_table = data.get("bench", {})
    bench: Bench = {}
    for tier_name, slot in bench_table.items():
        tier = Tier(tier_name)
        bench[tier] = BenchSlot(
            harness=Harness(slot["harness"]),
            model=slot.get("model"),
            capacity=int(slot.get("capacity", 1)),
            backup_models=tuple(slot.get("backup_models", [])),
            reasoning_effort=slot.get("reasoning_effort"),
        )
    resolved = bench or dict(DEFAULT_BENCH)
    _warn_when_review_is_self_review(resolved)
    return resolved


def _warn_when_review_is_self_review(bench: Bench) -> None:
    """One warning when senior and staff resolve to the same (harness, model).

    A warning rather than a refusal, because the configuration is legal and can
    be deliberate: with every subscription but one down, an operator may accept
    same-model review as the last resort. What must not happen silently is the
    property change: the reviewer is then the model that wrote the change, so
    review keeps its fresh pass and loses its second pair of eyes.

    Two different models from one provider stay silent on purpose; that is the
    sanctioned outage fallback. Two slots on one harness where only one pins a
    model also stay silent, because whether they coincide depends on the CLI's
    default and cannot be decided here. Both unpinned on one harness does warn:
    the same CLI default is the same model.
    """

    senior = bench.get(Tier.SENIOR)
    staff = bench.get(Tier.STAFF)
    if senior is None or staff is None:
        return
    if senior.harness is not staff.harness or senior.model != staff.model:
        return
    logger.warning(
        "senior and staff both resolve to harness %r with model %r; the reviewer is the "
        "model that wrote the change, so review keeps its fresh pass and loses its "
        "second pair of eyes",
        senior.harness.value,
        senior.model or "(CLI default)",
    )
