# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Which source tree this process is running, and at what revision.

Three places used to answer this question with three copies of the same
subprocess call, each deriving the repository root by counting `parents[...]`
from its own depth in the tree. That is one design decision - "the running code
identifies itself by its checkout, with an environment override" - recorded in
three places that have to be edited together, and they had already drifted: one
raised on a failed `git`, another returned None.

The answer is load-bearing. `pi-daemon` publishes it on `/health`, and
`start-agent-runtime.sh` compares it against a checkout's HEAD to decide whether
a resident daemon is serving stale code. A disagreement between two of these
copies reads as staleness that is not there.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .constants import DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS

# `src/local_first_agent_os/runtime_source.py` -> `src/local_first_agent_os` ->
# `src` -> the checkout. The package is installed editable, so this is the
# working tree rather than a copy under `site-packages`.
_RUNTIME_CHECKOUT = Path(__file__).resolve().parents[2]

RUNTIME_REVISION_ENV_VAR = "LOCAL_AGENT_RUNTIME_REVISION"


@dataclass(frozen=True)
class RuntimeSource:
    """The working tree a running process loaded its code from."""

    checkout: Path
    revision: str | None


def runtime_checkout() -> Path:
    """The working tree the currently-executing code was loaded from.

    This is a property of the code, not of the working directory: a process
    started from one git worktree while the operator stands in another still
    answers with the tree it is actually running.
    """

    return _RUNTIME_CHECKOUT


def revision_of(checkout: Path) -> str | None:
    """The HEAD of one working tree, or None when it cannot be read.

    Not being able to read it is a runtime failure - no git on PATH, a checkout
    that is not a repository - and the callers all have something useful to say
    about an unknown revision. None is that answer, rather than an exception
    every caller would have to catch to say the same thing.
    """

    try:
        completed = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip() or None


def runtime_revision() -> str | None:
    """The source revision of the running code.

    `start-agent-runtime.sh` exports the environment variable for the processes
    it starts, and that is the more specific answer because it names the
    revision the operator asked for. launchd does not inherit that environment,
    so deriving from the checkout keeps the answer meaningful for a supervised
    daemon and for a process started by hand.
    """

    # An exported-but-empty variable is absence, not a revision named "". A
    # shell that computes the value and gets nothing exports exactly that, and
    # reading it as an answer would report "no revision" for a checkout that has
    # one.
    if configured := os.environ.get(RUNTIME_REVISION_ENV_VAR, "").strip():
        return configured
    return revision_of(_RUNTIME_CHECKOUT)


def runtime_source() -> RuntimeSource:
    return RuntimeSource(checkout=runtime_checkout(), revision=runtime_revision())


__all__ = [
    "RUNTIME_REVISION_ENV_VAR",
    "RuntimeSource",
    "revision_of",
    "runtime_checkout",
    "runtime_revision",
    "runtime_source",
]
