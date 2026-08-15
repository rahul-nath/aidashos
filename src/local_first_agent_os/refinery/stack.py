# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Building the throwaway stack, and proving what is on it.

`bisect` decides which requests to try and who to blame. This module is the half
that touches git: allocate a worktree, merge the commits in order, notice a
conflict, and tear the worktree down. It decides nothing about attribution, and
it never advances the target project's integrated branch.

The promise it keeps
====================

**Nothing outside the integration worktree is touched.** Every merge happens in a
detached worktree under ``<saga_worktree_root>/integration/<batch_id>``, so a
conflicting merge leaves conflict markers in a throwaway directory and the target
project's working tree, index, ``HEAD``, and refs are untouched by every path
through this module. There is no path through this module that writes a ref.

Detached rather than ``-b``, so an abandoned attempt leaves no ref at all. A
named branch would leak, and a leaked branch under a prefix an operator
recognises is worse than a leaked directory, which ``git worktree list`` shows
and ``git worktree prune`` collects.

Why the merge strategy is not always the same
=============================================

A stack applying exactly one commit onto the base is merged ``--ff-only``, so its
tip *is* the approved sha and the existing
``integrated_commit_sha == approved_commit_sha`` harness fact holds literally
rather than by an argument. Anything else is merged ``--no-ff``, which keeps the
stack shape independent of how the base happened to sit relative to the first
commit and makes ``git log --first-parent`` on the stack exactly the queue order.

A single commit that cannot fast-forward, because the integrated branch moved
since the agent took its base, falls back to ``--no-ff``. That is not a conflict
and nobody is parked for it; it is the ordinary case of a queue that has been
running, and `verify_provenance` is what covers the resulting merge commit.

What a failed merge means
=========================

Distinguishing "these diffs disagree" from "git would not do that" is done by
asking for unmerged paths rather than by reading exit codes or stderr text. A
conflicted merge leaves entries in the index with a nonzero stage; a refused
fast-forward leaves none. The first is attributable to a request, the second is
not about anybody's diff.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..constants import DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS
from .requests import IntegrationBatchId, IntegrationRequestId, IntegrationSubject, MergeConflict

_INTEGRATION_WORKTREE_DIRECTORY = "integration"
"""Sibling of the ``agent/`` worktrees, so `git worktree list` reads unambiguously."""


@dataclass(frozen=True)
class GitFailure(Exception):
    """A git command the refinery cannot proceed without did not run.

    An exception rather than a verdict because there is no answer to report: a
    `rev-parse` that cannot run has not said the branch is missing, it has said
    nothing. The driver turns it into an abandoned attempt, which parks nobody.
    """

    command: tuple[str, ...]
    exit_code: int
    output: str

    def __str__(self) -> str:
        return f"git {' '.join(self.command)} exited {self.exit_code}: {self.output}"


@dataclass(frozen=True)
class _GitResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return self.stderr.strip() or self.stdout.strip()


@dataclass(frozen=True)
class IntegrationWorkspace:
    """One allocated integration worktree, and the base it was cut from."""

    batch_id: IntegrationBatchId
    path: Path
    base_sha: str


@dataclass(frozen=True)
class StackBuilt:
    """Every candidate applied cleanly. The tip is what a gate would judge."""

    tip_sha: str
    applied: tuple[IntegrationRequestId, ...]


@dataclass(frozen=True)
class StackConflicted:
    """A candidate would not apply onto the candidates before it.

    Attributable with no gate run at all, because the candidates ahead of it
    applied cleanly by construction. The merge is aborted before this is
    returned, so the worktree is left mergeable rather than mid-conflict.
    """

    request_id: IntegrationRequestId
    conflict: MergeConflict


@dataclass(frozen=True)
class StackUnbuildable:
    """git refused for a reason that is not about anyone's diff.

    Nobody is parked for one of these. The worktree could not be allocated, the
    base could not be read, or a merge failed while leaving no unmerged paths.
    """

    detail: str


type StackBuildOutcome = StackBuilt | StackConflicted | StackUnbuildable


@dataclass(frozen=True)
class ProvenanceHeld:
    """Every commit on the stack came from a request that was approved."""


@dataclass(frozen=True)
class ProvenanceBroken:
    """The stack carries work no request contributed, or is missing one it should.

    Two different failures in one verdict because both mean the same thing to a
    caller - do not advance the branch - and neither is expected to happen. They
    are separate fields so the message can say which.
    """

    unapproved_commits: tuple[str, ...]
    unreachable_requests: tuple[IntegrationRequestId, ...]

    def describe(self) -> str:
        parts: list[str] = []
        if self.unapproved_commits:
            parts.append(
                f"{len(self.unapproved_commits)} commit(s) on the stack were contributed by no "
                f"approved request: {', '.join(self.unapproved_commits[:5])}"
            )
        if self.unreachable_requests:
            parts.append(
                "the stack does not contain approved request(s) "
                f"{', '.join(self.unreachable_requests[:5])}"
            )
        return "; ".join(parts)


type ProvenanceVerdict = ProvenanceHeld | ProvenanceBroken


@dataclass(frozen=True)
class StackBuilder:
    """The git operations one refinery run performs, against one repository.

    A dataclass rather than free functions because every call needs the same
    three things, and threading them through eight signatures is how one of them
    ends up pointing at the wrong repository.
    """

    repository_path: Path
    worktree_root: Path
    timeout_seconds: int = DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS

    # -- reads -------------------------------------------------------------

    def integrated_tip(self, branch_name: str) -> str:
        """The sha every stack for this project starts from.

        Read at the top of each attempt rather than cached for the run: the
        branch is the operator's, and a run that built on a tip somebody moved
        underneath it would compute a fast-forward that no longer applies.
        """

        return self._must(("rev-parse", f"refs/heads/{branch_name}"), cwd=self.repository_path)

    def checked_out_branch(self) -> str | None:
        """The branch the target checkout has out, or None when it is detached.

        Milestone 4 refuses to advance a checkout sitting somewhere other than
        the declared integrated branch, for the same reason it refuses a dirty
        one: switching it would mutate a working tree this design promises not to
        touch, and on the success path, which is where the promise is worth most.
        """

        result = self._run(("symbolic-ref", "--quiet", "--short", "HEAD"), cwd=self.repository_path)
        if result.exit_code != 0:
            return None
        return result.stdout.strip() or None

    def working_tree_is_dirty(self) -> bool:
        """Whether the target checkout has uncommitted changes.

        `--porcelain` over `diff --quiet` because it answers for the index and
        the working tree together, and untracked files do not count: they cannot
        block a fast-forward, and treating them as dirt would refuse to land work
        because somebody left a scratch file lying around.
        """

        status = self._must(("status", "--porcelain", "--untracked-files=no"), self.repository_path)
        return bool(status.strip())

    # -- the worktree ------------------------------------------------------

    def allocate(
        self,
        *,
        batch_id: IntegrationBatchId,
        base_sha: str,
    ) -> IntegrationWorkspace | StackUnbuildable:
        """Cut a detached worktree at `base_sha`, or say why not."""

        path = self.worktree_root / _INTEGRATION_WORKTREE_DIRECTORY / str(batch_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return StackUnbuildable(f"could not create {path.parent}: {exc}")

        if path.exists():
            # A directory left by a run that died between allocation and
            # teardown. Removing it is safe precisely because nothing here ever
            # advanced a ref: an unfinished attempt can always be redone from
            # scratch, which is the same argument recovery makes for InFlight.
            self.teardown(IntegrationWorkspace(batch_id=batch_id, path=path, base_sha=base_sha))

        result = self._run(
            ("worktree", "add", "--detach", str(path), base_sha),
            cwd=self.repository_path,
        )
        if result.exit_code != 0:
            return StackUnbuildable(
                f"could not allocate an integration worktree at {path} from {base_sha}: "
                f"{result.output}"
            )
        return IntegrationWorkspace(batch_id=batch_id, path=path, base_sha=base_sha)

    def teardown(self, workspace: IntegrationWorkspace) -> str | None:
        """Remove the worktree, and mean it.

        The same `worktree remove --force` plus `shutil.rmtree` fallback the
        executor already uses, because the failure it covers is the same one: a
        worktree whose directory git has lost track of is removable by neither
        command alone, and `git worktree list` on the target project must be
        clean after every run, green or red.
        """

        result = self._run(
            ("worktree", "remove", "--force", str(workspace.path)),
            cwd=self.repository_path,
        )
        if result.exit_code == 0:
            return None
        if workspace.path.exists():
            shutil.rmtree(workspace.path, ignore_errors=True)
        # Drop the administrative entry the failed removal left behind, so the
        # next `git worktree list` does not report a directory that is gone.
        self._run(("worktree", "prune"), cwd=self.repository_path)
        return result.output or None

    # -- the merges --------------------------------------------------------

    def build(
        self,
        workspace: IntegrationWorkspace,
        subjects: Sequence[IntegrationSubject],
    ) -> StackBuildOutcome:
        """Merge each subject onto the stack in order, stopping at the first conflict.

        Order is the caller's, and it is the queue's total order. This function
        does not sort, deduplicate, or skip: doing any of those here would put a
        second opinion about ordering next to the one in `queue`, and the two
        would disagree the first time somebody changed one.
        """

        if not subjects:
            raise ValueError(
                "a stack of no subjects is NothingToIntegrate, not an empty build; "
                "an empty stack is trivially green and would certify nothing"
            )

        fast_forward_allowed = len(subjects) == 1
        applied: list[IntegrationRequestId] = []
        for subject in subjects:
            outcome = self._merge_one(
                workspace,
                subject,
                allow_fast_forward=fast_forward_allowed,
            )
            if outcome is not None:
                return outcome
            applied.append(subject.request_id)

        try:
            tip = self._must(("rev-parse", "HEAD"), cwd=workspace.path)
        except GitFailure as failure:
            return StackUnbuildable(f"the stack was built and its tip could not be read: {failure}")
        return StackBuilt(tip_sha=tip, applied=tuple(applied))

    def _merge_one(
        self,
        workspace: IntegrationWorkspace,
        subject: IntegrationSubject,
        *,
        allow_fast_forward: bool,
    ) -> StackConflicted | StackUnbuildable | None:
        """Apply one commit. `None` means it applied and the stack moved on."""

        if allow_fast_forward:
            result = self._run(("merge", "--ff-only", subject.commit_sha), cwd=workspace.path)
            if result.exit_code == 0:
                return None
            # A refused fast-forward leaves nothing behind and says nothing about
            # the diff; fall through to the real merge, which is what decides.

        result = self._run(
            ("merge", "--no-ff", "--no-edit", subject.commit_sha),
            cwd=workspace.path,
        )
        if result.exit_code == 0:
            return None

        conflicted = self._unmerged_paths(workspace)
        self._abort_merge(workspace)
        if conflicted:
            return StackConflicted(
                request_id=subject.request_id,
                conflict=MergeConflict(conflicted_paths=conflicted),
            )
        return StackUnbuildable(
            f"merging {subject.commit_sha} for request {subject.request_id} failed with no "
            f"unmerged paths, so it is not a statement about the diff: {result.output}"
        )

    def _unmerged_paths(self, workspace: IntegrationWorkspace) -> tuple[str, ...]:
        """The paths git could not resolve, in a stable order.

        `diff --name-only --diff-filter=U` reads the index rather than the
        merge's stderr, so this is the same answer whatever git chooses to print
        and whatever locale it prints it in.
        """

        result = self._run(
            ("diff", "--name-only", "--diff-filter=U"),
            cwd=workspace.path,
        )
        if result.exit_code != 0:
            return ()
        return tuple(sorted(line.strip() for line in result.stdout.splitlines() if line.strip()))

    def _abort_merge(self, workspace: IntegrationWorkspace) -> None:
        """Leave the worktree at the last clean stack, not mid-conflict.

        The worktree is about to be torn down in the common case, so this is
        belt and braces. It matters for the case the design actually turns on: a
        conflict drops the request and re-attempts the rest from the same base,
        and an unaborted merge would make the next merge refuse for a reason
        that has nothing to do with the commit it is refusing.
        """

        self._run(("merge", "--abort"), cwd=workspace.path)

    # -- the provenance check ---------------------------------------------

    def verify_provenance(
        self,
        workspace: IntegrationWorkspace,
        subjects: Sequence[IntegrationSubject],
        *,
        tip_sha: str,
    ) -> ProvenanceVerdict:
        """Whether the stack carries exactly the approved work and nothing else.

        `evaluate_lifecycle_invariants` requires the integrated commit to equal
        the approved one, and for a stack of more than one member the tip is a
        merge commit that equals no approved sha. This is what replaces that
        check, and it is strictly stronger than the containment test
        `workflow/engine.py` already uses: reachability alone would happily pass
        a stack carrying arbitrary extra work.

        Two clauses:

        - every approved commit is an ancestor of the tip, so nothing was
          silently dropped; and
        - every non-merge commit the stack added is one some approved request
          contributed.

        The second is the load-bearing one. The refinery adds only merge commits
        and never resolves a conflict, so it introduces no content of its own,
        and any non-merge commit in the integrated range that no request
        contributed is an unapproved commit.
        """

        # A `GitFailure` here propagates on purpose. Undecidable is not the same
        # as violated, and returning `ProvenanceBroken` for a `rev-list` that
        # could not run would name commits nobody wrote. The driver turns the
        # exception into an abandoned attempt, which parks nobody.
        unreachable = tuple(
            subject.request_id
            for subject in subjects
            if not self._is_ancestor(subject.commit_sha, tip_sha, cwd=workspace.path)
        )
        on_stack = self._non_merge_commits(workspace.base_sha, tip_sha, cwd=workspace.path)
        approved: set[str] = set()
        for subject in subjects:
            approved |= set(
                self._non_merge_commits(
                    workspace.base_sha,
                    subject.commit_sha,
                    cwd=workspace.path,
                )
            )

        unapproved = tuple(sha for sha in on_stack if sha not in approved)
        if unapproved or unreachable:
            return ProvenanceBroken(
                unapproved_commits=unapproved,
                unreachable_requests=unreachable,
            )
        return ProvenanceHeld()

    def _is_ancestor(self, ancestor_sha: str, descendant_sha: str, *, cwd: Path) -> bool:
        result = self._run(
            ("merge-base", "--is-ancestor", ancestor_sha, descendant_sha),
            cwd=cwd,
        )
        if result.exit_code not in (0, 1):
            raise GitFailure(
                command=("merge-base", "--is-ancestor", ancestor_sha, descendant_sha),
                exit_code=result.exit_code,
                output=result.output,
            )
        return result.exit_code == 0

    def _non_merge_commits(self, base_sha: str, tip_sha: str, *, cwd: Path) -> tuple[str, ...]:
        output = self._must(
            ("rev-list", "--no-merges", f"{base_sha}..{tip_sha}"),
            cwd=cwd,
        )
        return tuple(line.strip() for line in output.splitlines() if line.strip())

    # -- plumbing ----------------------------------------------------------

    def _run(self, args: Sequence[str], cwd: Path) -> _GitResult:
        try:
            completed = subprocess.run(
                ["git", "-C", str(cwd), *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return _GitResult(exit_code=-1, stdout="", stderr=f"{type(exc).__name__}: {exc}")
        return _GitResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def _must(self, args: Sequence[str], cwd: Path) -> str:
        result = self._run(args, cwd=cwd)
        if result.exit_code != 0:
            raise GitFailure(
                command=tuple(args),
                exit_code=result.exit_code,
                output=result.output,
            )
        return result.stdout.strip()


__all__ = [
    "GitFailure",
    "IntegrationWorkspace",
    "ProvenanceBroken",
    "ProvenanceHeld",
    "ProvenanceVerdict",
    "StackBuildOutcome",
    "StackBuilder",
    "StackBuilt",
    "StackConflicted",
    "StackUnbuildable",
]
