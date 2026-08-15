# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Representable prompt-view blocks derived from durable source artifacts.

The ledger remains complete. These values describe only the bounded view sent
to a model, and every non-verbatim variant carries an explicit source label.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger(__name__)


class ViewBlock(Protocol):
    """A prompt block whose fidelity is explicit in its concrete variant."""

    source: str

    def render(self) -> str: ...


@dataclass(frozen=True)
class VerbatimViewBlock:
    source: str
    content: str

    def render(self) -> str:
        return self.content


@dataclass(frozen=True)
class CompactedViewBlock:
    source: str
    content: str
    original_char_count: int

    def render(self) -> str:
        return (
            f"[compacted view from {self.source}; original_chars="
            f"{self.original_char_count}]\n{self.content}"
        )


@dataclass(frozen=True)
class TruncatedViewBlock:
    source: str
    content: str
    original_char_count: int

    def render(self) -> str:
        omitted = max(0, self.original_char_count - len(self.content))
        return (
            f"[truncated view from {self.source}; original_chars="
            f"{self.original_char_count}; omitted_chars={omitted}]\n"
            f"{self.content}"
        )


PromptViewBlock = VerbatimViewBlock | CompactedViewBlock | TruncatedViewBlock


@dataclass(frozen=True)
class ViewCompactionRequest:
    """The whole of what a compactor is asked to shorten, plus the budget it must fit.

    A parameter object rather than three positional arguments because the seam
    crosses into injected code that this module cannot type-check the body of,
    and two of the three are strings: a caller that swapped ``source`` and
    ``content`` would produce a plausible-looking summary of a label.
    """

    source: str
    content: str
    char_limit: int


type ViewCompactor = Callable[[ViewCompactionRequest], str]
"""Shortens an over-budget view. Injected, so this module needs no model at import."""


def _compacted_view_or_none(
    compactor: ViewCompactor,
    request: ViewCompactionRequest,
) -> CompactedViewBlock | None:
    """A usable summary, or ``None`` meaning the caller should truncate instead.

    Every way a summariser can disappoint has to end here rather than upward,
    because the caller is building the prompt for a spawned agent and a missing
    summariser must not be able to fail that dispatch. A compactor reaches a
    model over a socket, so unavailable, unreachable, timed out, and malformed
    are ordinary weather, not programmer errors, and the honest degraded answer
    already exists: the truncation this codebase shipped before compaction did.

    ``Exception`` and not ``BaseException`` on purpose - a cancelled or
    interrupted run must still stop, and swallowing that would turn a shutdown
    into a hang.

    An empty summary is rejected because a compactor that returns nothing has
    dropped everything, which is strictly worse than keeping a verbatim prefix.
    An over-budget summary is rejected for the same reason it was asked for: the
    limit is the caller's contract with the model's context window, and a
    compactor that ignored it has given no evidence it would respect a second
    chance. Both fall back to the prefix, which is at least faithful as far as
    it goes.
    """

    try:
        summary = compactor(request).strip()
    except Exception:
        logger.warning(
            "Compaction of %s failed; falling back to truncation.",
            request.source,
            exc_info=True,
        )
        return None
    if not summary or len(summary) > request.char_limit:
        logger.warning(
            "Compaction of %s returned %d chars against a %d limit; falling back to truncation.",
            request.source,
            len(summary),
            request.char_limit,
        )
        return None
    return CompactedViewBlock(
        source=request.source,
        content=summary,
        original_char_count=len(request.content),
    )


def build_bounded_view_block(
    *,
    source: str,
    content: str,
    char_limit: int,
    compactor: ViewCompactor | None = None,
) -> PromptViewBlock:
    """Return a fidelity-labeled view without silently discarding content.

    Overflow has two answers and this is the only place that picks between them.
    Truncation drops whatever sits at the end, which on a dependency block means
    the last task's output vanishes while the first task's is verbatim; a
    summary keeps the shape of all of it. ``compactor`` unset means truncate,
    so a caller with no model available - a test importing this module, a
    dispatch running while the local server is down - gets the older behaviour
    by asking for nothing.
    """

    if char_limit <= 0:
        raise ValueError("char_limit must be positive")
    if len(content) <= char_limit:
        return VerbatimViewBlock(source=source, content=content)
    if compactor is not None:
        compacted = _compacted_view_or_none(
            compactor,
            ViewCompactionRequest(source=source, content=content, char_limit=char_limit),
        )
        if compacted is not None:
            return compacted
    return TruncatedViewBlock(
        source=source,
        content=content[:char_limit],
        original_char_count=len(content),
    )


__all__ = [
    "CompactedViewBlock",
    "PromptViewBlock",
    "TruncatedViewBlock",
    "VerbatimViewBlock",
    "ViewBlock",
    "ViewCompactionRequest",
    "ViewCompactor",
    "build_bounded_view_block",
]
