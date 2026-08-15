# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Whether a run earned the right to leave a checkpoint behind.

The gate used to be ``all(capture.exit_code == 0 for capture in captures)``, which
is ``True`` on an empty tuple. A project declaring ``verification_commands = []``
therefore produced a checkpoint commit, a completed task, and a ``CODE_MERGE``
approval having verified nothing, and every reader downstream that trusts
"verification passed" was trusting that.

The bug is not the missing conditional. It is that one boolean was carrying two
questions - *did anything run* and *did what ran succeed* - and a tuple has no
room to say "nothing was ever going to run here". So the answer is a sum with a
member for each, and the checkpoint is permitted by exactly one of them.

``VerificationIncomplete`` exists because an empty capture tuple is genuinely
ambiguous at the call sites: verification is also skipped when the agent command
failed, when the task is a review, and when a supervisor parked the run at a
checkpoint. Those are not "verified" either, and folding them into the same
member as "none declared" would rebuild the conflation one level up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from .types import CommandRunCapture


@dataclass(frozen=True)
class VerificationNotDeclared:
    """No verification command was configured, so nothing was ever going to run.

    This is the state the gate used to read as success. It is a statement about
    configuration rather than about the run, which is why it carries no captures
    and why its remedy names the registry rather than the code.
    """


@dataclass(frozen=True)
class VerificationIncomplete:
    """Commands were declared and not all of them produced a capture.

    Reached when the run stopped before verification: the agent command failed,
    the task was a review, or a supervisor parked it. Distinct from failure,
    because nothing has been shown to be broken - only unproven.
    """

    declared: tuple[str, ...]
    captures: tuple[CommandRunCapture, ...]


@dataclass(frozen=True)
class VerificationPassed:
    """Every declared command ran and exited zero. The only member that certifies."""

    captures: tuple[CommandRunCapture, ...]


@dataclass(frozen=True)
class VerificationFailed:
    """Every declared command ran and at least one exited non-zero."""

    captures: tuple[CommandRunCapture, ...]

    @property
    def failed(self) -> tuple[CommandRunCapture, ...]:
        return tuple(capture for capture in self.captures if capture.exit_code != 0)


VerificationOutcome = (
    VerificationNotDeclared | VerificationIncomplete | VerificationPassed | VerificationFailed
)


def classify_verification(
    declared: tuple[str, ...],
    captures: tuple[CommandRunCapture, ...],
) -> VerificationOutcome:
    """What the declared commands and their captures amount to.

    Total over both inputs, and deliberately takes the declared commands rather
    than the captures alone: the captures cannot distinguish "none were declared"
    from "none had a chance to run", and that distinction is the whole point.
    """

    if not declared:
        return VerificationNotDeclared()
    if len(captures) != len(declared):
        return VerificationIncomplete(declared=declared, captures=captures)
    if all(capture.exit_code == 0 for capture in captures):
        return VerificationPassed(captures=captures)
    return VerificationFailed(captures=captures)


def checkpoint_permitted(outcome: VerificationOutcome) -> bool:
    """Whether this outcome may leave a durable checkpoint commit.

    Exhaustive on purpose. A member added later without a decision here is a type
    error rather than a silent default, which is the property the old ``all()``
    did not have.
    """

    match outcome:
        case VerificationPassed():
            return True
        case VerificationNotDeclared() | VerificationIncomplete() | VerificationFailed():
            return False
        case _:
            assert_never(outcome)


def uncertifiable_reason(outcome: VerificationOutcome, *, target_project_id: str) -> str | None:
    """The operator sentence for a run that could not be certified, or ``None``.

    Only ``VerificationNotDeclared`` produces one. A failed verification already
    reports itself through the captures' exit codes, and an incomplete one is
    explained by whatever stopped the run; neither needs a second voice. A run
    that verified nothing has no other voice at all, which is why this exists.
    """

    match outcome:
        case VerificationNotDeclared():
            return (
                f"Verification was never declared for target project {target_project_id!r}, "
                "so this run cannot be certified and no checkpoint was committed. "
                "Declare verification_commands for the project, or mark it read_only "
                "if it must not take code work."
            )
        case VerificationIncomplete() | VerificationPassed() | VerificationFailed():
            return None
        case _:
            assert_never(outcome)


__all__ = [
    "VerificationFailed",
    "VerificationIncomplete",
    "VerificationNotDeclared",
    "VerificationOutcome",
    "VerificationPassed",
    "checkpoint_permitted",
    "classify_verification",
    "uncertifiable_reason",
]
