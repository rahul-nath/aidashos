# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Git and worktree provenance for pow-wow code execution.

This module owns the representation of an allocated code worktree and every
operation that turns its mutable git state into durable checkpoint evidence.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from ..constants import DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS

type WorktreeCleanupPolicy = Literal["remove", "preserve"]


@dataclass(frozen=True)
class WorktreeAllocation:
    source_repo_path: str
    worktree_path: str
    head_sha: str
    branch_name: str
    cleanup_policy: WorktreeCleanupPolicy
    preserved: bool

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorktreeCommitCheckpoint:
    """A verified, branch-backed implementation checkpoint."""

    branch_name: str
    base_head_sha: str
    commit_sha: str | None
    commit_created: bool
    changed_from_base: bool
    checkpointed_files: tuple[str, ...]
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)


_AGENT_SCRATCH_PREFIXES = (
    ".codex-tmp/",
    ".claude-tmp/",
    ".qwen-tmp/",
    ".gemini-tmp/",
    ".goose-tmp/",
)
_EPHEMERAL_WORKTREE_PREFIXES = (
    *_AGENT_SCRATCH_PREFIXES,
    ".next/",
    ".turbo/",
    "build/",
    "coverage/",
    "dist/",
    "node_modules/",
)
_CODE_PATCH_MAX_BYTES = 2_000_000


def run_git_command_for_output(repo_path: Path, args: Sequence[str]) -> str:
    process = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        timeout=DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or process.stdout.strip())
    return process.stdout


def _git_command_succeeds(repo_path: Path, args: Sequence[str]) -> bool:
    process = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        timeout=DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS,
        check=False,
    )
    return process.returncode == 0


def _is_ephemeral_worktree_path(path: str) -> bool:
    return path.startswith(_EPHEMERAL_WORKTREE_PREFIXES)


def list_changed_worktree_files(
    worktree_path: Path,
    *,
    base_head_sha: str | None = None,
) -> tuple[str, ...]:
    status = run_git_command_for_output(worktree_path, ["status", "--porcelain=v1", "-uall"])
    files: list[str] = []
    for line in status.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if _is_ephemeral_worktree_path(path):
            continue
        files.append(path)
    if base_head_sha is not None:
        committed = run_git_command_for_output(
            worktree_path,
            ["diff", "--name-only", base_head_sha, "HEAD"],
        )
        for path in committed.splitlines():
            if path and not _is_ephemeral_worktree_path(path):
                files.append(path)
    return tuple(dict.fromkeys(files))


def summarize_worktree_diff(
    worktree_path: Path,
    *,
    base_head_sha: str | None = None,
) -> dict[str, Any]:
    status = run_git_command_for_output(worktree_path, ["status", "--short", "-uall"])
    diff_args = ["diff", "--stat"]
    if base_head_sha is not None:
        diff_args.append(base_head_sha)
    return {
        "status_short": status,
        "diff_stat": run_git_command_for_output(worktree_path, diff_args),
        "changed_files": list(
            list_changed_worktree_files(worktree_path, base_head_sha=base_head_sha)
        ),
    }


def build_worktree_code_patch(
    worktree_path: Path,
    *,
    group: str,
    head_sha: str,
    branch_name: str | None = None,
) -> dict[str, Any]:
    run_git_command_for_output(worktree_path, ["add", "-A", "--", "."])
    patch = run_git_command_for_output(worktree_path, ["diff", "--cached", "--binary", head_sha])
    raw = patch.encode("utf-8")
    truncated = len(raw) > _CODE_PATCH_MAX_BYTES
    if truncated:
        patch = raw[:_CODE_PATCH_MAX_BYTES].decode("utf-8", errors="ignore")
    return {
        "schema_version": "code_patch.v2",
        "worktree_group": group,
        "base_head_sha": head_sha,
        "branch_name": branch_name,
        "commit_sha": run_git_command_for_output(worktree_path, ["rev-parse", "HEAD"]).strip(),
        "patch": patch,
        "byte_size": len(raw),
        "truncated": truncated,
        "apply_hint": (
            "git -C <target_repo> apply --check <patch_file> && "
            "git -C <target_repo> apply <patch_file>"
        ),
    }


def commit_worktree_checkpoint(
    worktree: WorktreeAllocation,
    *,
    task_name: str,
) -> WorktreeCommitCheckpoint:
    """Commit verified work while preserving branch and ancestry invariants."""

    worktree_path = Path(worktree.worktree_path)
    try:
        active_branch = run_git_command_for_output(
            worktree_path, ["branch", "--show-current"]
        ).strip()
        if active_branch != worktree.branch_name:
            raise RuntimeError(
                "allocated branch changed "
                f"(expected {worktree.branch_name!r}, found {active_branch!r})"
            )
        if not _git_command_succeeds(
            worktree_path,
            ["merge-base", "--is-ancestor", worktree.head_sha, "HEAD"],
        ):
            raise RuntimeError("allocation base is not an ancestor of the worktree HEAD")

        # `git add -A` already skips untracked ignored build output. Naming an
        # ignored directory in a negative pathspec can still make Git reject
        # the whole checkpoint, which previously lost otherwise verified M2
        # work after `.next` and `node_modules` were produced.
        run_git_command_for_output(worktree_path, ["add", "-A", "--", "."])
        staged_files = tuple(
            line
            for line in run_git_command_for_output(
                worktree_path,
                ["diff", "--cached", "--name-only", "HEAD"],
            ).splitlines()
            if line
        )
        commit_created = False
        if staged_files:
            run_git_command_for_output(
                worktree_path,
                ["commit", "--no-verify", "-m", f"Agent checkpoint: {task_name}"],
            )
            commit_created = True

        head_sha = run_git_command_for_output(worktree_path, ["rev-parse", "HEAD"]).strip()
        changed_from_base = head_sha != worktree.head_sha
        checkpointed_files = tuple(
            line
            for line in run_git_command_for_output(
                worktree_path,
                ["diff", "--name-only", worktree.head_sha, "HEAD"],
            ).splitlines()
            if line
        )
        return WorktreeCommitCheckpoint(
            branch_name=worktree.branch_name,
            base_head_sha=worktree.head_sha,
            commit_sha=head_sha if changed_from_base else None,
            commit_created=commit_created,
            changed_from_base=changed_from_base,
            checkpointed_files=checkpointed_files,
        )
    except Exception as exc:  # noqa: BLE001 - checkpoint failure is durable task data
        return WorktreeCommitCheckpoint(
            branch_name=worktree.branch_name,
            base_head_sha=worktree.head_sha,
            commit_sha=None,
            commit_created=False,
            changed_from_base=False,
            checkpointed_files=(),
            error=f"{type(exc).__name__}: {exc}",
        )


__all__ = [
    "WorktreeAllocation",
    "WorktreeCleanupPolicy",
    "WorktreeCommitCheckpoint",
    "list_changed_worktree_files",
    "commit_worktree_checkpoint",
    "summarize_worktree_diff",
    "run_git_command_for_output",
    "build_worktree_code_patch",
]
