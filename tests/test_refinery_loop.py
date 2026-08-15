# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The refinery as a running process: a real ledger, a real repository, real rows.

Milestone 3 builds the stack and stops, so the thing most worth pinning here is
what it does *not* do. The integrated branch is never advanced, and a suite that
only checked returned values would not notice the day it starts being advanced by
accident. Every case therefore ends by looking at the repository.

The one durable verdict this milestone produces is a parked merge conflict, and
that is a real verdict rather than a placeholder: the requests ahead of a
conflict applied cleanly by construction, so the culprit is attributable with no
gate run at all. Everything else returns to the queue.

Why the loop is driven rather than the driver
=============================================

`integrate_batch` is called through `Refinery.poll_once` and `Refinery.drain`
wherever a test can, because the defect this whole design was written against is
a mechanism nothing consults. A test that called the driver directly would pass
whether or not the loop ever called it, which is the same defect wearing a test's
hat.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from refinery_support import (
    StackRepository,
    branch_names,
    build_stack_repository,
    git,
    subject_for,
    worktree_paths,
    write_registry_config,
)

from local_first_agent_os.coordination.approvals import submit_approval_request
from local_first_agent_os.coordination.integration_queue import (
    claim_requests_for_attempt,
    read_integration_requests,
    record_queued_request,
)
from local_first_agent_os.coordination.projects import create_saga
from local_first_agent_os.coordination.resident_loop import (
    ResidentLoop,
    ResidentLoopHeld,
    describe_resident_loops,
    hold_resident_loop,
)
from local_first_agent_os.coordination.store import connect
from local_first_agent_os.project_access import AccessMode, ProjectAccessPolicy
from local_first_agent_os.project_center import LinkedProject
from local_first_agent_os.refinery.bisect import RunAbandoned, StackAbandonment
from local_first_agent_os.refinery.loop import Drained, Idle, Refinery, run_refinery
from local_first_agent_os.refinery.requests import (
    BisectedOut,
    InFlight,
    IntegrationAttemptId,
    IntegrationBatchId,
    MergeConflict,
    Queued,
)
from local_first_agent_os.refinery.stack import StackBuilder
from local_first_agent_os.settings import get_settings

_PROJECT = "target"

_BRANCHES = {
    "alpha": {"alpha.py": "ALPHA = 1\n"},
    "beta": {"beta.py": "BETA = 1\n"},
    "conflicting": {"alpha.py": "ALPHA = 999\n"},
}


@pytest.fixture
def repository(tmp_path: Path) -> StackRepository:
    return build_stack_repository(tmp_path / "target", _BRANCHES)


@pytest.fixture
def project(repository: StackRepository) -> LinkedProject:
    return LinkedProject(
        id=_PROJECT,
        kind="test_repo",
        path=repository.path,
        status="active",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="refinery loop fixture",
        verification_commands=["true"],
        integrated_branch="main",
    )


@pytest.fixture
def refinery(
    repository: StackRepository,
    project: LinkedProject,
    tmp_path: Path,
) -> Refinery:
    return Refinery(
        _PROJECT,
        project=project,
        builder=StackBuilder(
            repository_path=repository.path,
            worktree_root=tmp_path / "worktrees",
        ),
        clock=lambda: 1_700_000_000.0,
    )


class _RecordingSleep:
    """Stands in for `time.sleep` so the loop's pacing is assertable.

    The distinction being tested is not cosmetic. A run that decided something
    polls again at once, because the moment a batch finishes is when its siblings
    are most likely to have arrived. A run that decided nothing must sleep, or it
    rebuilds the same stack against the same unchanged condition as fast as the
    machine allows.
    """

    def __init__(self) -> None:
        self.intervals: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.intervals.append(seconds)


def _enqueue(repository: StackRepository, *names: str) -> None:
    """Put requests in the queue with real approvals behind them.

    `integration_requests.approval_id` is a foreign key to `approval_requests`,
    which is the schema saying what invariant 1 says: no commit reaches an
    integrated branch without a resolved `CODE_MERGE` approval binding it. The
    approvals are submitted rather than faked so the rows here are the rows
    production writes.
    """

    saga_id = str(create_saga("Land approved agent branches")["saga_id"])
    with connect() as c:
        for index, name in enumerate(names):
            approval = submit_approval_request(
                saga_id,
                "CODE_MERGE",
                payload={
                    "target_project_id": _PROJECT,
                    "branch": f"agent/{name}",
                    "base_sha": repository.base_sha,
                    "commit_sha": repository.sha(name),
                    "intent_id": f"intent-{name}",
                    "pow_wow_id": f"pow-{name}",
                },
                requested_by="dispatcher_runner",
            )
            record_queued_request(
                c,
                Queued(
                    subject=subject_for(
                        repository,
                        name,
                        approval_id=str(approval["approval_id"]),
                        enqueued_at=1_700_000_000.0 + index,
                    )
                ),
                recorded_at=1_700_000_000.0,
            )


def _requests() -> dict[str, Any]:
    with connect() as c:
        return {
            request.subject.request_id: request
            for request in read_integration_requests(c, target_project_id=_PROJECT)
        }


def _assert_branch_never_advanced(repository: StackRepository) -> None:
    """Milestone 3's whole promise, and the one worth checking after every case."""

    assert git(repository.path, "rev-parse", "refs/heads/main") == repository.base_sha
    assert git(repository.path, "status", "--porcelain") == ""
    assert worktree_paths(repository.path) == (str(repository.path.resolve()),)
    assert branch_names(repository.path) == (
        "agent/alpha",
        "agent/beta",
        "agent/conflicting",
        "main",
    )


# ---------------------------------------------------------------------------
# The layering that makes the package importable
# ---------------------------------------------------------------------------


def test_the_refinery_package_does_not_import_its_own_driver_or_loop() -> None:
    """A cycle that only fires depending on which module was imported first.

    `coordination/integration_queue.py` imports `refinery.enqueue`, so importing
    the ledger executes `refinery/__init__`. When that file also imported
    `driver`, which imports the ledger back, the result was an `ImportError`
    naming a partially initialized module - but only for a process that reached
    the ledger first. Every refinery test passed in isolation and the whole suite
    failed to collect.

    The rule is the layering: rules below, rows in the middle, driver and loop
    above. This is the cheapest place to have that argument, and importing the
    ledger before anything else is the reproduction.
    """

    import importlib
    import subprocess
    import sys

    source = importlib.import_module("local_first_agent_os.refinery").__doc__ or ""
    assert "deliberately **not** re-exported" in source

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import local_first_agent_os.coordination.integration_queue as q;"
            "import local_first_agent_os.refinery.loop as l;"
            "import local_first_agent_os.refinery as r;"
            "assert 'integrate_batch' not in r.__all__;"
            "assert 'run_refinery' not in r.__all__;"
            "print('ok')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "ok"


# ---------------------------------------------------------------------------
# The empty queue
# ---------------------------------------------------------------------------


def test_an_empty_queue_allocates_nothing_and_writes_nothing(
    refinery: Refinery,
    repository: StackRepository,
) -> None:
    """A batch of zero is not a batch, and this is where that is load-bearing."""

    poll = refinery.poll_once()

    assert isinstance(poll, Idle)
    assert _requests() == {}
    _assert_branch_never_advanced(repository)


def test_an_empty_queue_sleeps_for_the_configured_interval(refinery: Refinery) -> None:
    sleep = _RecordingSleep()

    refinery.drain(interval_seconds=7.0, max_polls=2, sleep=sleep)

    assert sleep.intervals == [7.0]


def test_the_poll_interval_defaults_to_the_setting(refinery: Refinery) -> None:
    """Fifteen seconds, and it is a setting because the right value depends on
    how long somebody else's verification takes."""

    sleep = _RecordingSleep()

    refinery.drain(max_polls=2, sleep=sleep)

    assert sleep.intervals == [get_settings().refinery_poll_seconds]


# ---------------------------------------------------------------------------
# A stack that builds
# ---------------------------------------------------------------------------


def test_a_clean_stack_is_built_and_deliberately_not_landed(
    refinery: Refinery,
    repository: StackRepository,
) -> None:
    """The milestone boundary, stated as an assertion rather than as a comment.

    Milestone 4 replaces `INTEGRATED_BRANCH_ADVANCE_UNIMPLEMENTED` with the gate
    and the fast-forward. Until then, a stack that builds and proves clean still
    lands nothing, and its members go back to the queue rather than being parked:
    nothing is wrong with them.
    """

    _enqueue(repository, "alpha", "beta")

    poll = refinery.poll_once()

    assert isinstance(poll, Drained)
    outcome = poll.run.outcome
    # `RunAbandoned` rather than `RunCompleted` is the assertion: a completed run
    # has no reason and nothing returned, because it decided everybody.
    assert isinstance(outcome, RunAbandoned)
    assert outcome.integrated == ()
    assert outcome.isolated == ()
    assert outcome.reason is StackAbandonment.INTEGRATED_BRANCH_ADVANCE_UNIMPLEMENTED
    assert set(outcome.returned_to_queue) == {"req-alpha", "req-beta"}

    assert all(isinstance(request, Queued) for request in _requests().values())
    _assert_branch_never_advanced(repository)


def test_a_run_that_decided_nothing_sleeps_rather_than_spinning(
    refinery: Refinery,
    repository: StackRepository,
) -> None:
    """The hot-loop guard.

    "Do not sleep after doing work" is the design's rule, and an abandoned run
    did none: the batch went back to `Queued` unchanged and the next poll would
    rebuild the identical stack. In milestone 3 every clean stack abandons, so
    without this the loop would peg a core on a queue with one request in it.
    """

    _enqueue(repository, "alpha")
    sleep = _RecordingSleep()

    polls = refinery.drain(interval_seconds=3.0, max_polls=3, sleep=sleep)

    assert len(polls) == 3
    assert sleep.intervals == [3.0, 3.0]
    _assert_branch_never_advanced(repository)


# ---------------------------------------------------------------------------
# A conflict, which is a real verdict
# ---------------------------------------------------------------------------


def test_a_conflicting_request_is_parked_durably_and_the_rest_go_back(
    refinery: Refinery,
    repository: StackRepository,
) -> None:
    """One gate run is not spent, because no gate exists to spend yet.

    The row keeps the branch, the commit, the base, the cause, and the
    combination that refused it, because the remedy an operator is offered is a
    bounded revision against that base and none of it is reconstructable from
    "did not land".
    """

    _enqueue(repository, "alpha", "conflicting", "beta")

    poll = refinery.poll_once()

    assert isinstance(poll, Drained)
    assert [isolation.request_id for isolation in poll.run.outcome.isolated] == ["req-conflicting"]

    rows = _requests()
    parked = rows["req-conflicting"]
    assert isinstance(parked, BisectedOut)
    assert isinstance(parked.cause, MergeConflict)
    assert parked.cause.conflicted_paths == ("alpha.py",)
    assert parked.stack_base_sha == repository.base_sha
    assert parked.stack_beneath == ("req-alpha",)
    assert parked.subject.commit_sha == repository.sha("conflicting")

    assert isinstance(rows["req-alpha"], Queued)
    assert isinstance(rows["req-beta"], Queued)
    _assert_branch_never_advanced(repository)


def test_a_run_that_parked_someone_polls_again_without_sleeping(
    refinery: Refinery,
    repository: StackRepository,
) -> None:
    """Work happened, so siblings may have arrived while it did."""

    _enqueue(repository, "alpha", "conflicting")
    sleep = _RecordingSleep()

    polls = refinery.drain(interval_seconds=3.0, max_polls=2, sleep=sleep)

    assert len(polls) == 2
    # The first poll parked the conflict and went straight round again; the
    # second built a clean stack, decided nothing, and would have slept had it
    # not been the last permitted poll.
    assert sleep.intervals == []
    _assert_branch_never_advanced(repository)


def test_the_parked_request_stays_parked_across_later_polls(
    refinery: Refinery,
    repository: StackRepository,
) -> None:
    """`BisectedOut` is terminal, so no later run may re-judge or re-queue it."""

    _enqueue(repository, "alpha", "conflicting")
    refinery.drain(interval_seconds=0.0, max_polls=3, sleep=lambda _: None)

    parked = _requests()["req-conflicting"]

    assert isinstance(parked, BisectedOut)
    _assert_branch_never_advanced(repository)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


def test_a_refinery_killed_mid_attempt_leaves_rows_restart_returns_to_queued(
    refinery: Refinery,
    repository: StackRepository,
) -> None:
    """The invariants are what make this safe.

    Nothing advances the integrated branch unless a gate went green, so an
    unfinished attempt is always redoable from scratch and the row is the whole
    question. Nothing here inspects git.
    """

    _enqueue(repository, "alpha")
    with connect() as c:
        claim_requests_for_attempt(
            c,
            [
                r
                for r in read_integration_requests(c, target_project_id=_PROJECT)
                if isinstance(r, Queued)
            ],
            batch_id=IntegrationBatchId("ib_dead"),
            attempt_id=IntegrationAttemptId("ia_dead"),
            recorded_at=1_700_000_000.0,
        )
    assert isinstance(_requests()["req-alpha"], InFlight)

    recovered = refinery.recover()

    assert recovered == ("req-alpha",)
    assert isinstance(_requests()["req-alpha"], Queued)
    _assert_branch_never_advanced(repository)


def test_selection_crashes_on_an_outstanding_attempt_rather_than_skipping_it(
    refinery: Refinery,
    repository: StackRepository,
) -> None:
    """Recovery is a precondition, not optional cleanup.

    Skipping the row instead would build a second stack on a base the first
    refinery was about to invalidate, and one of the two would silently lose its
    batch. `drain` calls `recover` first; `poll_once` deliberately does not, so
    the crash is reachable and this says so.
    """

    _enqueue(repository, "alpha")
    with connect() as c:
        claim_requests_for_attempt(
            c,
            [
                r
                for r in read_integration_requests(c, target_project_id=_PROJECT)
                if isinstance(r, Queued)
            ],
            batch_id=IntegrationBatchId("ib_dead"),
            attempt_id=IntegrationAttemptId("ia_dead"),
            recorded_at=1_700_000_000.0,
        )

    with pytest.raises(ValueError, match="recover outstanding attempts"):
        refinery.poll_once()


def test_drain_recovers_before_it_selects(
    refinery: Refinery,
    repository: StackRepository,
) -> None:
    _enqueue(repository, "alpha")
    with connect() as c:
        claim_requests_for_attempt(
            c,
            [
                r
                for r in read_integration_requests(c, target_project_id=_PROJECT)
                if isinstance(r, Queued)
            ],
            batch_id=IntegrationBatchId("ib_dead"),
            attempt_id=IntegrationAttemptId("ia_dead"),
            recorded_at=1_700_000_000.0,
        )

    polls = refinery.drain(interval_seconds=0.0, max_polls=1, sleep=lambda _: None)

    assert len(polls) == 1
    assert isinstance(polls[0], Drained)
    _assert_branch_never_advanced(repository)


# ---------------------------------------------------------------------------
# One refinery per project
# ---------------------------------------------------------------------------


def test_a_second_refinery_on_one_project_is_refused_rather_than_racing() -> None:
    """Two would each compute a fast-forward from a base the other was about to
    invalidate, and one would silently lose a batch."""

    with hold_resident_loop(ResidentLoop.REFINERY, scope=_PROJECT):
        result = run_refinery(_PROJECT, max_polls=1)

    assert result["ok"] is False
    assert result["error"] == "resident_loop_busy"
    assert result["target_project_id"] == _PROJECT


def test_two_projects_refine_concurrently_because_the_lock_is_per_project() -> None:
    """Serializing them would make a slow project's test suite the pacing item
    for every other project on the machine."""

    with (
        hold_resident_loop(ResidentLoop.REFINERY, scope="project_one") as first,
        hold_resident_loop(ResidentLoop.REFINERY, scope="project_two") as second,
    ):
        assert isinstance(first, ResidentLoopHeld)
        assert isinstance(second, ResidentLoopHeld)


def test_the_refinery_is_reported_as_scoped_rather_than_as_unowned() -> None:
    """`owned: false` on the unscoped key says nothing about running refineries.

    A machine draining three projects holds three scoped locks and no unscoped
    one, so a report that listed the refinery beside the two singleton loops
    without saying so would be a confident lie on the operator's terminal.
    """

    entries = {entry["loop"]: entry for entry in describe_resident_loops()["loops"]}

    assert entries[ResidentLoop.REFINERY.value]["scoped_by"] == "target_project_id"
    assert entries[ResidentLoop.ENQUEUE_DRAINER.value]["scoped_by"] is None


# ---------------------------------------------------------------------------
# The coordination verb an operator actually types
# ---------------------------------------------------------------------------


@pytest.fixture
def registered_project(
    repository: StackRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[StackRepository]:
    """The ambient registry, because that is how the coordination layer reaches it.

    A command arrives as argv with no `Settings` attached, so
    `LOCAL_AGENT_CONFIG_DIR` is the seam and a test that injected the project
    would be exercising a path production does not take.
    """

    write_registry_config(tmp_path / "configs", repository.path, project_id=_PROJECT)
    monkeypatch.setenv("LOCAL_AGENT_CONFIG_DIR", str(tmp_path / "configs"))
    monkeypatch.setenv("LOCAL_AGENT_SAGA_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    get_settings.cache_clear()
    yield repository
    get_settings.cache_clear()


def test_the_command_reports_that_it_landed_nothing(
    registered_project: StackRepository,
) -> None:
    """An operator reading this must not conclude the merge is automatic now.

    The queue's first milestone made `/approve-merge` say plainly that no run
    drains the queue. This is the same obligation one step along: a run happened,
    it verified the stack, and it still did not land anything.
    """

    _enqueue(registered_project, "alpha")

    result = run_refinery(_PROJECT, interval_seconds=0.0, max_polls=1)

    assert result["ok"] is True
    assert result["advanced_the_integrated_branch"] is False
    assert result["integrated_branch"] == "main"
    assert "never advances the integrated branch" in result["note"]
    assert result["polls"][0]["outcome"] == "drained"
    assert (
        result["polls"][0]["abandoned_because"]
        == StackAbandonment.INTEGRATED_BRANCH_ADVANCE_UNIMPLEMENTED.value
    )
    _assert_branch_never_advanced(registered_project)


def test_the_command_names_the_parked_request_and_what_refused_it(
    registered_project: StackRepository,
) -> None:
    _enqueue(registered_project, "alpha", "conflicting")

    result = run_refinery(_PROJECT, interval_seconds=0.0, max_polls=1)

    assert result["ok"] is True
    assert result["polls"][0]["parked"] == [
        {
            "request_id": "req-conflicting",
            "cause": "MergeConflict",
            "stack_beneath": ["req-alpha"],
        }
    ]
    _assert_branch_never_advanced(registered_project)
