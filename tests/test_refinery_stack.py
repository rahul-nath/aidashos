# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What the refinery does to a repository, against real repositories.

The bisect rule is tested without git in `test_refinery_integration_queue.py`,
which is the right way to test a rule. This file is the other half and it takes
the opposite approach on purpose: every assertion here is about something only
git can answer. Whether a merge conflicts, which paths it conflicts on, whether
an aborted merge leaves an index a later merge can use, whether a worktree
removal actually removed it, and whether a `rev-list` range says what the
provenance check thinks it says are not things a fake can be trusted about,
because a fake would encode the same belief the code under test encodes.

The promise being tested is negative and therefore easy to break silently: the
target project's working tree, index, `HEAD`, and refs are untouched by every
path through this module. A test suite for that has to look at the repository
after each case rather than only at the returned value, which is why the
worktree, branch, and status assertions repeat rather than being factored into
one happy-path check.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from refinery_support import (
    StackRepository,
    branch_names,
    build_stack_repository,
    git,
    subject_for,
    worktree_paths,
)

from local_first_agent_os.refinery.requests import IntegrationBatchId
from local_first_agent_os.refinery.stack import (
    ProvenanceBroken,
    ProvenanceHeld,
    StackBuilder,
    StackBuilt,
    StackConflicted,
    StackUnbuildable,
)

# Two branches touching different files combine cleanly; `conflicting` rewrites
# the file `alpha` created, which is the only way to make git refuse.
_BRANCHES = {
    "alpha": {"alpha.py": "ALPHA = 1\n"},
    "beta": {"beta.py": "BETA = 1\n"},
    "gamma": {"gamma.py": "GAMMA = 1\n"},
    "conflicting": {"alpha.py": "ALPHA = 999\n"},
}


@pytest.fixture
def repository(tmp_path: Path) -> StackRepository:
    return build_stack_repository(tmp_path / "target", _BRANCHES)


@pytest.fixture
def builder(repository: StackRepository, tmp_path: Path) -> StackBuilder:
    return StackBuilder(
        repository_path=repository.path,
        worktree_root=tmp_path / "worktrees",
    )


def _batch(name: str) -> IntegrationBatchId:
    return IntegrationBatchId(name)


def _assert_target_untouched(repository: StackRepository) -> None:
    """The refinery's central promise, checked the only way it can be.

    Milestone 3 never advances anything, so `main` must still be the base it
    started at, no integration worktree may survive, and no ref may have been
    created for one.
    """

    assert git(repository.path, "rev-parse", "refs/heads/main") == repository.base_sha
    assert git(repository.path, "status", "--porcelain") == ""
    assert worktree_paths(repository.path) == (str(repository.path.resolve()),)
    assert branch_names(repository.path) == (
        "agent/alpha",
        "agent/beta",
        "agent/conflicting",
        "agent/gamma",
        "main",
    )


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def test_independent_requests_stack_in_queue_order(
    repository: StackRepository,
    builder: StackBuilder,
) -> None:
    """The stack carries every commit, and `--first-parent` is the queue order.

    Order is the property the whole bisect rule rests on: when two diffs
    disagree, the one approved later is the one parked. That is only a rule if
    the stack was actually built in that order.
    """

    workspace = builder.allocate(batch_id=_batch("b1"), base_sha=repository.base_sha)
    assert not isinstance(workspace, StackUnbuildable)

    subjects = [subject_for(repository, name) for name in ("alpha", "beta", "gamma")]
    built = builder.build(workspace, subjects)
    assert isinstance(built, StackBuilt)
    assert built.applied == tuple(subject.request_id for subject in subjects)

    merged = git(workspace.path, "log", "--first-parent", "--format=%s", f"{repository.base_sha}..")
    assert [line for line in merged.splitlines() if line.startswith("Merge")] != []
    for name in ("alpha", "beta", "gamma"):
        assert (workspace.path / f"{name}.py").exists()

    builder.teardown(workspace)
    _assert_target_untouched(repository)


def test_a_lone_request_fast_forwards_so_the_tip_is_the_approved_sha(
    repository: StackRepository,
    builder: StackBuilder,
) -> None:
    """Tip equality is preserved literally for the case it was written for.

    `evaluate_lifecycle_invariants` requires the integrated commit to equal the
    approved one. For a batch of one that is a fact rather than an argument, and
    it stays a fact only because this merge is `--ff-only`. A `--no-ff` here
    would produce a merge commit equal to no approved sha and quietly move the
    single-request case onto the containment check.
    """

    workspace = builder.allocate(batch_id=_batch("b2"), base_sha=repository.base_sha)
    assert not isinstance(workspace, StackUnbuildable)

    built = builder.build(workspace, [subject_for(repository, "alpha")])
    assert isinstance(built, StackBuilt)
    assert built.tip_sha == repository.sha("alpha")

    builder.teardown(workspace)
    _assert_target_untouched(repository)


def test_the_stack_is_built_from_the_sha_not_the_branch_name(
    repository: StackRepository,
    builder: StackBuilder,
) -> None:
    """A branch that moved after approval does not smuggle a commit in.

    `require_staff_review_provenance` bound the approval to a sha, not to a
    name. Merging the name would let a later commit ride in under an approval
    nobody gave it, which is why the subject carries `commit_sha` at all.
    """

    approved = repository.sha("alpha")
    git(repository.path, "switch", "agent/alpha")
    (repository.path / "sneaked.py").write_text("SNEAKED = True\n", encoding="utf-8")
    git(repository.path, "add", "-A")
    git(repository.path, "commit", "-m", "not approved by anybody")
    moved = git(repository.path, "rev-parse", "HEAD")
    git(repository.path, "switch", "main")
    assert moved != approved

    workspace = builder.allocate(batch_id=_batch("b3"), base_sha=repository.base_sha)
    assert not isinstance(workspace, StackUnbuildable)
    built = builder.build(workspace, [subject_for(repository, "alpha")])

    assert isinstance(built, StackBuilt)
    assert built.tip_sha == approved
    assert not (workspace.path / "sneaked.py").exists()
    builder.teardown(workspace)


def test_an_empty_stack_is_refused_rather_than_being_trivially_green(
    repository: StackRepository,
    builder: StackBuilder,
) -> None:
    """A stack of nothing passes every check by having nothing to check.

    The naive path would fast-forward the integrated branch to the base it
    started from, succeed, and write a durable record of an integration that
    integrated nothing, which a later reader cannot tell from one that did.
    """

    workspace = builder.allocate(batch_id=_batch("b4"), base_sha=repository.base_sha)
    assert not isinstance(workspace, StackUnbuildable)
    with pytest.raises(ValueError, match="NothingToIntegrate"):
        builder.build(workspace, [])
    builder.teardown(workspace)


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------


def test_a_conflict_names_the_request_and_the_paths(
    repository: StackRepository,
    builder: StackBuilder,
) -> None:
    """Attributable with no gate run, because everything ahead of it applied."""

    workspace = builder.allocate(batch_id=_batch("b5"), base_sha=repository.base_sha)
    assert not isinstance(workspace, StackUnbuildable)

    built = builder.build(
        workspace,
        [subject_for(repository, "alpha"), subject_for(repository, "conflicting")],
    )

    assert isinstance(built, StackConflicted)
    assert built.request_id == "req-conflicting"
    assert built.conflict.conflicted_paths == ("alpha.py",)
    builder.teardown(workspace)
    _assert_target_untouched(repository)


def test_a_conflict_is_aborted_so_the_stack_can_be_rebuilt_without_it(
    repository: StackRepository,
    builder: StackBuilder,
) -> None:
    """The rule drops the culprit and re-attempts from the same base.

    That only works if the conflicted merge was aborted. An unaborted merge
    leaves an index with unmerged entries, and the next merge refuses for a
    reason that has nothing to do with the commit it is refusing - which would
    park an innocent request with a conflict it did not cause.
    """

    workspace = builder.allocate(batch_id=_batch("b6"), base_sha=repository.base_sha)
    assert not isinstance(workspace, StackUnbuildable)

    conflicted = builder.build(
        workspace,
        [subject_for(repository, "alpha"), subject_for(repository, "conflicting")],
    )
    assert isinstance(conflicted, StackConflicted)
    assert git(workspace.path, "status", "--porcelain") == ""

    retried = builder.build(
        workspace,
        [subject_for(repository, "alpha"), subject_for(repository, "beta")],
    )
    assert isinstance(retried, StackBuilt)
    builder.teardown(workspace)
    _assert_target_untouched(repository)


def test_a_conflict_on_the_first_candidate_is_still_attributed_to_it(
    repository: StackRepository,
    builder: StackBuilder,
) -> None:
    """Nothing applied ahead of it, and it is still the one that would not apply."""

    git(repository.path, "switch", "main")
    (repository.path / "alpha.py").write_text("ALPHA = 'trunk moved'\n", encoding="utf-8")
    git(repository.path, "add", "-A")
    git(repository.path, "commit", "-m", "trunk edits the same file")
    moved_base = git(repository.path, "rev-parse", "HEAD")

    workspace = builder.allocate(batch_id=_batch("b7"), base_sha=moved_base)
    assert not isinstance(workspace, StackUnbuildable)

    built = builder.build(workspace, [subject_for(repository, "alpha")])
    assert isinstance(built, StackConflicted)
    assert built.request_id == "req-alpha"
    assert built.conflict.conflicted_paths == ("alpha.py",)
    builder.teardown(workspace)


# ---------------------------------------------------------------------------
# Containment and provenance
# ---------------------------------------------------------------------------


def test_provenance_holds_for_a_stack_of_exactly_the_approved_work(
    repository: StackRepository,
    builder: StackBuilder,
) -> None:
    workspace = builder.allocate(batch_id=_batch("b8"), base_sha=repository.base_sha)
    assert not isinstance(workspace, StackUnbuildable)
    subjects = [subject_for(repository, name) for name in ("alpha", "beta")]
    built = builder.build(workspace, subjects)
    assert isinstance(built, StackBuilt)

    assert isinstance(
        builder.verify_provenance(workspace, subjects, tip_sha=built.tip_sha),
        ProvenanceHeld,
    )
    builder.teardown(workspace)


def test_a_stack_carrying_a_commit_no_request_contributed_fails_provenance(
    repository: StackRepository,
    builder: StackBuilder,
) -> None:
    """The clause that makes this stronger than a reachability check.

    `workflow/engine.py`'s existing containment test asks only whether the
    approved commit is reachable from the tip, which a stack carrying arbitrary
    extra work passes happily. This asks the other direction: every non-merge
    commit the stack added must be one some approved request contributed.
    """

    workspace = builder.allocate(batch_id=_batch("b9"), base_sha=repository.base_sha)
    assert not isinstance(workspace, StackUnbuildable)
    subjects = [subject_for(repository, name) for name in ("alpha", "beta")]
    built = builder.build(workspace, subjects)
    assert isinstance(built, StackBuilt)

    # Exactly the shape being guarded: a commit on the stack that no approved
    # request contributed. Verifying against a subject list missing `beta` is
    # the same question from the other side, and is how a dropped request or a
    # smuggled commit would present.
    verdict = builder.verify_provenance(workspace, subjects[:1], tip_sha=built.tip_sha)

    assert isinstance(verdict, ProvenanceBroken)
    assert repository.sha("beta") in verdict.unapproved_commits
    assert "contributed by no approved request" in verdict.describe()
    builder.teardown(workspace)


def test_provenance_fails_when_the_stack_is_missing_an_approved_request(
    repository: StackRepository,
    builder: StackBuilder,
) -> None:
    """Containment, the first clause: nothing may be silently dropped."""

    workspace = builder.allocate(batch_id=_batch("b10"), base_sha=repository.base_sha)
    assert not isinstance(workspace, StackUnbuildable)
    built = builder.build(workspace, [subject_for(repository, "alpha")])
    assert isinstance(built, StackBuilt)

    verdict = builder.verify_provenance(
        workspace,
        [subject_for(repository, "alpha"), subject_for(repository, "beta")],
        tip_sha=built.tip_sha,
    )

    assert isinstance(verdict, ProvenanceBroken)
    assert verdict.unreachable_requests == ("req-beta",)
    assert "does not contain approved request" in verdict.describe()
    builder.teardown(workspace)


# ---------------------------------------------------------------------------
# The worktree
# ---------------------------------------------------------------------------


def test_teardown_leaves_no_worktree_and_no_ref(
    repository: StackRepository,
    builder: StackBuilder,
) -> None:
    """Detached rather than `-b`, so an abandoned attempt leaks no ref at all."""

    workspace = builder.allocate(batch_id=_batch("b11"), base_sha=repository.base_sha)
    assert not isinstance(workspace, StackUnbuildable)
    assert workspace.path.exists()
    assert str(workspace.path.resolve()) in worktree_paths(repository.path)

    assert builder.teardown(workspace) is None
    assert not workspace.path.exists()
    _assert_target_untouched(repository)


def test_teardown_recovers_from_a_directory_git_has_lost_track_of(
    repository: StackRepository,
    builder: StackBuilder,
) -> None:
    """`git worktree list` must be clean after every run, including the bad ones.

    A worktree whose administrative entry was pruned out from under it is
    removable by neither `git worktree remove` nor by doing nothing, which is
    why the executor already carries the `shutil.rmtree` fallback this reuses.
    """

    workspace = builder.allocate(batch_id=_batch("b12"), base_sha=repository.base_sha)
    assert not isinstance(workspace, StackUnbuildable)
    (workspace.path / ".git").unlink()

    builder.teardown(workspace)

    assert not workspace.path.exists()
    _assert_target_untouched(repository)


def test_allocating_over_a_directory_a_dead_run_left_behind_succeeds(
    repository: StackRepository,
    builder: StackBuilder,
) -> None:
    """A refinery that died between allocation and teardown must not wedge itself.

    Safe to clear precisely because nothing here ever advanced a ref: an
    unfinished attempt can always be redone from scratch, which is the same
    argument `recover_in_flight_requests` makes for the rows.
    """

    first = builder.allocate(batch_id=_batch("b13"), base_sha=repository.base_sha)
    assert not isinstance(first, StackUnbuildable)

    second = builder.allocate(batch_id=_batch("b13"), base_sha=repository.base_sha)

    assert not isinstance(second, StackUnbuildable)
    assert second.path == first.path
    built = builder.build(second, [subject_for(repository, "alpha")])
    assert isinstance(built, StackBuilt)
    builder.teardown(second)
    _assert_target_untouched(repository)


def test_allocating_from_a_sha_the_repository_does_not_have_is_not_a_conflict(
    repository: StackRepository,
    builder: StackBuilder,
) -> None:
    """Nobody is parked for it, because it is not about anyone's diff."""

    outcome = builder.allocate(
        batch_id=_batch("b14"),
        base_sha="0123456789abcdef0123456789abcdef01234567",
    )

    assert isinstance(outcome, StackUnbuildable)
    _assert_target_untouched(repository)


# ---------------------------------------------------------------------------
# Reads the fast-forward will need
# ---------------------------------------------------------------------------


def test_the_integrated_tip_is_read_from_the_declared_branch(
    repository: StackRepository,
    builder: StackBuilder,
) -> None:
    assert builder.integrated_tip("main") == repository.base_sha


def test_a_checkout_reports_its_branch_and_its_dirtiness(
    repository: StackRepository,
    builder: StackBuilder,
) -> None:
    """Both are milestone 4's refusals, and both are facts about the operator.

    Read here because milestone 3 is what puts them in the builder, and a probe
    nothing has ever called is a probe nobody knows is wrong.
    """

    assert builder.checked_out_branch() == "main"
    assert builder.working_tree_is_dirty() is False

    (repository.path / "README.md").write_text("edited by a human\n", encoding="utf-8")
    assert builder.working_tree_is_dirty() is True

    git(repository.path, "switch", "agent/alpha")
    assert builder.checked_out_branch() == "agent/alpha"


def test_an_untracked_file_is_not_dirtiness(
    repository: StackRepository,
    builder: StackBuilder,
) -> None:
    """It cannot block a fast-forward, so treating it as dirt would park work
    because somebody left a scratch file lying around."""

    (repository.path / "scratch.txt").write_text("notes\n", encoding="utf-8")

    assert builder.working_tree_is_dirty() is False
