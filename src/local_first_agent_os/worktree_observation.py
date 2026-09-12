# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Direct observations of an allocated worktree, before executor cleanup."""

from __future__ import annotations

import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .constants import DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS
from .verification_git import verification_git_environment


@dataclass(frozen=True)
class WorktreeUsable:
    path: Path


@dataclass(frozen=True)
class WorktreeLost:
    path: Path


@dataclass(frozen=True)
class WorktreeUnverifiable:
    path: Path
    error: str


type WorktreeObservation = WorktreeUsable | WorktreeLost | WorktreeUnverifiable


def observe_worktree(path: Path) -> WorktreeObservation:
    """Only direct absence or a successful Git observation can prove loss.

    A failed Git invocation may mean a permission or tool failure, so its prose
    never grants an uncharged attempt. Probing a subdirectory is insufficient:
    Git can silently discover an unrelated repository above a replaced path.
    """

    try:
        if not stat.S_ISDIR(path.stat().st_mode):
            return WorktreeLost(path)
        (path / ".git").stat()
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS,
            env=verification_git_environment(),
            check=False,
        )
        if result.returncode:
            return WorktreeUnverifiable(path, f"Git probe exited {result.returncode}")
        if Path(result.stdout.strip()).resolve() != path.resolve():
            return WorktreeLost(path)
    except (FileNotFoundError, NotADirectoryError) as exc:
        # A missing executable is not evidence that the allocated directory died.
        if exc.filename in (str(path), str(path / ".git")):
            return WorktreeLost(path)
        return WorktreeUnverifiable(path, str(exc))
    except (OSError, subprocess.TimeoutExpired) as exc:
        return WorktreeUnverifiable(path, str(exc))
    return WorktreeUsable(path)
