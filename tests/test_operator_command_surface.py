# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The ledger CLI has two spellings, and this pins them to one entry point.

`agent-ledger` was added because the operator docs and the demo script typed
`uv run python agent_coordination_mcp.py --root . <verb>`, which is 40 characters
of prologue before the verb and does not fit on a screen being recorded.

The reason it is a `[project.scripts]` entry rather than a shell alias is the
subject of the first test. An alias would be a second surface: a name with its
own resolution rules, invisible to `uv run`, that docs would then have to choose
between. A console script that names the same `main` the compatibility shell
imports is not a second surface, because there is no second implementation for
the two names to drift apart into. That is a property, so it gets an assertion.

The other two tests exist because the docs now type the short form bare. A
command in a runbook is a claim that it runs, and the cheapest place to catch a
verb that was renamed out from under a document is here rather than on camera.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import io
import re
import shlex
import tomllib
from pathlib import Path
from types import ModuleType

import pytest

from local_first_agent_os.coordination.cli import build_parser

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_COMPAT_SHELL = _REPOSITORY_ROOT / "agent_coordination_mcp.py"

# The docs an operator copy-pastes from. Handoffs are excluded on purpose: they
# are dated records of what was run on a particular day, and a test that forced
# them to keep parsing against today's grammar would be asking history to stay
# current.
# Only the ones present in this checkout. The public snapshot carries this suite
# and only some of these documents, and a document is not dragged into the
# snapshot to keep a test green: the guard runs on whatever is here.
_CANDIDATE_OPERATOR_DOCS = (
    "docs/demo_shooting_script.md",
    "docs/cockpit_e2e_runbook.md",
    "docs/work_unit_operator_walkthrough.md",
)

_OPERATOR_DOCS = tuple(
    relative
    for relative in _CANDIDATE_OPERATOR_DOCS
    if (_REPOSITORY_ROOT / relative).exists()
)

# Only fenced bash blocks. Prose that names the command in backticks is
# explanation, not something anyone pastes, and scanning it made this suite fail
# on a sentence about where `agent-ledger` comes from.
_BASH_BLOCK = re.compile(r"^```bash\n(.*?)^```", re.MULTILINE | re.DOTALL)


def _console_scripts() -> dict[str, str]:
    with (_REPOSITORY_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["scripts"]


def _load_compat_shell() -> ModuleType:
    """Import the root-level shell by path.

    It is a loose script beside the package rather than a module inside it, so
    the installed distribution does not put it on `sys.path` and a plain import
    of it finds nothing.
    """

    spec = importlib.util.spec_from_file_location("agent_coordination_mcp", _COMPAT_SHELL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _argv_of(line: str) -> list[str] | None:
    """The `agent-ledger` invocation in one shell line, as argv.

    Handles the two wrappers the docs actually use: a `watch -n 2 '...'` quote
    around the whole command, and a trailing `| head -N`. Both are shell, not
    grammar, and the parser should not be asked to read them.
    """

    if "agent-ledger" not in line:
        return None

    command = line[line.index("agent-ledger") + len("agent-ledger") :]
    command = command.split("|")[0].rstrip()

    try:
        return shlex.split(command)
    except ValueError:
        # An unbalanced quote here is the `watch -n 2 '...'` wrapper's closing
        # one, left behind when the command had no pipe to split it off. Drop
        # one trailing quote and try again; a genuinely malformed line still
        # raises and is a doc bug worth failing on.
        return shlex.split(command.rstrip("'\""))


def _documented_commands() -> list[tuple[str, str]]:
    """Every `agent-ledger` line in the operator docs this tree actually carries.

    A missing document is skipped rather than fatal, because `tests/` and
    `docs/` do not travel together. The public snapshot's manifest is code-only:
    it carries the whole test suite and exactly four paths under `docs/`, none
    of them these three, so reading them unconditionally turned a deliberate
    publishing decision into a collection error that took the entire suite down
    with it. Here every document is present and the coverage is unchanged; there
    the list is empty and the parametrized test below skips.
    """

    found: list[tuple[str, str]] = []
    for relative in _OPERATOR_DOCS:
        document = _REPOSITORY_ROOT / relative
        if not document.is_file():
            continue
        for block in _BASH_BLOCK.finditer(document.read_text(encoding="utf-8")):
            for line in block.group(1).splitlines():
                if "agent-ledger" in line:
                    found.append((relative, line.strip()))
    return found


def test_the_console_script_and_the_compat_shell_are_one_entry_point() -> None:
    """`agent-ledger` and `agent_coordination_mcp.py` must resolve to one object.

    Not "must behave the same", which a test can only sample. The same function
    object cannot disagree with itself, and that is the whole argument for
    preferring a console script over an alias here.
    """

    target = _console_scripts()["agent-ledger"]
    module_path, _, attribute = target.partition(":")
    entry_point = getattr(importlib.import_module(module_path), attribute)

    shell = _load_compat_shell()

    assert shell.main is entry_point, (
        f"`agent-ledger` points at {target}, but agent_coordination_mcp.py imports "
        "a different callable. These are supposed to be two names for one command."
    )


def test_the_compat_shell_stays_a_shell() -> None:
    """It may import and exit, and it may not grow behaviour of its own.

    The moment this file does anything the console script does not, the two
    names stop being interchangeable and every doc that picked one is wrong
    about the other.
    """

    source = _COMPAT_SHELL.read_text(encoding="utf-8")
    body = [
        line
        for line in source.splitlines()
        if line.strip() and not line.strip().startswith("#") and '"""' not in line
    ]

    assert len(body) <= 4, (
        "agent_coordination_mcp.py has grown past an import and a SystemExit. "
        f"It is documented as a compatibility shell for the same `main` that "
        f"`agent-ledger` names; found {len(body)} statements:\n" + "\n".join(body)
    )


@pytest.mark.parametrize(("document", "line"), _documented_commands())
def test_every_documented_ledger_command_parses(document: str, line: str) -> None:
    """A runbook command is a claim that it runs; the parser is what adjudicates.

    Placeholders like `<work_unit_id>` parse as ordinary positional strings, so
    this checks the grammar rather than the values: that the verb exists, that
    its required positionals are present, and that no flag was renamed out from
    under a document.
    """

    parser = build_parser()
    argv = _argv_of(line)
    assert argv is not None

    with contextlib.redirect_stderr(io.StringIO()) as captured:
        try:
            parser.parse_args(argv)
        except SystemExit as exit_signal:  # argparse's failure channel
            pytest.fail(
                f"{document} documents `{line}`, which the ledger CLI refuses to "
                f"parse (exit {exit_signal.code}):\n{captured.getvalue()}"
            )


def test_the_documented_commands_are_actually_found() -> None:
    """A guard on the regex, not on the docs.

    Every assertion above is parametrized over what the scan returns, so a
    pattern that quietly matched nothing would turn this file into a suite that
    passes by testing zero commands.
    """

    found = _documented_commands()
    assert len(found) >= 15, f"expected the operator docs to carry commands, scanned {len(found)}"
    assert {document for document, _ in found} == set(_OPERATOR_DOCS)


@pytest.mark.parametrize("relative", _OPERATOR_DOCS)
def test_operator_docs_do_not_pass_the_redundant_root_flag(relative: str) -> None:
    """`--root .` is the parser's own default, spelled out.

    `build_parser` documents `--root` as "repo root; otherwise auto-detect .git
    or cwd", so passing `.` from the repo root asks for what would happen
    anyway. It was nine characters in front of the verb in 35 documented
    commands, which is why it is worth a tripwire rather than one cleanup.
    """

    text = (_REPOSITORY_ROOT / relative).read_text(encoding="utf-8")

    assert "--root ." not in text, (
        f"{relative} passes `--root .`, which is what the CLI already does when "
        "the flag is omitted. Drop it rather than documenting the default."
    )


def test_root_is_optional_and_self_detecting() -> None:
    """The premise of the test above, asserted rather than assumed.

    If `--root` ever becomes required, the tripwire above stops being a
    style rule and starts being wrong, and this fails first to say so.
    """

    action = next(a for a in build_parser()._actions if a.dest == "root")

    assert not action.required
    assert action.default is None
    assert action.help is not None and "auto-detect" in action.help
