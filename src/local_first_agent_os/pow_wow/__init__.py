# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Public pow-wow execution and ledger surface.

Every export here is resolved lazily, on first attribute access, from the leaf
module that actually defines it. This package once did ``from .executor import *``,
which made importing any leaf module - even ``protocol`` for one enum - execute
the full executor and everything it imports. That is how ``spawn_authority``
ended up in an import cycle that only fired in processes which imported it
before ``pow_wow``: ``pytest tests/test_staffing.py`` alone could not collect
while the full suite passed. The lazy surface keeps the public names importable
from the package while leaving ``executor`` out of ``sys.modules`` until
someone asks for a name that lives there.

``tests/test_pow_wow_import_graph.py`` pins this: each leaf module must import
in a fresh process without pulling ``executor``.
"""

# Package indexes intentionally re-export their public child-module surfaces.
# ruff: noqa: F401

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from .dry_run import DryRunPowWowExecutor
    from .executor import CliPowWowExecutor, FakeProcessPowWowExecutor
    from .git_ops import (
        WorktreeAllocation,
        WorktreeCleanupPolicy,
        WorktreeCommitCheckpoint,
        build_worktree_code_patch,
        list_changed_worktree_files,
    )
    from .ledger import (
        describe_coordination_ledger,
        persist_pow_wow_run_result,
        resolve_coordination_events_path,
        run_coordination_command,
        run_typed_coordination_command,
        serialize_coordination_content_to_json,
    )
    from .prompts import build_agent_task_prompt
    from .types import (
        CommandRunCapture,
        CoordinationCommandFn,
        DelegateFn,
        DispatchKind,
        ExecutionAttemptLease,
        ExecutionLeaseStatus,
        PowWowArtifact,
        PowWowExecutionContext,
        PowWowExecutor,
        PowWowRunResult,
        PowWowRunStatus,
        PowWowTaskResult,
        PowWowTaskSpec,
        PowWowTaskStatus,
        build_default_saga_tasks,
    )

# Each name maps to the leaf module that defines it, not to a module that
# happens to re-export it, so asking for a cheap name never loads a heavy
# module. Only the two executor classes cost an executor import.
_EXPORT_HOMES: Final[dict[str, str]] = {
    "CliPowWowExecutor": "executor",
    "CommandRunCapture": "types",
    "CoordinationCommandFn": "types",
    "DelegateFn": "types",
    "DispatchKind": "types",
    "DryRunPowWowExecutor": "dry_run",
    "ExecutionAttemptLease": "types",
    "ExecutionLeaseStatus": "types",
    "FakeProcessPowWowExecutor": "executor",
    "PowWowArtifact": "types",
    "PowWowExecutionContext": "types",
    "PowWowExecutor": "types",
    "PowWowRunResult": "types",
    "PowWowRunStatus": "types",
    "PowWowTaskResult": "types",
    "PowWowTaskSpec": "types",
    "PowWowTaskStatus": "types",
    "WorktreeAllocation": "git_ops",
    "WorktreeCleanupPolicy": "git_ops",
    "WorktreeCommitCheckpoint": "git_ops",
    "build_agent_task_prompt": "prompts",
    "build_default_saga_tasks": "types",
    "build_worktree_code_patch": "git_ops",
    "describe_coordination_ledger": "ledger",
    "list_changed_worktree_files": "git_ops",
    "persist_pow_wow_run_result": "ledger",
    "resolve_coordination_events_path": "ledger",
    "run_coordination_command": "ledger",
    "run_typed_coordination_command": "ledger",
    "serialize_coordination_content_to_json": "ledger",
}

# A literal rather than `sorted(_EXPORT_HOMES)` because pyright resolves
# re-exports from `__all__` only when it can evaluate it statically. The test
# suite pins that this list and the map hold exactly the same names.
__all__ = [
    "CliPowWowExecutor",
    "CommandRunCapture",
    "CoordinationCommandFn",
    "DelegateFn",
    "DispatchKind",
    "DryRunPowWowExecutor",
    "ExecutionAttemptLease",
    "ExecutionLeaseStatus",
    "FakeProcessPowWowExecutor",
    "PowWowArtifact",
    "PowWowExecutionContext",
    "PowWowExecutor",
    "PowWowRunResult",
    "PowWowRunStatus",
    "PowWowTaskResult",
    "PowWowTaskSpec",
    "PowWowTaskStatus",
    "WorktreeAllocation",
    "WorktreeCleanupPolicy",
    "WorktreeCommitCheckpoint",
    "build_agent_task_prompt",
    "build_default_saga_tasks",
    "build_worktree_code_patch",
    "describe_coordination_ledger",
    "list_changed_worktree_files",
    "persist_pow_wow_run_result",
    "resolve_coordination_events_path",
    "run_coordination_command",
    "run_typed_coordination_command",
    "serialize_coordination_content_to_json",
]


def __getattr__(name: str) -> Any:
    home = _EXPORT_HOMES.get(name)
    if home is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{home}", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
