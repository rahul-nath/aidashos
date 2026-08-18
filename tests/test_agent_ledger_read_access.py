# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What a dispatched agent may ask the ledger, and how it is told where to ask.

The property throughout is that this offer can only add knowledge. It never
lets an agent write to the record that judges it, and turning it off returns
the spawn to what it was before the feature existed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from local_first_agent_os.agent_ledger_mcp import (
    AGENT_LEDGER_SERVER_NAME,
    agent_ledger_server_command,
    claude_mcp_args,
    codex_mcp_args,
)
from local_first_agent_os.coordination.cli import (
    AGENT_READABLE_TOOLS,
    build_agent_read_mcp_server,
    build_mcp_server,
)
from local_first_agent_os.coordination.contracts import CoordinationCommandName

# Resolved up front because the builders resolve: on macOS /tmp is a symlink to
# /private/tmp, and a test comparing against the unresolved spelling would be
# asserting that the path is *not* canonicalised, which is the opposite of what
# these functions promise a child process that starts elsewhere.
_ROOT = Path("/tmp/example-coordination-root").resolve()
_CHECKOUT = Path("/tmp/checkout").resolve()


def test_the_agent_server_exposes_exactly_the_declared_read_set() -> None:
    """The tool list is the safety property, so it is asserted as a set equality.

    Not "contains no writes", which would pass for a server that also exposed
    something harmless nobody meant to publish. A new tool reaching agents has
    to be a deliberate edit to `AGENT_READABLE_TOOLS`, and this fails until it
    is.
    """

    tools = asyncio.run(build_agent_read_mcp_server().list_tools())

    assert sorted(tool.name for tool in tools) == sorted(AGENT_READABLE_TOOLS)


def test_no_verb_an_agent_can_reach_writes_to_the_ledger() -> None:
    """Stated against the vocabulary rather than against three known names.

    An agent that could write could file evidence about its own run, and both
    the verification gate and the cross-provider review rest on an agent's
    account of itself not being evidence.
    """

    mutating = (
        "submit",
        "create",
        "claim",
        "release",
        "resolve",
        "approve",
        "deny",
        "grant",
        "revoke",
        "complete",
        "cancel",
        "start",
        "run",
        "delete",
        "amend",
        "fail",
        "retry",
        "evaluate",
        "decide",
        "open",
        "append",
        "attach",
        "heartbeat",
        "register",
        "supersede",
        "reconcile",
    )
    for name in AGENT_READABLE_TOOLS:
        assert not name.startswith(mutating), f"{name} reads like a write"
        assert name.startswith(("read_", "list_", "describe_", "get_"))


def test_the_agent_surface_is_a_fraction_of_the_operator_surface() -> None:
    """Built up from a tuple rather than filtered down from the full server.

    A filter would make the guarantee depend on the removal staying complete as
    tools are added; this way a new operator tool is absent from the agent
    surface until somebody names it.
    """

    operator = asyncio.run(build_mcp_server().list_tools())
    agent = asyncio.run(build_agent_read_mcp_server().list_tools())

    operator_names = {tool.name for tool in operator}
    agent_names = {tool.name for tool in agent}
    assert agent_names < operator_names
    assert len(agent_names) < len(operator_names) / 10


def test_migrating_the_schema_is_not_offered_to_any_model() -> None:
    """A CLI verb on purpose, and a tool on neither server.

    On 2026-08-17 a dispatched agent migrated the shared production ledger from
    an isolated worktree, twice in one day, and both times it did so by reading.
    Removing the implicit path and then handing the explicit one to a model would
    give back exactly what was taken away, with a nicer name on it.

    The absence is asserted rather than left to the registration list, because
    `build_mcp_server` names its tools one by one and a later reader completing
    the set would look like tidying rather than a policy change.
    """

    operator = asyncio.run(build_mcp_server().list_tools())
    agent = asyncio.run(build_agent_read_mcp_server().list_tools())
    verb = CoordinationCommandName.MIGRATE_COORDINATION_SCHEMA.value

    assert verb not in {tool.name for tool in operator}
    assert verb not in {tool.name for tool in agent}
    assert verb not in AGENT_READABLE_TOOLS


def test_the_claim_verbs_are_not_offered() -> None:
    """The standing direction is not to wire claims, and this pins it.

    The file set is unknowable at dispatch time, `claims.path` is a global
    primary key so two projects sharing `src/main.py` would block each other,
    and the integration queue that would have consumed claims instead reads
    `changed_files` from git after a run.
    """

    for verb in ("claim_files", "release_files", "assert_claimed", "list_claims"):
        assert verb not in AGENT_READABLE_TOOLS


def test_the_server_command_is_absolute_and_names_its_ledger() -> None:
    """The consumer's working directory is another repository's worktree.

    A relative path would resolve against that tree, and `uv run` there would
    resolve a different project's environment. `--root` is explicit for the
    reason ledger_selection exists: a child resolving its own ledger could
    answer from a different database than the run it is describing.
    """

    command = agent_ledger_server_command(_ROOT, checkout=_CHECKOUT)

    assert command[0] == "uv"
    assert "--directory" in command
    assert command[command.index("--directory") + 1] == str(_CHECKOUT)
    assert command[command.index("--root") + 1] == str(_ROOT)
    assert command[-2:] == ("--audience", "agent")
    for part in command[1:]:
        assert not part.startswith("./") and not part.startswith("../")


def test_claude_is_told_this_is_the_only_mcp_surface() -> None:
    """`--strict-mcp-config` is the load-bearing half of the claude offer.

    Without it the agent also inherits the operator's user-scoped and
    project-scoped servers, so a dispatched agent's tool surface would depend on
    whatever a human had configured that week.
    """

    args = claude_mcp_args(_ROOT, checkout=_CHECKOUT)

    assert "--strict-mcp-config" in args
    config = json.loads(args[args.index("--mcp-config") + 1])
    assert list(config["mcpServers"]) == [AGENT_LEDGER_SERVER_NAME]
    entry = config["mcpServers"][AGENT_LEDGER_SERVER_NAME]
    assert entry["command"] == "uv"
    assert "--audience" in entry["args"]


def test_codex_gets_the_same_offer_in_its_own_spelling() -> None:
    """Both harnesses take MCP config per invocation, which is why both get it.

    An earlier draft asserted this was Claude-only and would have quietly ended
    the harness-neutrality this repository claims. `codex mcp` manages servers
    and `-c` overrides nested config, the same mechanism already used for
    `model_reasoning_effort`.
    """

    args = codex_mcp_args(_ROOT, checkout=_CHECKOUT)

    assert args.count("-c") == 2
    overrides = dict(
        pair.split("=", 1)
        for pair in args[1::2]  # every value after a -c
    )
    assert overrides[f"mcp_servers.{AGENT_LEDGER_SERVER_NAME}.command"] == '"uv"'
    parsed_args = json.loads(overrides[f"mcp_servers.{AGENT_LEDGER_SERVER_NAME}.args"])
    assert "--audience" in parsed_args and "agent" in parsed_args


def test_both_harnesses_are_pointed_at_one_server() -> None:
    """The same offer, so which harness ran a task cannot change what it knew."""

    claude_config = json.loads(
        claude_mcp_args(_ROOT, checkout=_CHECKOUT)[1],
    )["mcpServers"][AGENT_LEDGER_SERVER_NAME]
    codex = codex_mcp_args(_ROOT, checkout=_CHECKOUT)
    codex_args = json.loads(codex[codex.index("-c", 2) + 1].split("=", 1)[1])

    assert [claude_config["command"], *claude_config["args"]] == [
        json.loads(codex[1].split("=", 1)[1]),
        *codex_args,
    ]


@pytest.mark.parametrize("harness_bin", ["claude", "codex"])
def test_an_executor_without_a_ledger_root_offers_nothing(tmp_path: Path, harness_bin: str) -> None:
    """Off is the pre-feature spawn exactly, not a narrower configuration.

    The operator turning this off should get the argv that shipped before it
    existed, so no MCP flag of any spelling may survive.
    """

    from local_first_agent_os.pow_wow.executor import CliPowWowExecutor
    from local_first_agent_os.spawn_authority import ReadOnlyInspection
    from local_first_agent_os.staffing import FrontierHarness

    executor = CliPowWowExecutor(worktree_root=tmp_path, agent_ledger_root=None)
    harness = FrontierHarness.CLAUDE if harness_bin == "claude" else FrontierHarness.CODEX

    command = executor._build_agent_cli_command(harness, None, "prompt", ReadOnlyInspection())

    assert "--mcp-config" not in command
    assert "--strict-mcp-config" not in command
    assert not any(part.startswith("mcp_servers.") for part in command)


def test_the_offer_never_displaces_the_prompt(tmp_path: Path) -> None:
    """The prompt is positional and must stay last under every harness.

    `--mcp-config` is variadic on claude, so an offer appended after the prompt
    would swallow it. This is the same hazard `--disallowedTools` already has.
    """

    from local_first_agent_os.pow_wow.executor import CliPowWowExecutor
    from local_first_agent_os.spawn_authority import (
        ReadOnlyInspection,
        SupervisedCommands,
        UnattendedImplementation,
    )
    from local_first_agent_os.staffing import FrontierHarness

    executor = CliPowWowExecutor(worktree_root=tmp_path, agent_ledger_root=_ROOT)

    for harness in FrontierHarness:
        for posture in (ReadOnlyInspection(), SupervisedCommands(), UnattendedImplementation()):
            command = executor._build_agent_cli_command(harness, None, "THE PROMPT", posture)
            assert command[-1] == "THE PROMPT"
            assert any("agent_coordination_mcp.py" in part for part in command)


def test_the_doctrine_tells_the_agent_the_surface_exists() -> None:
    """An offer nobody is told about is another built-and-unconsulted mechanism.

    The startup skill already says execution history is a query rather than an
    inference; until this, a dispatched agent had no way to run that query. The
    contract now names the surface and says its absence is not an error, because
    the operator may have it switched off.
    """

    from local_first_agent_os.engineering_doctrine import CURRENT_ENGINEERING_DOCTRINE

    text = CURRENT_ENGINEERING_DOCTRINE.text
    assert "agent_ledger" in text
    assert "read-only" in text
    assert "absence is not an error" in text


def test_a_configured_executor_offers_the_ledger_to_both_harnesses(tmp_path: Path) -> None:
    """Harness neutrality is the property that made this worth doing.

    Both CLIs take MCP configuration per invocation, so which harness a tier
    happens to be staffed with cannot decide whether its agent can establish
    what already ran.
    """

    from local_first_agent_os.pow_wow.executor import CliPowWowExecutor
    from local_first_agent_os.spawn_authority import UnattendedImplementation
    from local_first_agent_os.staffing import FrontierHarness

    executor = CliPowWowExecutor(worktree_root=tmp_path, agent_ledger_root=_ROOT)

    claude = executor._build_agent_cli_command(
        FrontierHarness.CLAUDE, None, "p", UnattendedImplementation()
    )
    codex = executor._build_agent_cli_command(
        FrontierHarness.CODEX, None, "p", UnattendedImplementation()
    )

    assert "--mcp-config" in claude and "--strict-mcp-config" in claude
    assert any(part.startswith("mcp_servers.agent_ledger.") for part in codex)
    assert str(_ROOT) in " ".join(claude)
    assert str(_ROOT) in " ".join(codex)
