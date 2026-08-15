# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Whether an approved `CODE_MERGE` may join the queue, and why it may not.

Enqueue happens on resolution to APPROVED, not on submission. A request queued
before a human agreed to it would let the refinery merge something nobody
approved, which is invariant 1 and also `AGENT_BRANCH_AUTO_MERGE = False` set to
True by a different route.

The refusals are the content of this module, and they are all the same shape of
question: is this approval a thing that could land at all. Each one names a
precondition the rest of the design assumes and never re-checks, so a payload
that fails one here fails it once, at the boundary, rather than three milestones
later inside a merge:

- the merge is by sha and the sha has to exist. `require_staff_review_provenance`
  bound the approval to a commit, not to a branch name, so the refinery merges
  the commit; a sha the repository does not have is not something to discover
  while a stack is half built.
- the commit has to sit on top of the base the approval named. If it does not,
  merging it carries history nobody reviewed, which is the same hole as merging
  by name arriving through a different door.
- the project has to declare a gate. `all(... for ... in ())` is True, so a
  project with no verification commands would give every stack a green gate it
  never ran, and that vacuous truth already produced one class of false
  certification in `pow_wow/executor.py`.
- the project's declared integrated branch has to exist. It is where a stack
  starts and the one ref the whole design writes, so a name the repository does
  not have is a run with no base and no target. Refused here rather than in the
  loop so the operator hears about it when they approve, which is when they are
  still looking at the terminal, instead of whenever a poll next wakes up.

`PROJECT_IS_READ_ONLY` is a fifth refusal the design document does not list. It
is here because the one write this whole design performs is a fast-forward of
the target project's integrated branch, and a project the registry marks
read-only is one the system may not write to. `dispatcher_runner` already
refuses code dispatch for such a project, so nothing should be able to produce
the approval; a queue entry that nothing may ever land is worth refusing where it
would be created rather than discovering at the one moment it matters.

Nothing here touches the ledger. What a duplicate means is a fact about rows
that already exist, so it is decided by `coordination/integration_queue.py`,
where a unique index can decide it rather than a check somebody remembers to run.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from ..constants import DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS
from ..project_center import LinkedProject
from .requests import IntegrationRequestId, IntegrationSubject, Queued, is_full_commit_sha


class RepositoryProbe(Protocol):
    """The three questions enqueue asks git, and nothing else.

    A protocol rather than a module function because the answers are the only
    part of this decision that needs a repository, and a rule that can be
    exercised without one is a rule that gets exercised.
    """

    def contains_commit(self, sha: str) -> bool:
        """Whether this repository holds a commit object with that name."""
        ...

    def is_ancestor(self, *, ancestor_sha: str, descendant_sha: str) -> bool:
        """Whether the first commit is reachable from the second."""
        ...

    def has_branch(self, branch_name: str) -> bool:
        """Whether `refs/heads/<branch_name>` exists here."""
        ...


@dataclass(frozen=True)
class GitRepositoryProbe:
    """The real answers, from the target project's own repository."""

    repository_path: Path
    timeout_seconds: int = DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS

    @classmethod
    def for_project(cls, project: LinkedProject) -> GitRepositoryProbe:
        return cls(project.expanded_path)

    def contains_commit(self, sha: str) -> bool:
        # `^{commit}` rather than a bare existence check: a name that resolves to
        # a blob or a tree exists and is still not something to merge.
        return self._exit_code("cat-file", "-e", f"{sha}^{{commit}}") == 0

    def is_ancestor(self, *, ancestor_sha: str, descendant_sha: str) -> bool:
        code = self._exit_code("merge-base", "--is-ancestor", ancestor_sha, descendant_sha)
        if code not in (0, 1):
            raise RuntimeError(
                f"git merge-base --is-ancestor {ancestor_sha} {descendant_sha} in "
                f"{self.repository_path} exited {code}; the refinery cannot decide whether "
                "the approved commit descends from its base"
            )
        return code == 0

    def has_branch(self, branch_name: str) -> bool:
        # The full ref path rather than the bare name: `show-ref --verify` on
        # `refs/heads/<name>` cannot be satisfied by a tag or a remote-tracking
        # ref that happens to share the name, and the refinery fast-forwards a
        # local branch or nothing.
        return self._exit_code("show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}") == 0

    def _exit_code(self, *args: str) -> int:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=self.repository_path,
                capture_output=True,
                check=False,
                text=True,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(
                f"git {' '.join(args)} in {self.repository_path} could not run: {exc}"
            ) from exc
        return completed.returncode


class EnqueueRefusal(StrEnum):
    """Why an approved `CODE_MERGE` is not a thing the refinery can land."""

    MALFORMED_SUBJECT = "MALFORMED_SUBJECT"
    PROJECT_NOT_LINKED = "PROJECT_NOT_LINKED"
    PROJECT_IS_READ_ONLY = "PROJECT_IS_READ_ONLY"
    GATE_NOT_DECLARED = "GATE_NOT_DECLARED"
    INTEGRATED_BRANCH_MISSING = "INTEGRATED_BRANCH_MISSING"
    COMMIT_NOT_IN_REPOSITORY = "COMMIT_NOT_IN_REPOSITORY"
    COMMIT_IS_ITS_OWN_BASE = "COMMIT_IS_ITS_OWN_BASE"
    COMMIT_NOT_DESCENDED_FROM_BASE = "COMMIT_NOT_DESCENDED_FROM_BASE"


@dataclass(frozen=True)
class EnqueueAdmitted:
    """The approval describes exactly one landable commit, and here it is."""

    request: Queued


@dataclass(frozen=True)
class EnqueueRefused:
    """It does not, and this is the sentence an operator gets to read.

    `message` is written for the person who just typed approve, so it names the
    project, the sha, or the field rather than restating the enum. The enum is
    what a caller matches on; the message is what a caller prints.
    """

    refusal: EnqueueRefusal
    message: str


type EnqueueAdmission = EnqueueAdmitted | EnqueueRefused


class ProjectRegistry(Protocol):
    """The registry lookup enqueue needs. `ProjectCenter` satisfies it."""

    def project_by_id(self, project_id: str) -> LinkedProject: ...


_REQUIRED_PAYLOAD_FIELDS = (
    "target_project_id",
    "branch",
    "base_sha",
    "commit_sha",
    "intent_id",
    "pow_wow_id",
)
"""What a `CODE_MERGE` payload must carry to describe one landable commit.

The first four are what the merge itself needs. The last two are provenance, and
they are required for the same reason the subject carries them: a parked request
is read by a human months later, and a durable row that cannot say which run
produced it is a record of nothing. `dispatcher_runner` is the only producer and
sets all six, so requiring them costs nothing today and tells a second producer
immediately rather than letting it write rows with holes in them.
"""


def admit_to_queue(
    payload: Mapping[str, object],
    *,
    approval_id: str,
    request_id: IntegrationRequestId,
    enqueued_at: float,
    registry: ProjectRegistry,
    probe_for: Callable[[LinkedProject], RepositoryProbe] = GitRepositoryProbe.for_project,
) -> EnqueueAdmission:
    """Turn one resolved `CODE_MERGE` payload into a queued request, or refuse it.

    Ordered cheapest first, and deliberately so: the field checks need nothing,
    the registry lookup needs a config file, and only the last two spawn git. An
    approval that is malformed never reaches a subprocess.
    """

    fields = {name: _text(payload.get(name)) for name in _REQUIRED_PAYLOAD_FIELDS}
    missing = [name for name, value in fields.items() if not value]
    if missing:
        return EnqueueRefused(
            EnqueueRefusal.MALFORMED_SUBJECT,
            f"CODE_MERGE approval {approval_id} names no {', '.join(missing)}, so it does "
            "not describe a commit the refinery could land",
        )
    for name in ("base_sha", "commit_sha"):
        if not is_full_commit_sha(fields[name]):
            return EnqueueRefused(
                EnqueueRefusal.MALFORMED_SUBJECT,
                f"CODE_MERGE approval {approval_id} carries {name}={fields[name]!r}, which is "
                "not a full lowercase object name; the refinery merges by sha and an "
                "abbreviation is a prefix query whose answer can change",
            )

    project_id = fields["target_project_id"]
    try:
        project = registry.project_by_id(project_id)
    except (KeyError, FileNotFoundError, ValueError) as exc:
        return EnqueueRefused(
            EnqueueRefusal.PROJECT_NOT_LINKED,
            f"CODE_MERGE approval {approval_id} targets project {project_id!r}, which the "
            f"project registry does not offer: {exc}",
        )
    if project.read_only:
        return EnqueueRefused(
            EnqueueRefusal.PROJECT_IS_READ_ONLY,
            f"project {project_id!r} is read-only, and the one write this queue performs is "
            "a fast-forward of its integrated branch",
        )
    if not project.verification_commands:
        return EnqueueRefused(
            EnqueueRefusal.GATE_NOT_DECLARED,
            f"project {project_id!r} declares no verification commands, so a stack built "
            "for it would pass a gate that never ran; declare them in linked_projects.toml "
            "before approving a merge into it",
        )

    probe = probe_for(project)
    if not probe.has_branch(project.integrated_branch):
        return EnqueueRefused(
            EnqueueRefusal.INTEGRATED_BRANCH_MISSING,
            f"project {project_id!r} declares integrated_branch "
            f"{project.integrated_branch!r}, which does not exist in its repository; the "
            "refinery starts every stack at that branch's tip and fast-forwards it at the "
            "end, so there is nothing to build on and nothing to advance",
        )
    for name in ("commit_sha", "base_sha"):
        if not probe.contains_commit(fields[name]):
            return EnqueueRefused(
                EnqueueRefusal.COMMIT_NOT_IN_REPOSITORY,
                f"{project_id} does not contain {name} {fields[name]} from CODE_MERGE "
                f"approval {approval_id}; the branch it was committed on may have been "
                "deleted or never existed in this checkout",
            )
    if fields["commit_sha"] == fields["base_sha"]:
        return EnqueueRefused(
            EnqueueRefusal.COMMIT_IS_ITS_OWN_BASE,
            f"CODE_MERGE approval {approval_id} asks to land {fields['commit_sha']}, which "
            "is the base it was taken from; there is nothing to integrate",
        )
    if not probe.is_ancestor(ancestor_sha=fields["base_sha"], descendant_sha=fields["commit_sha"]):
        return EnqueueRefused(
            EnqueueRefusal.COMMIT_NOT_DESCENDED_FROM_BASE,
            f"{fields['commit_sha']} does not descend from base {fields['base_sha']} in "
            f"{project_id}; merging it would carry history the approval did not name",
        )

    return EnqueueAdmitted(
        Queued(
            IntegrationSubject(
                request_id=request_id,
                target_project_id=project_id,
                branch_name=fields["branch"],
                base_head_sha=fields["base_sha"],
                commit_sha=fields["commit_sha"],
                approval_id=approval_id,
                intent_id=fields["intent_id"],
                pow_wow_id=fields["pow_wow_id"],
                milestone_key=_text(payload.get("milestone_id")) or None,
                changed_files=_changed_files(payload.get("changed_files")),
                enqueued_at=enqueued_at,
            )
        )
    )


def _text(value: object) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _changed_files(value: object) -> tuple[str, ...]:
    """What the run reported it touched, for the operator's benefit only.

    Not a collision predicate and not validated against the diff. Path overlap
    has false positives and false negatives for the question anyone cares about,
    which is whether the combination works, and the gate answers that directly.
    """

    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


__all__ = [
    "EnqueueAdmission",
    "EnqueueAdmitted",
    "EnqueueRefusal",
    "EnqueueRefused",
    "GitRepositoryProbe",
    "ProjectRegistry",
    "RepositoryProbe",
    "admit_to_queue",
]
