# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import re

TURN_USER = "<|user|>"
TURN_ASSISTANT = "<|assistant|>"
_ROLE_RE = re.compile(r"<\|(user|assistant)\|>")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    whitespace_tokens = len(text.split())
    char_estimate = (len(text) + 3) // 4
    return max(whitespace_tokens, char_estimate)


def parse_turns(context: str) -> list[tuple[str, str]]:
    segments = _ROLE_RE.split(context)
    turns = []
    i = 1
    while i < len(segments) - 1:
        turns.append((segments[i], segments[i + 1].strip()))
        i += 2
    return turns


def format_turns(turns: list[tuple[str, str]]) -> str:
    return "".join(f"<|{role}|>\n{content}\n" for role, content in turns)
