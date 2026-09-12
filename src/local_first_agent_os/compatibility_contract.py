# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Conservative compatibility checks against immutable previous-client contracts.

Existing shapes must remain equal; new named commands, tools and HTTP operations
may be added. More permissive schema subtyping requires an explicit policy change,
not a heuristic that can silently approve a breaking release.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping
from typing import Any

from .coordination.cli import build_mcp_server, build_parser


def _without_documentation(value: Any) -> Any:
    if isinstance(value, list):
        return [_without_documentation(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _without_documentation(item)
            for key, item in value.items()
            if not (
                key in {"title", "description", "summary", "examples"} and isinstance(item, str)
            )
        }
    return value


def _cli_contract(parser: argparse.ArgumentParser) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                result[f"command:{name}"] = _cli_contract(child)
        else:
            # Session defaults are deliberately resolved per invocation from the
            # environment. Compatibility records their grammar, not an operator ID.
            result[f"argument:{action.dest}"] = {
                "options": action.option_strings,
                "nargs": action.nargs,
                "required": action.required,
                "choices": list(action.choices) if action.choices is not None else None,
                "type": getattr(action.type, "__name__", None),
                "action": type(action).__name__,
            }
    return result


def capture_contract(openapi: Mapping[str, Any]) -> dict[str, Any]:
    tools = asyncio.run(build_mcp_server().list_tools())
    return {
        "schema_version": "public_contract_snapshot.v1",
        "cli": _cli_contract(build_parser()),
        "mcp": {
            tool.name: _without_documentation(
                {
                    "inputSchema": tool.inputSchema,
                    "outputSchema": tool.outputSchema,
                }
            )
            for tool in tools
        },
        "http": {
            f"{method.upper()} {path}": _without_documentation(operation)
            for path, operations in openapi["paths"].items()
            for method, operation in operations.items()
        },
        "http_models": _without_documentation(openapi["components"]["schemas"]),
    }


def compatibility_changes(
    previous: Mapping[str, Any], candidate: Mapping[str, Any]
) -> tuple[str, ...]:
    if previous.get("schema_version") != "public_contract_snapshot.v1":
        raise ValueError("unknown previous contract snapshot version")
    if candidate.get("schema_version") != previous["schema_version"]:
        raise ValueError("unknown candidate contract snapshot version")
    changes: list[str] = []
    for surface in ("cli", "mcp", "http", "http_models"):
        if surface not in previous or surface not in candidate:
            raise ValueError(f"contract snapshot missing {surface}")
        for name, old in previous[surface].items():
            current = candidate[surface].get(name)
            if current is None:
                changes.append(f"{surface}:{name}: removed")
            elif json.dumps(old, sort_keys=True) != json.dumps(current, sort_keys=True):
                changes.append(f"{surface}:{name}: changed")
    return tuple(changes)
