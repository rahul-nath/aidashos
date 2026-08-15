# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A target repository and a linked-project registry an approved commit can exist in.

Enqueue refuses a `CODE_MERGE` approval whose commit the target repository does
not contain, so every test that resolves one now needs a repository with that
commit in it. That is more setup than any single test wants to own, and it is the
same setup for all of them, so it lives here rather than being copied to the next
file that resolves an approval.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from local_first_agent_os.refinery.requests import IntegrationRequestId, IntegrationSubject

AGENT_BRANCH = "agent/refinery-test"


@dataclass(frozen=True)
class TargetRepository:
    """One target project's git state, with the three shas the refusals need.

    `unrelated_sha` is an orphan commit: a real object in this repository that no
    ancestry walk from `base_sha` can reach. That is the shape of a branch
    someone rebuilt on a different base after the approval was written, and it is
    the only way to exercise the descent check without deleting anything.
    """

    path: Path
    base_sha: str
    commit_sha: str
    unrelated_sha: str


def git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def build_target_repository(path: Path) -> TargetRepository:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "refinery@example.com")
    git(path, "config", "user.name", "Refinery Test")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "base")
    base_sha = git(path, "rev-parse", "HEAD")

    git(path, "switch", "-c", AGENT_BRANCH)
    (path / "feature.py").write_text("ENABLED = True\n", encoding="utf-8")
    git(path, "add", "feature.py")
    git(path, "commit", "-m", "Agent checkpoint: implementation")
    commit_sha = git(path, "rev-parse", "HEAD")

    git(path, "switch", "--orphan", "unrelated")
    (path / "elsewhere.py").write_text("OTHER = True\n", encoding="utf-8")
    git(path, "add", "elsewhere.py")
    git(path, "commit", "-m", "unrelated history")
    unrelated_sha = git(path, "rev-parse", "HEAD")

    git(path, "switch", "main")
    return TargetRepository(
        path=path,
        base_sha=base_sha,
        commit_sha=commit_sha,
        unrelated_sha=unrelated_sha,
    )


@dataclass(frozen=True)
class StackRepository:
    """A target repository with several agent branches cut from one base.

    This is the shape milestone 3 is about: `_run_batch` runs independent
    milestones concurrently and each one allocates its worktree from the target
    project's current ``HEAD``, so N branches share one base, and no combination
    of them has ever been tested against each other.
    """

    path: Path
    base_sha: str
    commits: Mapping[str, str]

    def sha(self, name: str) -> str:
        return self.commits[name]


def build_stack_repository(
    path: Path,
    branches: Mapping[str, Mapping[str, str]],
    *,
    trunk: str = "main",
) -> StackRepository:
    """One base commit, then one branch per entry writing the files it names.

    Two branches writing the same path with different content conflict; two
    writing different paths do not. That is the whole vocabulary the stack
    builder's tests need, and expressing it as data keeps each test's setup to
    the one line that says which case it is.
    """

    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", trunk)
    git(path, "config", "user.email", "refinery@example.com")
    git(path, "config", "user.name", "Refinery Test")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    git(path, "add", "README.md")
    git(path, "commit", "-m", "base")
    base_sha = git(path, "rev-parse", "HEAD")

    commits: dict[str, str] = {}
    for name, files in branches.items():
        git(path, "switch", "-c", f"agent/{name}", base_sha)
        for filename, content in files.items():
            (path / filename).write_text(content, encoding="utf-8")
        git(path, "add", "-A")
        git(path, "commit", "-m", f"Agent checkpoint: {name}")
        commits[name] = git(path, "rev-parse", "HEAD")
    git(path, "switch", trunk)
    return StackRepository(path=path, base_sha=base_sha, commits=commits)


def subject_for(
    repository: StackRepository,
    name: str,
    *,
    project_id: str = "target",
    request_id: str | None = None,
    approval_id: str | None = None,
    enqueued_at: float = 1_700_000_000.0,
) -> IntegrationSubject:
    """One approved commit, as the queue holds it.

    `approval_id` defaults to a readable stand-in, which is enough for the tests
    that never write a row. `integration_requests.approval_id` is a foreign key
    to `approval_requests`, so anything that does persist the subject has to pass
    the id of an approval that exists.
    """

    return IntegrationSubject(
        request_id=IntegrationRequestId(request_id or f"req-{name}"),
        target_project_id=project_id,
        branch_name=f"agent/{name}",
        base_head_sha=repository.base_sha,
        commit_sha=repository.sha(name),
        approval_id=approval_id or f"approval-{name}",
        intent_id=f"intent-{name}",
        pow_wow_id=f"pow-{name}",
        milestone_key=f"milestone-{name}",
        changed_files=(),
        enqueued_at=enqueued_at,
    )


def worktree_paths(repository_path: Path) -> tuple[str, ...]:
    """Every worktree git knows about, main checkout included."""

    listed = git(repository_path, "worktree", "list", "--porcelain")
    return tuple(
        line.split(" ", 1)[1] for line in listed.splitlines() if line.startswith("worktree ")
    )


def branch_names(repository_path: Path) -> tuple[str, ...]:
    listed = git(repository_path, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    return tuple(line.strip() for line in listed.splitlines() if line.strip())


def write_registry_config(config_dir: Path, repository_path: Path, *, project_id: str) -> None:
    """A registry the ambient `load_project_center()` will read.

    Ambient rather than injected because that is how the coordination layer
    reaches it: a command arrives as argv with no `Settings` attached, so
    `LOCAL_AGENT_CONFIG_DIR` is the seam, and a test that handed the registry in
    directly would be testing a path production does not take.
    """

    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "linked_projects.toml").write_text(
        f"""
[center]
id = "local_first_agent_os"
description = "refinery fixture"
control_plane_project = "{project_id}"
default_saga_project = "{project_id}"
default_memory_project = "{project_id}"

[[projects]]
id = "{project_id}"
kind = "test_repo"
path = "{repository_path}"
status = "active"
read_only = false
description = "refinery fixture target"
primary_interfaces = ["pytest"]
owns = ["."]
avoid = []
verification_commands = ["true"]
""".strip()
        + "\n",
        encoding="utf-8",
    )


def code_merge_payload(repository: TargetRepository, *, project_id: str) -> dict[str, Any]:
    """The payload `dispatcher_runner` submits, with nothing missing."""

    return {
        "target_project_id": project_id,
        "branch": AGENT_BRANCH,
        "base_sha": repository.base_sha,
        "commit_sha": repository.commit_sha,
        "intent_id": "intent-refinery",
        "pow_wow_id": "pow-refinery",
        "milestone_id": "milestone-refinery",
        "changed_files": ["feature.py"],
    }
