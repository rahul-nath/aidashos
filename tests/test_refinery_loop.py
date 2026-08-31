# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The refinery as a running process: a real ledger, a real repository, real rows.

The load-bearing proof is not the returned value. A green project gate advances
the declared branch to the exact verified tip and records every request as
integrated. A red gate, dirty target checkout, or undecidable Git operation does
not advance it. Every case therefore ends by inspecting both Git and the durable
queue.

A merge conflict is a durable verdict rather than a placeholder: the requests
ahead of it applied cleanly by construction, so the culprit is attributable
without interpreting Git's prose.

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
from local_first_agent_os.refinery.bisect import RunAbandoned, RunCompleted, StackAbandonment
from local_first_agent_os.refinery.loop import (
    Drained,
    Idle,
    Refinery,
    run_refinery,
    run_refinery_fleet,
)
from local_first_agent_os.refinery.requests import (
    BisectedOut,
    GateFailed,
    InFlight,
    Integrated,
    IntegrationAttemptId,
    IntegrationBatchId,
    MergeConflict,
    Queued,
)
from local_first_agent_os.refinery.stack import (
    GitFailure,
    SourceWorktreePreserved,
    StackBuilder,
)
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
            c.execute(
                "UPDATE approval_requests "
                "SET status='APPROVED', resolved_by='test-operator', resolved_at=? "
                "WHERE approval_id=?",
                (1_700_000_000.0, str(approval["approval_id"])),
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


def test_a_clean_stack_is_verified_landed_and_recorded(
    refinery: Refinery,
    repository: StackRepository,
) -> None:
    """A green combination advances once and makes every request durable."""

    _enqueue(repository, "alpha", "beta")

    poll = refinery.poll_once()

    assert isinstance(poll, Drained)
    outcome = poll.run.outcome
    assert isinstance(outcome, RunCompleted)
    assert outcome.integrated == ("req-alpha", "req-beta")
    assert outcome.isolated == ()

    rows = _requests()
    assert all(isinstance(request, Integrated) for request in rows.values())
    landed = git(repository.path, "rev-parse", "refs/heads/main")
    assert landed != repository.base_sha
    assert all(
        git(repository.path, "merge-base", "--is-ancestor", repository.sha(name), landed) == ""
        for name in ("alpha", "beta")
    )
    assert worktree_paths(repository.path) == (str(repository.path.resolve()),)


def test_a_landed_run_drains_immediately_then_sleeps_on_empty(
    refinery: Refinery,
    repository: StackRepository,
) -> None:
    """Work drains immediately; only the following empty poll sleeps."""

    _enqueue(repository, "alpha")
    sleep = _RecordingSleep()

    polls = refinery.drain(interval_seconds=3.0, max_polls=3, sleep=sleep)

    assert len(polls) == 3
    assert sleep.intervals == [3.0]
    assert isinstance(_requests()["req-alpha"], Integrated)


# ---------------------------------------------------------------------------
# A conflict, which is a real verdict
# ---------------------------------------------------------------------------


def test_a_conflicting_request_is_parked_durably_and_the_rest_land(
    refinery: Refinery,
    repository: StackRepository,
) -> None:
    """The row keeps the branch, the commit, the base, the cause, and the
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

    assert isinstance(rows["req-alpha"], Integrated)
    assert isinstance(rows["req-beta"], Integrated)
    assert git(repository.path, "rev-parse", "refs/heads/main") != repository.base_sha


def test_a_run_that_parked_someone_polls_again_without_sleeping(
    refinery: Refinery,
    repository: StackRepository,
) -> None:
    """Work happened, so siblings may have arrived while it did."""

    _enqueue(repository, "alpha", "conflicting")
    sleep = _RecordingSleep()

    polls = refinery.drain(interval_seconds=3.0, max_polls=2, sleep=sleep)

    assert len(polls) == 2
    # The first poll parks the conflict and lands the remainder, then the empty
    # second poll is the final permitted poll.
    assert sleep.intervals == []
    assert isinstance(_requests()["req-alpha"], Integrated)


def test_the_parked_request_stays_parked_across_later_polls(
    refinery: Refinery,
    repository: StackRepository,
) -> None:
    """`BisectedOut` is terminal, so no later run may re-judge or re-queue it."""

    _enqueue(repository, "alpha", "conflicting")
    refinery.drain(interval_seconds=0.0, max_polls=3, sleep=lambda _: None)

    parked = _requests()["req-conflicting"]

    assert isinstance(parked, BisectedOut)
    assert isinstance(_requests()["req-alpha"], Integrated)


def test_a_red_project_gate_parks_the_request_and_leaves_main_unchanged(
    repository: StackRepository,
    tmp_path: Path,
) -> None:
    project = LinkedProject(
        id=_PROJECT,
        kind="test_repo",
        path=repository.path,
        status="active",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="red gate fixture",
        verification_commands=["printf 'combination refused' >&2; exit 7"],
        integrated_branch="main",
    )
    refinery = Refinery(
        _PROJECT,
        project=project,
        builder=StackBuilder(repository.path, tmp_path / "worktrees"),
        clock=lambda: 1_700_000_000.0,
    )
    _enqueue(repository, "alpha")

    poll = refinery.poll_once()

    assert isinstance(poll, Drained)
    parked = _requests()["req-alpha"]
    assert isinstance(parked, BisectedOut)
    assert isinstance(parked.cause, GateFailed)
    assert parked.cause.exit_code == 7
    assert "combination refused" in parked.cause.output_excerpt
    _assert_branch_never_advanced(repository)


def test_a_dirty_target_checkout_refuses_fast_forward_and_requeues(
    refinery: Refinery,
    repository: StackRepository,
) -> None:
    _enqueue(repository, "alpha")
    (repository.path / "README.md").write_text("operator edit\n", encoding="utf-8")

    poll = refinery.poll_once()

    assert isinstance(poll, Drained)
    assert isinstance(poll.run.outcome, RunAbandoned)
    assert poll.run.outcome.reason is StackAbandonment.FAST_FORWARD_REFUSED


def test_refinery_rechecks_live_approval_before_fast_forward(
    refinery: Refinery,
    repository: StackRepository,
) -> None:
    """A stale queued row cannot turn a no-longer-approved commit into authority."""

    _enqueue(repository, "alpha")
    with connect() as connection:
        connection.execute("UPDATE approval_requests SET status='REVOKED' WHERE status='APPROVED'")

    poll = refinery.poll_once()

    assert isinstance(poll, Drained)
    assert isinstance(poll.run.outcome, RunAbandoned)
    assert poll.run.outcome.reason is StackAbandonment.APPROVAL_REVOKED
    _assert_branch_never_advanced(repository)
    assert isinstance(_requests()["req-alpha"], Queued)
    assert git(repository.path, "rev-parse", "refs/heads/main") == repository.base_sha


def test_a_green_merge_removes_the_clean_execution_worktree_but_keeps_its_branch(
    refinery: Refinery,
    repository: StackRepository,
    tmp_path: Path,
) -> None:
    source = tmp_path / "agent-alpha-worktree"
    git(repository.path, "worktree", "add", str(source), "agent/alpha")
    _enqueue(repository, "alpha")

    poll = refinery.poll_once()

    assert isinstance(poll, Drained)
    assert not source.exists()
    assert "agent/alpha" in branch_names(repository.path)
    assert poll.run.source_worktree_cleanup
    assert type(poll.run.source_worktree_cleanup[0][1]).__name__ == "SourceWorktreeRemoved"


def test_source_worktree_inspection_failure_is_a_nonfatal_cleanup_report(
    repository: StackRepository,
    tmp_path: Path,
    monkeypatch,
) -> None:
    builder = StackBuilder(repository.path, tmp_path / "worktrees")
    original_must = StackBuilder._must

    def fail_listing(self, args, cwd):
        if tuple(args) == ("worktree", "list", "--porcelain"):
            raise GitFailure(tuple(args), 1, "simulated read failure")
        return original_must(self, args, cwd)

    monkeypatch.setattr(StackBuilder, "_must", fail_listing)

    cleanup = builder.cleanup_source_worktree(
        branch_name="agent/alpha",
        approved_tip_sha=repository.sha("alpha"),
    )

    assert isinstance(cleanup, SourceWorktreePreserved)
    assert "simulated read failure" in cleanup.detail


def test_replay_after_git_lands_but_the_ledger_transaction_rolls_back(
    refinery: Refinery,
    repository: StackRepository,
    monkeypatch,
) -> None:
    """The unavoidable Git/Postgres crash window is an idempotent replay.

    Git cannot join the ledger transaction. If the process dies after the exact
    fast-forward, Postgres rolls the claim back to QUEUED. Replaying sees the
    approved commit already below main, verifies the unchanged combination, and
    records it without creating a duplicate commit.
    """

    import local_first_agent_os.refinery.driver as driver_module

    _enqueue(repository, "alpha")
    original_record_integrated = driver_module.record_integrated

    def simulate_process_death(*args, **kwargs):
        raise RuntimeError("simulated death after fast-forward")

    monkeypatch.setattr(driver_module, "record_integrated", simulate_process_death)

    with pytest.raises(RuntimeError, match="simulated death after fast-forward"):
        refinery.poll_once()

    landed_before_replay = git(repository.path, "rev-parse", "refs/heads/main")
    assert landed_before_replay == repository.sha("alpha")
    assert isinstance(_requests()["req-alpha"], Queued)

    monkeypatch.setattr(driver_module, "record_integrated", original_record_integrated)
    replay = refinery.poll_once()

    assert isinstance(replay, Drained)
    assert isinstance(_requests()["req-alpha"], Integrated)
    assert git(repository.path, "rev-parse", "refs/heads/main") == landed_before_replay


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
    assert isinstance(_requests()["req-alpha"], Integrated)
    assert git(repository.path, "rev-parse", "refs/heads/main") == repository.sha("alpha")


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


def test_the_command_reports_the_actual_branch_advance(
    registered_project: StackRepository,
) -> None:
    """The coordination result reports the repository fact, not a static note."""

    _enqueue(registered_project, "alpha")

    result = run_refinery(_PROJECT, interval_seconds=0.0, max_polls=1)

    assert result["ok"] is True
    assert result["advanced_the_integrated_branch"] is True
    assert result["integrated_branch"] == "main"
    assert result["polls"][0]["outcome"] == "drained"
    assert result["polls"][0]["integrated"] == ["req-alpha"]
    assert git(registered_project.path, "rev-parse", "refs/heads/main") == registered_project.sha(
        "alpha"
    )


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
    assert result["polls"][0]["integrated"] == ["req-alpha"]


def test_the_resident_fleet_requires_an_explicit_project_allowlist(
    registered_project: StackRepository,
) -> None:
    result = run_refinery_fleet((), interval_seconds=0.0, max_polls=1)

    assert result["ok"] is False
    assert result["error"] == "refinery_fleet_empty"


def test_the_resident_fleet_drains_only_the_named_project(
    registered_project: StackRepository,
) -> None:
    _enqueue(registered_project, "alpha")

    result = run_refinery_fleet((_PROJECT,), interval_seconds=0.0, max_polls=1)

    assert result["ok"] is True
    project_result = result["polls"][0]["projects"][0]
    assert project_result["target_project_id"] == _PROJECT
    assert project_result["advanced_the_integrated_branch"] is True
