# SPDX-License-Identifier: AGPL-3.0-or-later
"""Failure frames owned by the sealed Codex inspection process, not its model."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Mapping, Sequence
from typing import Any

from .coordination.failures import FailureV1, expected_failure
from .coordination.outcomes import TerminalOutcome

_OPERATION = "codex_inspection"
_EXIT_OUTCOMES = {
    125: frozenset({TerminalOutcome.REVIEW_UNAVAILABLE, TerminalOutcome.UNKNOWN_FAILURE}),
    130: frozenset({TerminalOutcome.OPERATOR_CANCELED}),
}


def inspection_failure_event(outcome: TerminalOutcome, message: str) -> dict[str, Any]:
    if outcome not in set().union(*_EXIT_OUTCOMES.values()):
        raise ValueError("unsupported inspection failure outcome")
    failure = expected_failure(outcome, operation=_OPERATION, message=message)
    return {
        "type": "error",
        "failure_code": failure.error_code,
        "text": failure.message,
        "failure": failure.to_dict(),
    }


def inspection_process_failure(
    command: Sequence[str] | str, *, stdout: str, exit_code: int
) -> FailureV1 | None:
    """Accept only the prepared wrapper's terminal, internally consistent frame.

    Model text lives inside item.completed events; it cannot become a host
    refusal even if it quotes this entire frame. The exact command shape also
    excludes ordinary code-writing CLIs and arbitrary shell commands.
    """
    try:
        argv = shlex.split(command) if isinstance(command, str) else list(command)
    except ValueError:
        return None
    if (
        exit_code not in _EXIT_OUTCOMES
        or len(argv) != 7
        or argv[1:4] != ["-m", "local_first_agent_os.codex_review_launch", "--request"]
        or argv[5] != "--request-sha256"
        or not argv[0]
        or not argv[4]
        or re.fullmatch(r"[0-9a-f]{64}", argv[6]) is None
    ):
        return None
    frames: list[Mapping[str, Any]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            frame = json.loads(line)
        except (ValueError, RecursionError):
            return None
        if not isinstance(frame, Mapping):
            return None
        frames.append(frame)
    if not frames or any(frame.get("type") == "error" for frame in frames[:-1]):
        return None
    frame = frames[-1]
    if set(frame) != {"type", "failure_code", "text", "failure"} or frame["type"] != "error":
        return None
    try:
        outcome = TerminalOutcome(frame["failure_code"])
    except (TypeError, ValueError):
        return None
    message = frame["text"]
    if outcome not in _EXIT_OUTCOMES[exit_code] or not isinstance(message, str) or not message:
        return None
    failure = expected_failure(outcome, operation=_OPERATION, message=message)
    return failure if frame["failure"] == failure.to_dict() else None
