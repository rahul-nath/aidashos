# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A cast: several stances on one question, reduced by one synthesizer.

The tier bench answers how much model a task needs. It does not answer from
which stance, and those are different axes: a marketing read and a design read
of the same brief disagree in a way two senior engineers do not. This builds the
second axis out of what the pow-wow already has, rather than out of a new
subsystem.

Three things make that possible without new machinery. `PowWowTaskSpec.role`
reaches the spawned agent verbatim, as the "You are the ..." line
`pow_wow/prompts.py` builds, so a stance is a prompt-level fact.
`capability_gate.policy_principal`
resolves a role to its `POLICIES.md` section before it falls back to the seat, so
a member named `marketing` is governed by `## Principal: marketing` when that
section exists and by the default denials when it does not. And `AgentCaller`
already scopes every capability check to a `pow_wow_id`, so a grant given here is
given to this cast and not to the agent name at large.

The members carry no `blocked_by` between them, which is what makes them a panel
rather than a pipeline: the executor fans concurrent tasks out to the bench slot's
capacity. The synthesizer blocks on all of them, because a reduction that starts
before its inputs land is reducing nothing.

Members should not all sit on one model. Three stances sampled from one set of
weights share a prior, and their agreement measures the prior rather than the
question - the objection that killed the homogeneous junior panel on 2026-08-05.
`DispatchTier` is per member so a cast can be staffed across genuinely different
architectures, and `docs/live_evaluation.md` records which local models are
currently able to hold a seat.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..coordination.contracts import DispatchKind
from ..staffing import JudgmentRole
from ..vocabulary import DispatchTier
from .protocol import TaskPurpose
from .types import PowWowTaskSpec

SYNTHESIS_TASK_SUFFIX = "synthesis"


@dataclass(frozen=True, slots=True)
class CastMember:
    """One stance in a cast.

    ``name`` is load-bearing in three places at once: it is the task role, the
    persona the prompt asserts, and the `POLICIES.md` principal the capability
    gate resolves. Naming a member after a job rather than a mood is what makes
    the third of those useful, because a section can only grant what a reader can
    recognise.
    """

    name: str
    stance: str
    tier: DispatchTier = DispatchTier.JUNIOR

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("cast member name must be non-empty")
        if not self.stance.strip():
            raise ValueError(f"cast member {self.name!r} must declare a stance")
        if self.name != self.name.strip().lower():
            raise ValueError(
                f"cast member name must be lowercase and unpadded to match a "
                f"POLICIES.md principal: {self.name!r}"
            )


def build_cast_tasks(
    *,
    prefix: str,
    goal: str,
    members: tuple[CastMember, ...],
    synthesis_tier: DispatchTier = DispatchTier.SENIOR,
) -> tuple[PowWowTaskSpec, ...]:
    """One task per stance, plus the synthesizer that reduces them.

    Reduced by a synthesizer rather than by a vote. The parked junior swarm
    reduced a panel by majority on the first non-empty line, which returns no
    majority on exactly the prose a panel exists to produce; the 2026-08-06
    amendment concluded that a judge is the reduction that survives contact with
    the output. The synthesizer is that judge, and it is a task like any other so
    its output is ledger evidence rather than a value returned in memory.
    """

    if len(members) < 2:
        raise ValueError("a cast needs at least two stances; one stance is just a task")
    names = [member.name for member in members]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"cast member names must be unique: {', '.join(duplicates)}")

    member_tasks = tuple(
        PowWowTaskSpec(
            task_name=f"{prefix}_{member.name}",
            role=member.name,
            description=(
                f"{goal}\n\n"
                f"Answer as the {member.name}. {member.stance}\n"
                "Hold that stance rather than balancing it against the others: the "
                "cast is reduced later, and a member that pre-compromises removes "
                "the disagreement the reduction needs."
            ),
            success_criteria=(
                f"The answer is recognisably the {member.name} view rather than a neutral summary.",
                "Claims that depend on outside information say what was consulted.",
            ),
            purpose=TaskPurpose.ADVISORY,
            judgment=JudgmentRole(name=member.name, tier=member.tier, stance=member.stance),
            dispatch_kind=DispatchKind.CAST,
        )
        for member in members
    )

    synthesis = PowWowTaskSpec(
        task_name=f"{prefix}_{SYNTHESIS_TASK_SUFFIX}",
        role="synthesizer",
        description=(
            f"{goal}\n\n"
            f"Reduce the {len(members)} stances above into one recommendation: "
            f"{', '.join(names)}.\n"
            "Name the disagreements before resolving them, and say which stance "
            "each resolution favours and why. Where the stances agree, say whether "
            "they agree for the same reason, because agreement reached two ways is "
            "evidence and agreement reached one way is a shared assumption."
        ),
        success_criteria=(
            "Every stance is represented, including any the recommendation rejects.",
            "Each disagreement is resolved explicitly rather than averaged away.",
            "The recommendation is one document an operator can act on.",
        ),
        purpose=TaskPurpose.ADVISORY,
        judgment=JudgmentRole(name="synthesizer", tier=synthesis_tier, stance="reducer"),
        dispatch_kind=DispatchKind.CAST,
        blocked_by=tuple(task.task_name for task in member_tasks),
    )
    return (*member_tasks, synthesis)


__all__ = ["SYNTHESIS_TASK_SUFFIX", "CastMember", "build_cast_tasks"]
