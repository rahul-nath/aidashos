# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What `CLAUDE.md` and `AGENTS.md` may say, pinned where an edit has to see it.

Two instruction files describe one repository to two harnesses. Nothing loads
them together, so they drift silently and the drift is only visible to whoever
happens to read both, which is nobody on a normal day.

Both of these assertions exist because the drift already happened.
`CLAUDE.md` dropped an `@docs/design_principles.md` import and wrote three
paragraphs on why; `AGENTS.md` kept it, so every Codex-side session paid roughly
1400 tokens the argument had just refused, and the two files disagreed in
writing about the same decision. Both then named `engineering_doctrine.v1`
while the module had moved to v2, so an instruction file was telling agents the
version of a contract that no longer existed.

This is the cheapest place to have that argument. Someone who decides the import
belongs after all has to delete an assertion that says why it does not.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from local_first_agent_os.engineering_doctrine import CURRENT_ENGINEERING_DOCTRINE

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md")


def _read(name: str) -> str:
    """The instruction file's text, skipping when this checkout does not own one.

    `CLAUDE.md` and `AGENTS.md` are deliberately absent from the public
    snapshot's allowlist, because a public clone owns its own instructions and
    the private ones point at handoffs and design principles that do not travel.
    This suite does travel, so without the guard these five tests fail in the
    snapshot forever, for a reason that is a deliberate decision rather than a
    regression. A failure nobody can fix teaches a reader to ignore the suite.
    """

    path = _REPOSITORY_ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is not part of this checkout; it is private-repo-owned")
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", _INSTRUCTION_FILES)
def test_no_instruction_file_imports_the_design_principles(name: str) -> None:
    """An unconditional import is paid for by every session, including the ones
    where no design decision came up.

    The rule is about the import and not about the file: both instruction files
    are supposed to name `docs/design_principles.md` in prose and tell a reader
    to open it when a decision turns on it.
    """

    imports = [
        line
        for line in _read(name).splitlines()
        if line.strip().startswith("@") and "design_principles" in line
    ]

    assert imports == [], (
        f"{name} imports docs/design_principles.md. That is unconditional, so it costs "
        "roughly 1400 tokens in every session whatever the task is, and CLAUDE.md argues "
        "against it at length. If the decision has genuinely changed, change it in both "
        "files and rewrite that argument rather than leaving them disagreeing."
    )


@pytest.mark.parametrize("name", _INSTRUCTION_FILES)
def test_instruction_files_name_the_doctrine_version_that_exists(name: str) -> None:
    """A versioned contract named by the wrong version is worse than unnamed.

    The whole point of the sentence these files carry is that a dispatched agent
    receives one bounded, versioned contract. Naming a version the module does
    not define tells a reader to go looking for something that is not there.
    """

    named = set(re.findall(r"engineering_doctrine\.v\d+", _read(name)))
    current = CURRENT_ENGINEERING_DOCTRINE.schema_version

    assert named, f"{name} no longer names the engineering doctrine contract at all"
    assert named == {current}, (
        f"{name} names {sorted(named)} but the module defines {current!r}. "
        "A version bump has to reach the instruction files, because they are what tells "
        "an agent which contract it is operating under."
    )


def test_both_instruction_files_point_at_the_principles_in_prose() -> None:
    """Removing the import must not have removed the pointer.

    The failure this guards is the plausible over-correction: someone reads the
    argument against the import, deletes the line, and leaves an agent with no
    idea the file exists.
    """

    for name in _INSTRUCTION_FILES:
        assert "docs/design_principles.md" in _read(name), (
            f"{name} no longer mentions docs/design_principles.md. It is not imported on "
            "purpose, which only works while something still tells the reader to open it."
        )
