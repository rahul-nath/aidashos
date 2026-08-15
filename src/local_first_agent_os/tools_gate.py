# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Gated tool layer — how a dispatched agent takes a real-world action.

An agent that needs to hit a Shelly plug (or any side-effecting endpoint) calls
a *tool*. Tools are gated by an explicit allow-list so a keyword-triggered agent
can't flip physical switches unless the operator pre-authorized that exact tool
(in workspace_policies). This mirrors the coordination ledger's
request_tool_permission / approval primitives at the execution edge.

Design notes:
- Representable/valid: `ToolResult` distinguishes three outcomes that must not
  be conflated — DENIED (not allow-listed), ran+OK, ran+ERROR. A caller can't
  mistake "blocked by policy" for "ran and failed".
- Keep-your-secrets: `ToolGate` does not know where the allow-list comes from
  (workspace_policies, a saga grant, ...); it's handed the resolved set.
- Side effects are injectable (`http_get`) so tests never touch the network and
  a dry-run mode is trivial.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

HttpGet = Callable[[str], Any]


@dataclass(frozen=True)
class ToolResult:
    tool: str
    allowed: bool
    ran: bool = False
    ok: bool = False
    output: Any = None
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "allowed": self.allowed,
            "ran": self.ran,
            "ok": self.ok,
            "output": self.output,
            "error": self.error,
        }


def _default_http_get(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 - operator-authorized LAN call
        body = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"status_code": resp.status, "body": body[:500]}


def shelly_plug(
    *,
    base_url: str,
    action: str = "off",
    channel: int = 0,
    http_get: HttpGet | None = None,
) -> dict[str, Any]:
    """Toggle a Shelly Gen4 plug over the LAN (RPC endpoint).

    base_url e.g. 'http://192.168.1.50'; action in {'on','off'}."""
    if action not in {"on", "off"}:
        raise ValueError(f"shelly_plug action must be 'on' or 'off', got {action!r}")
    on = "true" if action == "on" else "false"
    url = f"{base_url.rstrip('/')}/rpc/Switch.Set?id={int(channel)}&on={on}"
    getter = http_get or _default_http_get
    return {"url": url, "action": action, "response": getter(url)}


# Built-in tools. Add more here; each is gated by the allow-list, not by presence.
DEFAULT_TOOLS: dict[str, Callable[..., Any]] = {
    "shelly_plug": shelly_plug,
}


@dataclass
class ToolGate:
    """Runs a named tool ONLY if it is in the operator's allow-list."""

    allowed: set[str]
    tools: dict[str, Callable[..., Any]] = field(default_factory=lambda: dict(DEFAULT_TOOLS))

    @classmethod
    def from_allowlist(cls, allowed: Iterable[str]) -> ToolGate:
        return cls(allowed=set(allowed))

    def execute_allowed_tool(
        self,
        tool: str,
        args: Mapping[str, Any],
        *,
        requested_by: str = "",
    ) -> ToolResult:
        if tool not in self.allowed:
            return ToolResult(
                tool=tool,
                allowed=False,
                error=f"tool {tool!r} is not in the operator allow-list; "
                "pre-authorize it in workspace_policies to enable.",
            )
        fn = self.tools.get(tool)
        if fn is None:
            return ToolResult(tool=tool, allowed=True, error=f"unknown tool {tool!r}")
        try:
            return ToolResult(tool=tool, allowed=True, ran=True, ok=True, output=fn(**args))
        except Exception as exc:  # noqa: BLE001 - report tool failure, don't crash the caller
            return ToolResult(
                tool=tool, allowed=True, ran=True, ok=False, error=f"{type(exc).__name__}: {exc}"
            )
