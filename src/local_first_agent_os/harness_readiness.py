# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Whether the frontier CLIs a run needs are actually signed in.

A logged-out harness is not a failure of the work. It is a fact about the
machine, knowable in under a second, and the system used to discover it by
spawning agents and reading `exit=1` several minutes into a run.

Deliberately not part of compilation. The compiler is pure and offline, and one
document must compile to one plan hash on every machine; a check that reads the
environment would make the same text produce different plans on different hosts.
This answers a different question - "may this run start *here, now*" - which is
why it belongs at the point a human grants execution.

Only harnesses the bench actually names are probed. Staffing every tier to the
local model is a supported configuration, and refusing to start it because a
frontier CLI nobody asked for is logged out would be a check inventing its own
requirement.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from .staffing import (
    Bench,
    BenchSlot,
    FrontierHarness,
    FrontierPairing,
    LocalHarness,
    Staffing,
    classify_harness,
    resolve_bench,
)
from .vocabulary import DispatchTier

_PROBE_TIMEOUT_SECONDS: Final = 20.0


@dataclass(frozen=True, slots=True)
class HarnessReady:
    """The CLI answered and holds a usable credential."""

    harness: FrontierHarness

    @property
    def ready(self) -> bool:
        return True

    def describe(self) -> str:
        return f"{self.harness.value} is signed in"


@dataclass(frozen=True, slots=True)
class HarnessNotReady:
    """The CLI is present and answered, and says it cannot act.

    ``remedy`` is the whole point of the type. An operator reading this needs the
    command that fixes it, not a description of the state, because the state is
    one command away from resolved and a message that omits it turns a
    twenty-second fix into a search.
    """

    harness: FrontierHarness
    detail: str
    remedy: str

    @property
    def ready(self) -> bool:
        return False

    def describe(self) -> str:
        return f"{self.harness.value}: {self.detail}. Fix with: {self.remedy}"


@dataclass(frozen=True, slots=True)
class HarnessUnknown:
    """The probe itself could not answer.

    Distinct from ``HarnessNotReady`` because the responses differ. A CLI that is
    absent, hung, or speaking an unrecognised dialect has told us nothing about
    its credential, and reporting that as "logged out" would send an operator to
    re-run a login that was never the problem.
    """

    harness: FrontierHarness
    detail: str

    @property
    def ready(self) -> bool:
        return False

    def describe(self) -> str:
        return f"{self.harness.value}: readiness unknown ({self.detail})"


HarnessReadiness = HarnessReady | HarnessNotReady | HarnessUnknown


def _run_probe(command: list[str]) -> subprocess.CompletedProcess[str] | None:
    if shutil.which(command[0]) is None:
        return None
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell, no user input
            command,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _probe_claude() -> HarnessReadiness:
    """`claude auth status`, read by its field rather than its exit code.

    It exits zero whether or not anybody is signed in, so the exit code answers a
    question nobody asked. `loggedIn` is the answer, and reading the code instead
    would report a logged-out machine as ready - the precise mistake this module
    exists to stop making at a more expensive layer.

    What this cannot tell you is whether the credential *works*. `loggedIn` is
    true for any non-empty `CLAUDE_CODE_OAUTH_TOKEN`, including a garbage one,
    because the CLI reports the presence of a credential rather than the result
    of using it. So a pass here narrows the failure and does not eliminate it:
    the cheap check catches the common case, and a token that is present and
    rejected still surfaces where it always did, in the run. Probing validity
    would mean spending a real API call on every start, which is the cost this
    check exists to avoid.
    """

    harness = FrontierHarness.CLAUDE
    completed = _run_probe(["claude", "auth", "status"])
    if completed is None:
        return HarnessUnknown(harness=harness, detail="claude CLI is not installed or did not run")
    try:
        payload = json.loads(completed.stdout)
    except (TypeError, ValueError):
        return HarnessUnknown(harness=harness, detail="claude auth status did not return JSON")
    if not isinstance(payload, dict) or "loggedIn" not in payload:
        return HarnessUnknown(harness=harness, detail="claude auth status omitted loggedIn")
    if payload["loggedIn"] is True:
        return HarnessReady(harness=harness)
    return HarnessNotReady(
        harness=harness,
        detail="not signed in, so every senior task would exit 1 without a reason",
        remedy="claude auth login (or `claude setup-token` for unattended runs)",
    )


def _probe_codex() -> HarnessReadiness:
    """`codex login status`, which does answer with its exit code."""

    harness = FrontierHarness.CODEX
    completed = _run_probe(["codex", "login", "status"])
    if completed is None:
        return HarnessUnknown(harness=harness, detail="codex CLI is not installed or did not run")
    if completed.returncode == 0:
        return HarnessReady(harness=harness)
    return HarnessNotReady(
        harness=harness,
        detail="not signed in, so every staff review would fail to start",
        remedy="codex login",
    )


def probe_harness(harness: FrontierHarness) -> HarnessReadiness:
    match harness:
        case FrontierHarness.CLAUDE:
            return _probe_claude()
        case FrontierHarness.CODEX:
            return _probe_codex()


def frontier_harnesses_on_bench(
    bench: Bench | Staffing | None = None,
) -> frozenset[FrontierHarness]:
    """Which spawnable CLIs this machine's staffing actually calls for.

    A tier staffed to the local delegate contributes nothing, which is what keeps
    an all-local bench startable with no frontier account at all.

    A `Staffing` answers with everything in play - the seated pairing, its
    declared escapes, and any solo frontier tier - because the caller holding
    one is a door, and a door that probed only the seated vendors would find
    every escape `HarnessUnknown` and refuse an outage a proven way out could
    have absorbed. A bare bench has no escape declarations, so it answers with
    its own tiers, exactly as before.
    """

    if isinstance(bench, Staffing):
        return bench.frontier_harnesses_in_play()
    harnesses: set[FrontierHarness] = set()
    for tier in DispatchTier:
        kind = classify_harness(resolve_bench(tier, bench).harness)
        if isinstance(kind, LocalHarness):
            continue
        harnesses.add(kind)
    return frozenset(harnesses)


def check_frontier_readiness(bench: Bench | Staffing | None = None) -> tuple[HarnessReadiness, ...]:
    return tuple(probe_harness(harness) for harness in sorted(frontier_harnesses_on_bench(bench)))


@dataclass(frozen=True, slots=True)
class TierServed:
    """The tier's configured slot can act, so nothing deviates from the bench."""

    tier: DispatchTier
    configured: BenchSlot

    @property
    def startable(self) -> bool:
        return True

    @property
    def slot(self) -> BenchSlot:
        return self.configured

    def describe(self) -> str:
        return f"{self.tier.value} runs on {self.configured.harness.value} as configured"


@dataclass(frozen=True, slots=True)
class TierRestaffed:
    """The configured harness cannot act and a ready frontier peer takes the tier.

    Only a *frontier* peer is eligible. The local harness is never substituted in
    here, because "some process answered" is not the question - the question is
    whether the replacement can do this tier's work, and a served local model
    standing in for a frontier implementer is the silent-substitution failure
    this module exists to avoid rather than an instance of recovering from it.

    The bench is an operator decision, so this deviating from it is a real
    override and `restaffings()` exists to say so out loud.
    """

    tier: DispatchTier
    configured: BenchSlot
    replacement: BenchSlot
    detail: str

    @property
    def startable(self) -> bool:
        return True

    @property
    def slot(self) -> BenchSlot:
        """What this tier actually runs on.

        The harness, its model, and its effort knob come from the replacement,
        because all three are properties of the provider being asked; a
        `reasoning_effort` that means something to one CLI is not a setting the
        other one has. The capacity stays the configured tier's, because how much
        of this tier may run at once is a statement about the tier's role.

        One consequence is deliberate and worth naming: while a tier is
        re-staffed, the replacement provider carries both tiers' concurrency at
        the same time. Bounding that is a separate decision from being able to
        start at all, and it is not made here.
        """

        return BenchSlot(
            harness=self.replacement.harness,
            model=self.replacement.model,
            capacity=self.configured.capacity,
            backup_models=self.replacement.backup_models,
            reasoning_effort=self.replacement.reasoning_effort,
            # The replacement's own profiles, not the configured seat's. They
            # name models in the provider that is actually going to run, so
            # they travel with the seat that brought them; the configured
            # seat's would name ids in the spent provider's vocabulary.
            #
            # Dropping them was a silent and expensive defect. A restaffed seat
            # lost its `independent_reading` profile, so
            # `resolve_bench_for_workload` found none and handed reading tasks
            # the full seat model - during a quota outage, which is exactly when
            # the cheap profile matters most. Observed on work unit
            # c88ff4167c66 (2026-08-30): the two independent-reading tasks ran
            # `claude-opus-5` and `claude-fable-5` while the file declared
            # `claude-sonnet-5` for both, and the Fable reading exhausted that
            # model's credits and blocked the milestone.
            workload_profiles=self.replacement.workload_profiles,
        )

    def describe(self) -> str:
        return (
            f"{self.tier.value} moves from {self.configured.harness.value} to "
            f"{self.replacement.harness.value}: {self.detail}"
        )


@dataclass(frozen=True, slots=True)
class TierUnstaffable:
    """No harness on this bench can act for this tier.

    The only state that justifies refusing to start. A tier whose harness is
    down but whose peer is up is not this, which is the whole point: refusing
    the run then would turn a condition the system can recover from into a stop
    the operator has to clear by hand.
    """

    tier: DispatchTier
    configured: BenchSlot
    detail: str

    @property
    def startable(self) -> bool:
        return False

    @property
    def slot(self) -> BenchSlot:
        return self.configured

    def describe(self) -> str:
        return f"{self.tier.value} cannot run: {self.detail}"


TierStaffing = TierServed | TierRestaffed | TierUnstaffable


def _ready_frontier_peer(
    failed: FrontierHarness,
    readiness: dict[FrontierHarness, HarnessReadiness],
    bench: Bench | None = None,
) -> BenchSlot | None:
    """A frontier slot on this bench, other than the failed one, that answered ready.

    `HarnessUnknown` is not eligible. Substituting onto a harness that could not
    be probed would trade a refusal we understand for a run we do not, and the
    probe layer already decided that not knowing is reported rather than acted
    on.
    """

    for tier in DispatchTier:
        slot = resolve_bench(tier, bench)
        kind = classify_harness(slot.harness)
        if isinstance(kind, LocalHarness) or kind is failed:
            continue
        if isinstance(readiness.get(kind), HarnessReady):
            return slot
    return None


def _paired_door_staffing(
    staffing: Staffing, readiness: dict[FrontierHarness, HarnessReadiness]
) -> dict[DispatchTier, TierStaffing]:
    """The frontier pair at a door, moved as the one decision it is declared as.

    The mirror of `harness_availability._paired_tier_staffing`, and deliberately
    a separate body: that one is handed a set of spent quotas the ledger
    reported, this one a probe's answer per harness. Same shape, different
    evidence, and the difference is not cosmetic - a spent quota is a fact about
    a vendor, while `HarnessUnknown` is the absence of a fact, and only this side
    has to decide what to do about not knowing.

    It decides the same way `_ready_frontier_peer` always did: a pairing is a
    candidate only if every vendor it needs answered `HarnessReady`. Moving onto
    a harness that could not be probed would trade a refusal we understand for a
    run we do not. But `HarnessNotReady` on the *seated* pairing is what triggers
    a move; an unprobed seated vendor is not, because not knowing is reported
    rather than acted on.
    """

    seats = staffing.seated.seats()
    blocked = [
        readiness[kind]
        for kind in sorted(staffing.seated.frontier_harnesses())
        if isinstance(readiness.get(kind), HarnessNotReady)
    ]
    if not blocked:
        return {tier: TierServed(tier=tier, configured=slot) for tier, slot in seats.items()}
    detail = "; ".join(state.describe() for state in blocked)

    def usable(pairing: FrontierPairing) -> bool:
        return all(
            isinstance(readiness.get(kind), HarnessReady) for kind in pairing.frontier_harnesses()
        )

    target = next((pairing for pairing in staffing.escape_pairings() if usable(pairing)), None)
    if target is None:
        chain = ", ".join(staffing.seated.fallback) or "none declared"
        return {
            tier: TierUnstaffable(
                tier=tier,
                configured=slot,
                detail=f"{detail}; no fallback pairing is ready (chain: {chain})",
            )
            for tier, slot in seats.items()
        }
    return {
        tier: TierRestaffed(
            tier=tier,
            configured=slot,
            replacement=target.seats()[tier],
            detail=f"{detail}; the pair moves to pairing {target.name!r}",
        )
        for tier, slot in seats.items()
    }


def plan_tier_staffing(
    bench: Bench | Staffing | None = None,
    states: Iterable[HarnessReadiness] | None = None,
) -> tuple[TierStaffing, ...]:
    """How each tier is actually staffed once this machine has been asked.

    The readiness probe answers per harness, and "can this run start" is per
    tier. Those came apart the moment a bench staffed two tiers to two different
    providers: a logged-out claude said nothing about whether codex could take
    senior work, and refusing on the harness answer stopped runs that had a
    ready peer sitting on the same bench.

    Handed a `Staffing`, the two frontier seats move as a pair to a declared
    fallback pairing, exactly as the dispatch path moves them. The door used to
    restaff per tier onto a ready peer's slot, which meant the operator-facing
    answer and the unattended one could disagree about the same outage: the
    door would report senior moved onto the staff seat's model while the
    dispatcher moved the pair to a pairing an operator had actually declared and
    checked. One outage, two stories, and the door's was the one a human read
    before granting execution.

    Handed a bare `Bench`, every tier walks the per-tier rule: a bench carries no
    pairing declarations, so a ready peer's slot is the only replacement it can
    name.

    `states` is injectable so a caller that already probed does not probe twice,
    and so tests can describe a machine instead of being one.
    """

    readiness = {
        state.harness: state
        for state in (states if states is not None else check_frontier_readiness(bench))
    }
    paired: dict[DispatchTier, TierStaffing] = {}
    if isinstance(bench, Staffing):
        paired = _paired_door_staffing(bench, readiness)
        bench = bench.bench
    plan: list[TierStaffing] = []
    for tier in DispatchTier:
        if tier in paired:
            plan.append(paired[tier])
            continue
        slot = resolve_bench(tier, bench)
        kind = classify_harness(slot.harness)
        if isinstance(kind, LocalHarness):
            plan.append(TierServed(tier=tier, configured=slot))
            continue
        state = readiness.get(kind)
        if not isinstance(state, HarnessNotReady):
            # Ready, or a probe that could not answer. Neither is grounds to move
            # a tier off the harness its operator chose for it.
            plan.append(TierServed(tier=tier, configured=slot))
            continue
        replacement = _ready_frontier_peer(kind, readiness, bench)
        if replacement is None:
            plan.append(TierUnstaffable(tier=tier, configured=slot, detail=state.describe()))
            continue
        plan.append(
            TierRestaffed(
                tier=tier,
                configured=slot,
                replacement=replacement,
                detail=state.describe(),
            )
        )
    return tuple(plan)


def effective_bench(plan: Iterable[TierStaffing]) -> Bench:
    """The bench with substitutions applied, for a caller that resolves tiers.

    Total over the plan, including unstaffable tiers, which keep their
    configured slot. A caller is expected to have refused already if anything
    blocks; leaving a hole here would make an unstaffable tier raise `KeyError`
    somewhere far from the decision that it could not be staffed.
    """

    return {item.tier: item.slot for item in plan}


def restaffings(plan: Iterable[TierStaffing]) -> tuple[str, ...]:
    """Every tier that moved off its configured harness.

    Separate from the refusals because it is not one: the run proceeds. It is
    still not allowed to be silent, because the bench is a decision an operator
    made and this is the system declining to follow it.
    """

    return tuple(item.describe() for item in plan if isinstance(item, TierRestaffed))


def readiness_refusals(states: Iterable[HarnessReadiness]) -> tuple[str, ...]:
    """Everything an operator should read, including what could not be answered.

    ``HarnessUnknown`` is included here. A probe that could not answer is not
    evidence that the run is fine, and this is the reporting view.
    """

    return tuple(state.describe() for state in states if not state.ready)


def staffing_refusals(plan: Iterable[TierStaffing]) -> tuple[str, ...]:
    """Only what justifies refusing to start, which is a tier question.

    Renamed from `blocking_refusals`, which took harness states and refused on
    any `HarnessNotReady`. That was the right shape when the question was "is
    this CLI usable" and the wrong one for "can this run start": a bench
    staffing senior to claude and staff to codex refused the whole run when
    either provider was down, while the other sat there ready to take the work.

    The rename is the point rather than tidying. Handing the old argument to a
    tier-shaped filter returns an empty tuple, which reads as "nothing blocks"
    and is the one wrong answer that costs something here. A name that no longer
    exists fails at import instead.

    The reasoning underneath is unchanged and still the reason `TierUnknown`
    does not exist. A probe that could not answer has told us nothing, and
    refusing on ignorance would block setups this module never anticipated - an
    unusual install path, a harness reached some other way. Reporting is the
    honest response to not knowing; refusal is the honest response to being told
    that nothing on the bench can do the work.
    """

    return tuple(item.describe() for item in plan if isinstance(item, TierUnstaffable))


__all__ = [
    "HarnessNotReady",
    "HarnessReadiness",
    "HarnessReady",
    "HarnessUnknown",
    "TierRestaffed",
    "TierServed",
    "TierStaffing",
    "TierUnstaffable",
    "check_frontier_readiness",
    "effective_bench",
    "frontier_harnesses_on_bench",
    "plan_tier_staffing",
    "probe_harness",
    "readiness_refusals",
    "restaffings",
    "staffing_refusals",
]
