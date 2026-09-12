# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Permanent refusal contract for retired unmanaged frontier-query entrypoints."""

from __future__ import annotations

from enum import StrEnum

from .contracts import DirectiveHelp

RETIRED_AGENT_QUERY_ALIASES = frozenset({"/claude", "/cc", "/codex"})


class AgentQueryRetirement(StrEnum):
    RETIRED = "AGENT_QUERY_RETIRED"

    @property
    def message(self) -> str:
        return (
            "Direct /claude, /cc, and /codex queries are retired in AiDashOS. "
            "Use the installed provider CLI directly outside AiDashOS, or use an "
            "approved WorkUnit for system-owned work."
        )

    def help(self) -> DirectiveHelp:
        return DirectiveHelp(
            summary=self.message,
            suggestions=[
                "Run claude or codex directly in your terminal outside AiDashOS.",
                "Use the managed document, approval, and WorkUnit flow for AiDashOS-owned work.",
            ],
            canonical_examples=[],
        )

    def help_payload(self) -> dict[str, object]:
        return {**self.help().as_dict(), "error_code": self.value}


def retired_agent_query_alias(raw: str) -> str | None:
    """Recognize the retired name without interpreting a former provider payload."""
    parts = raw.split(maxsplit=1)
    return parts[0] if parts and parts[0] in RETIRED_AGENT_QUERY_ALIASES else None
