# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""How a dispatched agent is told where to find the read-only ledger.

The executor owns which agent gets this; this module owns what "this" is, in
each harness's own spelling. Both are the same offer - run this command, speak
MCP to it, ask it three questions - and neither harness is privileged over the
other, which is the property that made the offer worth making at all.

Nothing here starts a server. A stdio MCP server is a child of whoever consumes
it, so what these functions produce is an argument telling the harness what to
spawn if it decides to ask something. A task that asks nothing spawns nothing.

See docs/completed/agent_ledger_read_access_design.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from .runtime_source import runtime_checkout

AGENT_LEDGER_SERVER_NAME: Final = "agent_ledger"
"""The name the agent sees. Tools arrive namespaced under it in both harnesses."""


def agent_ledger_server_command(
    coordination_root: Path,
    *,
    checkout: Path | None = None,
) -> tuple[str, ...]:
    """The argv that runs the read-only ledger server.

    Absolute, and rooted in the checkout the running code came from rather than
    in the process's working directory. The consumer of this command is an agent
    whose working directory is a worktree of some *other* repository, so a
    relative path would resolve against the wrong tree, and `uv run` there would
    resolve a different project's environment.

    ``--root`` is passed explicitly for the reason `coordination/ledger_selection`
    exists: a child that resolved its own ledger from ambient environment could
    answer from a different database than the run it is describing, and a wrong
    answer here is indistinguishable from a right one.
    """

    repository = (checkout or runtime_checkout()).resolve()
    return (
        "uv",
        "run",
        "--directory",
        str(repository),
        "python",
        str(repository / "agent_coordination_mcp.py"),
        "--root",
        str(Path(coordination_root).expanduser().resolve()),
        "serve",
        "--audience",
        "agent",
    )


def claude_mcp_args(coordination_root: Path, *, checkout: Path | None = None) -> list[str]:
    """`--mcp-config` as an inline JSON string, plus the flag that makes it exclusive.

    Inline rather than a written file so nothing lands in the agent's worktree:
    a config file there would show up in `changed_files` and be read as part of
    the work.

    `--strict-mcp-config` is the load-bearing half. Without it the agent also
    inherits the operator's user-scoped and project-scoped servers, so a
    dispatched agent's tool surface would depend on whatever a human happened to
    have configured that week. With it, this offer is the entire MCP surface.
    """

    command = agent_ledger_server_command(coordination_root, checkout=checkout)
    config = {
        "mcpServers": {
            AGENT_LEDGER_SERVER_NAME: {
                "command": command[0],
                "args": list(command[1:]),
            }
        }
    }
    return ["--mcp-config", json.dumps(config), "--strict-mcp-config"]


def codex_mcp_args(coordination_root: Path, *, checkout: Path | None = None) -> list[str]:
    """The same offer as `-c` overrides, which is how codex takes nested config.

    One `-c` per leaf because the flag parses its value as TOML, and this is the
    same mechanism the executor already uses for `model_reasoning_effort`.

    There is no codex equivalent of `--strict-mcp-config`: an operator's own
    `~/.codex/config.toml` servers are additive here and cannot be suppressed
    per invocation. That asymmetry is real and is the reason the offer is
    read-only rather than merely narrow - what a dispatched agent may do with
    this ledger cannot depend on a flag only one harness has.
    """

    command = agent_ledger_server_command(coordination_root, checkout=checkout)
    prefix = f"mcp_servers.{AGENT_LEDGER_SERVER_NAME}"
    return [
        "-c",
        f"{prefix}.command={_toml_string(command[0])}",
        "-c",
        f"{prefix}.args={_toml_string_array(command[1:])}",
    ]


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _toml_string_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


__all__ = [
    "AGENT_LEDGER_SERVER_NAME",
    "agent_ledger_server_command",
    "claude_mcp_args",
    "codex_mcp_args",
]
