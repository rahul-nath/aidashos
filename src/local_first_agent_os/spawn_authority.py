# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What a process spawned for a piece of work is allowed to do.

The compiled plan already computes this. Every ``ExecutorKind`` declares a
``permitted_tools`` tuple of ``Capability``, the compiler copies it into the
milestone's ``ToolPolicy``, and it is hashed into the immutable plan. It then
reaches ``MilestoneContext.permitted_tools``, gets rendered into the agent's
prompt as the line ``Permitted tools: ...``, and is dropped.

The spawn decision was made somewhere else entirely, from a boolean:

    is_review = task.judgment.name == "reviewer" or "review" in task.task_name.lower()

and every task that boolean called false was launched with
``--dangerously-skip-permissions`` or ``--dangerously-bypass-approvals-and-sandbox``.
So a planning agent, whose declaration permits only ``read_repository`` and
``invoke_model``, got a shell with no sandbox and write access to the checkout,
and a task named ``review_next_step`` got a read-only spawn while not being a
review to any other part of the system.

This module is the missing derivation: capabilities in, posture out, and the
posture is what the command builder takes. Nothing here reads a task name.

Two facts compose, and they are different facts. The dispatch intent carries a
**ceiling** - what the milestone that asked for this work may do at all - and the
task carries a **role**. One milestone's intent fans out into an implementer, a
reviewer, and a junior, and the reviewer must not inherit ``write_repository``
from the ceiling. Intersection is the only composition of two authorities that
cannot widen either one, which is why ``narrowed_to`` is the only way to combine
them here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, assert_never

from .capabilities import Capability, parse_capability
from .work_units.executors import EXECUTOR_REGISTRY, ExecutorKind

if TYPE_CHECKING:
    from .pow_wow.protocol import TaskPurpose

# `TaskPurpose` is imported lazily inside `authority_for_purpose`, its one
# runtime use, rather than at module level. Importing anything under `pow_wow`
# executes the package `__init__`, which imports the whole executor, which
# imports this module back - so a process that touched `spawn_authority` first
# crashed on a half-initialized module, while one that touched `pow_wow` first
# never saw it. The full test suite happens to be the second kind of process and
# `pytest tests/test_staffing.py` alone was the first, which is how this hid.


@dataclass(frozen=True)
class ReadOnlyInspection:
    """It may look. It may not write the checkout and may not run a shell."""


@dataclass(frozen=True)
class SupervisedCommands:
    """It may run commands but not write the checkout.

    A test runner and a repository validator live here: both need a shell and
    neither should be able to edit the thing they are checking. Today both get an
    implementer's full bypass, because the only distinction that existed was
    review versus not-review.
    """


@dataclass(frozen=True)
class UnattendedImplementation:
    """It may write the checkout and run commands, unsupervised.

    The only posture that earns a bypass flag, and it is earned by declaring both
    capabilities rather than by not being a review.
    """


# Three variants rather than an enum so a fourth - a deploy posture for
# `deliver.deployment`, which holds `publish_deployment` and today receives an
# implementer's bypass - is addable without editing the three that exist.
type SpawnPosture = ReadOnlyInspection | SupervisedCommands | UnattendedImplementation


def describe_posture(posture: SpawnPosture) -> str:
    """A stable name for a posture, for artifacts and logs."""

    match posture:
        case ReadOnlyInspection():
            return "read_only_inspection"
        case SupervisedCommands():
            return "supervised_commands"
        case UnattendedImplementation():
            return "unattended_implementation"
    assert_never(posture)


@dataclass(frozen=True)
class SpawnAuthority:
    """What a spawned process may do, as a value rather than as a flag."""

    capabilities: frozenset[Capability]

    @classmethod
    def of(cls, capabilities: Iterable[Capability]) -> SpawnAuthority:
        return cls(frozenset(capabilities))

    @classmethod
    def nothing(cls) -> SpawnAuthority:
        """The authority of work nothing has declared anything about.

        Empty, not "everything". A missing declaration is the case that produced
        the defect, so the safe reading is the one that grants least: an intent
        submitted before the capability column existed gets a read-only spawn
        rather than the bypass it used to get.
        """

        return cls(frozenset())

    @classmethod
    def for_executor(cls, kind: ExecutorKind) -> SpawnAuthority:
        """The ceiling one milestone's executor kind declares."""

        return cls(frozenset(EXECUTOR_REGISTRY[kind].permitted_tools))

    @classmethod
    def from_names(cls, names: Iterable[str]) -> SpawnAuthority:
        """Parse a persisted capability list, refusing a name nothing answers to.

        `parse_capability` raises rather than skipping, because a grant for a
        capability that does not exist can never be satisfied and dropping it
        silently would widen or narrow the set with nobody deciding which.
        """

        return cls(frozenset(parse_capability(name) for name in names))

    def to_names(self) -> tuple[str, ...]:
        return tuple(sorted(item.value for item in self.capabilities))

    def narrowed_to(self, other: SpawnAuthority) -> SpawnAuthority:
        """The authority both this and ``other`` allow.

        Intersection, so combining a ceiling with a role can only ever remove.
        Union would let a reviewer's task inherit an implementer's ceiling, which
        is the failure this whole module exists to make unwritable.
        """

        return SpawnAuthority(self.capabilities & other.capabilities)

    def posture(self) -> SpawnPosture:
        """Which of the three postures this authority earns.

        The rule the compiled ACL document states, spelled once: a bypass is
        emitted if and only if the capability set holds both ``write_repository``
        and ``run_command``.
        """

        writes = Capability.WRITE_REPOSITORY in self.capabilities
        runs = Capability.RUN_COMMAND in self.capabilities
        if writes and runs:
            return UnattendedImplementation()
        if runs:
            return SupervisedCommands()
        return ReadOnlyInspection()


def authority_for_purpose(purpose: TaskPurpose | None) -> SpawnAuthority:
    """The most any task of this purpose may do, before the ceiling narrows it.

    A role bound, not a grant. It cannot widen anything: the caller intersects it
    with the intent's ceiling, so a purpose naming ``write_repository`` still
    gets nothing if the milestone that asked for the work never had it.

    Exhaustive over ``TaskPurpose`` so a seventh purpose is a type error rather
    than an unlisted one silently receiving whatever the ceiling holds.
    """

    from .pow_wow.protocol import TaskPurpose

    match purpose:
        case TaskPurpose.IMPLEMENTATION | TaskPurpose.RECOVERY_REVISION:
            # No `publish_deployment`, deliberately, and no task purpose grants
            # it. Deploying is an operator action - the repository's own doctrine
            # is that nothing auto-merges, deploys, purchases, or sends external
            # communication without the approval gate - so an agent role that
            # could carry it would be the gate written in the wrong place.
            #
            # It was here, and `POLICIES.md` is what found it: an
            # implementer built with an unbounded ceiling was being authorized
            # for deployment it had no use for and no plan asked for.
            return SpawnAuthority.of(
                (
                    Capability.READ_REPOSITORY,
                    Capability.WRITE_REPOSITORY,
                    Capability.RUN_COMMAND,
                    Capability.INVOKE_MODEL,
                    Capability.WRITE_ARTIFACT,
                    Capability.ASK_OPERATOR,
                )
            )
        case TaskPurpose.DETERMINISTIC_CHECK | TaskPurpose.BROWSER_ACCEPTANCE:
            # A check runs things and reports; it does not edit what it checks.
            return SpawnAuthority.of(
                (
                    Capability.READ_REPOSITORY,
                    Capability.RUN_COMMAND,
                    Capability.INVOKE_MODEL,
                )
            )
        case TaskPurpose.REVIEW | TaskPurpose.ADVISORY:
            # A reviewer inspects. It has never been permitted to mutate, and the
            # old boolean got this one right for the wrong reason.
            return SpawnAuthority.of(
                (
                    Capability.READ_REPOSITORY,
                    Capability.INVOKE_MODEL,
                    Capability.ASK_OPERATOR,
                )
            )
        case None:
            # `PowWowTaskSpec.__post_init__` always resolves a purpose, so this
            # is unreachable through the spec. Answering with the narrowest
            # authority keeps it that way if some other caller appears.
            return SpawnAuthority.nothing()
    assert_never(purpose)


__all__ = [
    "ReadOnlyInspection",
    "SpawnAuthority",
    "SpawnPosture",
    "SupervisedCommands",
    "UnattendedImplementation",
    "authority_for_purpose",
    "describe_posture",
]
