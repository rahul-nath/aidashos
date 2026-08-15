# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One project's access decision, and its remit.

`read_only` is a boolean, which is the weakest sum: read-only versus read-write
leaves no room for append-only or review-only without a second boolean beside the
first that everyone must remember to check. `AccessMode` makes a third case a
member rather than a field.

`owns` and `avoid` sit here because they answer the same question - what is this
project for - but they are emphatically *not* path rules. The registry spells them
as prose: "voice and terminal command interface", "raw personal-memory exports".
They are a remit for a model to read, and treating them as globs matched against
changed files makes every file in the repository look out of bounds. They belong
in a prompt, not in a gate, and this type carries them so a caller cannot mistake
one for the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AccessMode(StrEnum):
    """How much of a project an agent may change.

    The two members are what the configuration expresses today. The point of the
    type is that a third - append-only, review-only, writable-except - arrives as
    a member and a match arm rather than as another boolean nobody remembers to
    check alongside the first.
    """

    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


@dataclass(frozen=True)
class WriteAllowed:
    """The path may be written."""


@dataclass(frozen=True)
class WriteRefused:
    """The path may not be written, and why.

    A reason rather than a bare False because the two causes need different
    operator responses: a read-only project is a registry decision, an avoided
    path is a decision about one directory inside a writable project.
    """

    reason: str


WriteVerdict = WriteAllowed | WriteRefused


@dataclass(frozen=True)
class ProjectAccessPolicy:
    """One project's access decision, whole.

    ``owns`` and ``avoid`` are prose statements of remit, not patterns. Nothing
    matches them mechanically; they exist to tell an agent what this project is
    responsible for and what it should leave alone.
    """

    mode: AccessMode
    owns: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()

    @property
    def read_only(self) -> bool:
        """Kept so existing callers keep working while they migrate.

        The boolean is now derived from the mode rather than stored beside it, so
        the two can no longer disagree.
        """

        return self.mode is AccessMode.READ_ONLY

    def to_payload(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "owns": list(self.owns),
            "avoid": list(self.avoid),
        }


def access_policy_from_record(
    *,
    read_only: bool,
    owns: tuple[str, ...] | list[str],
    avoid: tuple[str, ...] | list[str],
) -> ProjectAccessPolicy:
    """Build a policy from the three fields the registry file still spells apart."""

    return ProjectAccessPolicy(
        mode=AccessMode.READ_ONLY if read_only else AccessMode.READ_WRITE,
        owns=tuple(owns),
        avoid=tuple(avoid),
    )


__all__ = [
    "AccessMode",
    "ProjectAccessPolicy",
    "WriteAllowed",
    "WriteRefused",
    "WriteVerdict",
    "access_policy_from_record",
]
