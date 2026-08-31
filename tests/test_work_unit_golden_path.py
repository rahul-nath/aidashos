# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A WorkUnit driven from a document to SUCCEEDED.

``tests/conftest.py`` pins ``LOCAL_AGENT_USE_DBOS=false`` before the package is
imported, because ``@dbos_step`` and ``@dbos_workflow`` bind at import time. So
the ordinary suite has never run a real DBOS workflow, every green WorkUnit trace
in it went through the simulated runtime, and none of them ever submitted a
dispatch intent. Every defect in the 2026-08-04 handoffs lived in that gap.

Two lanes, for two different reasons:

- ``test_the_golden_path_runs_through_the_resident_loops`` starts the enqueue
  drainer and the ledger dispatcher as **real subprocesses** against disposable
  databases, exactly the way ``scripts/start-agent-runtime.sh`` does. That is the
  only shape where "the production resident constructors" is literally true, and
  the only one that exercises DBOS's cross-process notification path. It is gated
  on ``LOCAL_AGENT_RUN_POSTGRES_INTEGRATION=1``, which is what
  ``scripts/run_dbos_postgres_smoke.sh`` already exports.
- everything else is ledger semantics, and runs in the ordinary lane against the
  per-test Postgres schema.

The design doc is ``docs/examples/work_unit_golden_path_design_doc.md`` rather
than the acceptance one, because the acceptance document's IMPLEMENT milestones
require ``source_patch``, which the evidence gate grants only for non-empty
``changed_files``. A bounded advisory turn cannot honestly produce that, and
making the gate accept prose would be deleting the check to pass the test.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
import pytest
from postgres_support import SKIP_UNLESS_INTEGRATION, postgres_admin_url
from psycopg import sql
from pytest_bdd import given, parsers, scenarios, then, when
from work_unit_support import compile_acceptance_doc

from local_first_agent_os.contracts import DispatchIntentStatus
from local_first_agent_os.coordination import DispatchKind
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units import service
from local_first_agent_os.work_units.crash_recovery_loop import CrashReconciler
from local_first_agent_os.work_units.events import (
    DispatchIntentCreated,
    MilestoneTransition,
    WorkUnitTransition,
)
from local_first_agent_os.work_units.execution import (
    DispatchBackedExecutorRuntime,
    DispatchParked,
    DispatchSettled,
)
from local_first_agent_os.work_units.execution_recovery import execution_workflow_id
from local_first_agent_os.work_units.executors import _BOUNDED_RETRY
from local_first_agent_os.work_units.lifecycle import (
    FailureClass,
    LifecyclePhase,
    MilestoneExecutionStatus,
    WorkUnitStatus,
)
from local_first_agent_os.work_units.root_workflow import EnqueueDelivery

scenarios("features/work_unit_golden_path.feature")


REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH_DOC = REPO_ROOT / "docs" / "examples" / "work_unit_golden_path_design_doc.md"
FIRST_MILESTONE = "a"
REVIEW_MILESTONE = "b"


# --------------------------------------------------------------------------- #
# The full drive, through real resident processes
# --------------------------------------------------------------------------- #


@contextmanager
def _disposable_databases() -> Iterator[tuple[str, str]]:
    """A coordination database and a DBOS system database, both dropped after.

    Two, not one: DBOS owns its own system database and the smoke script keeps
    them apart for the same reason this does - a run that can write to
    `local_agent` is the bug that script was rewritten for.
    """

    admin_url = postgres_admin_url()
    suffix = uuid.uuid4().hex[:12]
    coordination = f"golden_path_{suffix}"
    dbos_system = f"golden_path_dbos_{suffix}"
    base = admin_url.rsplit("/", 1)[0]
    with psycopg.connect(admin_url, autocommit=True) as connection:
        for name in (coordination, dbos_system):
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    # SQLAlchemy-style, the way `run_dbos_postgres_smoke.sh` writes them. A plain
    # `postgresql://` makes SQLAlchemy reach for psycopg2, which this project does
    # not depend on, and the resident loop exits with `ModuleNotFoundError` in its
    # first second - having printed a JSON error nothing was reading.
    driver = f"postgresql+psycopg://{base.split('://', 1)[1]}"
    try:
        yield f"{driver}/{coordination}", f"{driver}/{dbos_system}"
    finally:
        with psycopg.connect(admin_url, autocommit=True) as connection:
            for name in (coordination, dbos_system):
                connection.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
                )


def _golden_path_config_dir(root: Path, target: Path) -> Path:
    """The config a resident dispatcher actually reads.

    `load_project_center` raises `FileNotFoundError` without
    `linked_projects.toml`, and `DispatcherIntentRunner.__init__` loads
    `staffing.toml`. Both are production requirements rather than test scaffolding,
    which is why they are written here rather than stubbed.
    """

    config_dir = root / "configs"
    config_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "pi_prompts.toml",
        "model_quality.toml",
        "model_registry.toml",
        "workspace_policies.toml",
    ):
        source = REPO_ROOT / "configs" / name
        if source.exists():
            shutil.copy(source, config_dir / name)
    # Every tier on the local harness, deliberately. The bench is the single
    # source of truth for tier -> runtime, so this is a configuration an operator
    # can write rather than a stub the test injects - and it is the configuration
    # of an operator with no frontier subscription.
    #
    # It is also the property this test exists to prove. A plan milestone
    # dispatches at senior tier, and before the routing fix a senior slot naming
    # `pi` was launched as `claude --model gemma4`. Staffing every tier locally is
    # what makes this drive prove the fix instead of spending somebody's
    # subscription.
    #
    # Two different local models, because a shared seat is unrepresentable and
    # this bench has two. `glimmer` is the registry's `deliberator`, "the first
    # entry in this file whose model is chosen for judgment rather than for
    # speed", so it holds the critic seat - the same shape the frontier seating
    # uses when it puts the better critic on review.
    #
    # This is why the fixture copies `model_registry.toml` above: both server
    # names have to resolve to a role the ModelManager routes, and
    # `role_for_server_name` is what does it.
    (config_dir / "staffing.toml").write_text(
        """
seated_pairing = "all-local"

[pairings.all-local.senior]
harness = "pi"
model = "gemma4"
capacity = 2

[pairings.all-local.staff]
harness = "pi"
model = "glimmer"
capacity = 1

[bench.junior]
harness = "pi"
model = "gemma4"
capacity = 4
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (config_dir / "linked_projects.toml").write_text(
        f"""
[center]
id = "local-first-agent-os"
description = "golden path center"
control_plane_project = "local-first-agent-os"
default_saga_project = "local-first-agent-os"
default_memory_project = "local-first-agent-os"

[[projects]]
id = "local-first-agent-os"
kind = "test_repo"
path = {json.dumps(str(target))}
status = "active"
read_only = false
description = "golden path target"
primary_interfaces = ["pytest"]
owns = ["tests"]
avoid = []
# A writable project must certify code with at least one verification command;
# the project center refuses the empty list on a writable target.
verification_commands = ["python3 -c 'raise SystemExit(0)'"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return config_dir


def _resident_env(
    *, coordination_url: str, dbos_url: str, root: Path, config_dir: Path
) -> dict[str, str]:
    """The environment `start-agent-runtime.sh` composes, pointed at throwaways."""

    return {
        **os.environ,
        "LOCAL_AGENT_USE_DBOS": "true",
        "LOCAL_AGENT_MOCK_MODELS": "true",
        "LOCAL_AGENT_COORDINATION_BACKEND": "postgres",
        "AGENT_COORDINATION_BACKEND": "postgres",
        "LOCAL_AGENT_COORDINATION_DATABASE_URL": coordination_url,
        "AGENT_COORDINATION_DATABASE_URL": coordination_url,
        "LOCAL_AGENT_DATABASE_URL": coordination_url,
        "LOCAL_AGENT_DBOS_SYSTEM_DATABASE_URL": dbos_url,
        "DBOS_SYSTEM_DATABASE_URL": dbos_url,
        "AGENT_COORDINATION_ROOT": str(root),
        "LOCAL_AGENT_COORDINATION_ROOT": str(root),
        "LOCAL_AGENT_CONFIG_DIR": str(config_dir),
        "LOCAL_AGENT_ARTIFACT_ROOT": str(root / "artifacts"),
        "LOCAL_AGENT_SPOOL_DIR": str(root / "spool"),
        # An unset schema means the database's own `public`, which is what a
        # whole disposable database wants. Inheriting the suite's per-test schema
        # name here fails as `InvalidSchemaName` on the first write.
        "AGENT_COORDINATION_SCHEMA": "",
    }


@contextmanager
def _resident_loops(env: dict[str, str]) -> Iterator[list[subprocess.Popen[bytes]]]:
    """Start the two loops the operator scripts start, and stop them after.

    `hold_resident_loop` releases its advisory lock when the connection closes, so
    SIGTERM is enough; nothing has to be unlocked by hand.
    """

    commands = [
        [
            sys.executable,
            str(REPO_ROOT / "agent_coordination_mcp.py"),
            "run_enqueue_drainer",
            "--interval-seconds",
            "1",
        ],
        [
            sys.executable,
            str(REPO_ROOT / "agent_coordination_mcp.py"),
            "run_ledger_dispatcher",
            "--interval-seconds",
            "1",
        ],
    ]
    processes = [
        subprocess.Popen(
            command, env=env, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        for command in commands
    ]
    try:
        _assert_still_running(processes)
        yield processes
    finally:
        for process in processes:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()


def _assert_still_running(processes: list[subprocess.Popen[bytes]]) -> None:
    """Fail now, with the loop's own words, if one of them has already exited.

    A resident loop that dies immediately still returns a well-formed `err`
    payload on stdout and exits zero, so nothing downstream notices. Without this
    the symptom is a 240-second timeout on a ledger nobody is draining.
    """

    time.sleep(3.0)
    for process in processes:
        if process.poll() is not None:
            raise AssertionError(
                f"resident loop {process.args} exited immediately "
                f"(code {process.returncode}):\n{_drain(process)}"
            )


def _drain(process: subprocess.Popen[bytes]) -> str:
    """Whatever a loop has written so far, without blocking on one still running.

    A resident loop that died silently is the failure most worth seeing here, and
    reading its pipe only after it exits is how that stays invisible.
    """

    if process.stdout is None:
        return ""
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    return process.stdout.read().decode("utf-8", "replace")[-6000:]


def _await(predicate: Any, *, timeout: float, what: str, diagnose: Any = None) -> Any:
    """Poll a durable predicate rather than sleeping a guessed interval.

    The resident loops are separate processes, so the only honest synchronisation
    is the ledger they both write to.
    """

    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"timed out after {timeout:.0f}s waiting for {what}"
                + ("\n\n" + diagnose() if diagnose else "")
            )
        time.sleep(0.5)


@SKIP_UNLESS_INTEGRATION
@pytest.mark.integration
@pytest.mark.lifecycle
def test_the_golden_path_runs_through_the_resident_loops(tmp_path: Path) -> None:
    """DesignDoc to SUCCEEDED, through the processes an operator actually starts.

    Every step here is a production constructor called the way the runtime calls
    it. Nothing is simulated except the model, and that only because
    `LOCAL_AGENT_MOCK_MODELS` is the repository's own way of saying "answer
    deterministically" - the junior still travels the real delegate path, the real
    adapter, and the real artifact store.
    """

    target = tmp_path / "target"
    target.mkdir()
    subprocess.run(("git", "init", "-q"), cwd=target, check=True)
    (target / "README.md").write_text("# golden path target\n", encoding="utf-8")
    subprocess.run(("git", "add", "README.md"), cwd=target, check=True)
    subprocess.run(
        (
            "git",
            "-c",
            "user.name=Golden Path",
            "-c",
            "user.email=golden-path@example.invalid",
            "commit",
            "-qm",
            "initial target",
        ),
        cwd=target,
        check=True,
    )
    config_dir = _golden_path_config_dir(tmp_path, target)

    with _disposable_databases() as (coordination_url, dbos_url):
        env = _resident_env(
            coordination_url=coordination_url,
            dbos_url=dbos_url,
            root=tmp_path,
            config_dir=config_dir,
        )

        def _coordination(*argv: str) -> dict[str, Any]:
            """One coordination command, in a process configured like the loops.

            The test process itself never imports DBOS - `conftest` pinned it off
            before any import - so the drive happens through the same CLI shim
            `start-agent-runtime.sh` invokes.
            """

            completed = subprocess.run(
                [sys.executable, str(REPO_ROOT / "agent_coordination_mcp.py"), *argv],
                env=env,
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr
            return json.loads(completed.stdout)

        def _in_process(script: str) -> None:
            """Run one snippet in a DBOS-launched process, as the API server is.

            The test process itself cannot: `conftest` pinned the flag off before
            any import, and the decorators have already bound.
            """

            completed = subprocess.run(
                [sys.executable, "-c", script],
                env=env,
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert completed.returncode == 0, completed.stdout + completed.stderr

        compiled = _coordination(
            "compile_design_doc", str(GOLDEN_PATH_DOC), "--design-doc-id", "golden_path"
        )
        revision_id = compiled["compiled_plan_revision_id"]
        assert compiled["runnable"] is True, compiled

        with _resident_loops(env) as processes:
            started = _coordination("start_work_unit", revision_id)
            work_unit_id = started["work_unit_id"]

            def _view() -> dict[str, Any]:
                return _coordination("get_work_unit", work_unit_id)["work_unit"]

            # The drainer hands it to DBOS, DBOS runs the root workflow, the
            # milestone submits an intent, and the dispatcher claims it.
            _await(
                lambda: _view()["status"] != WorkUnitStatus.QUEUED.value,
                timeout=120,
                what="the drainer to hand the WorkUnit to DBOS",
            )
            plan_milestone = _await(
                lambda: next(
                    (
                        item
                        for item in _view()["milestones"]
                        if item["stable_key"] == FIRST_MILESTONE and item.get("dispatch_intent_id")
                    ),
                    None,
                ),
                timeout=180,
                what="the plan milestone to name its dispatch intent",
                diagnose=lambda: "\n\n".join(_drain(process) for process in processes),
            )
            # The link task 2 made durable: the summary column, not just the event.
            assert plan_milestone["dispatch_intent_id"]

            def _why_stuck() -> str:
                """The ledger and the loops' own output, on the way out.

                A golden-path test that times out saying only that it timed out
                is the shape everything else in this session was about.
                """

                intents = _coordination("list_dispatch_intents").get("intents", [])
                events = _coordination("list_work_unit_events", work_unit_id)
                loops = [
                    {
                        "argv": process.args,
                        "exit_code": process.poll(),
                        "output": _drain(process),
                    }
                    for process in processes
                ]
                return json.dumps(
                    {
                        "intents": intents,
                        "milestones": _view()["milestones"],
                        "events": events.get("events", [])[-15:],
                        "loops": loops,
                    },
                    indent=2,
                    sort_keys=True,
                    default=str,
                )

            _await(
                lambda: next(
                    (
                        item
                        for item in _view()["milestones"]
                        if item["stable_key"] == FIRST_MILESTONE
                        and item["status"] == MilestoneExecutionStatus.SUCCEEDED.value
                    ),
                    None,
                ),
                timeout=240,
                what="the local junior delegate to settle the plan milestone",
                diagnose=_why_stuck,
            )
            artifacts = _coordination("get_work_unit", work_unit_id)["work_unit"]["artifacts"]
            assert any(item["artifact_type"] == "implementation_plan" for item in artifacts), (
                "the plan milestone must record its evidence, not merely succeed"
            )

            pending = _await(
                lambda: _view()["pending_decisions"],
                timeout=180,
                what="the review milestone to ask for an operator decision",
            )
            request_id = pending[0]["request_id"]
            # Through a process that has launched DBOS, the way `api.py`'s
            # lifespan does, rather than through the bare CLI.
            #
            # `notify_operator_decision` returns False when DBOS is not launched
            # in the calling process, and the milestone's approval wait is a
            # `DBOS.recv` with a 24-hour budget. So a decision recorded by a bare
            # CLI is durable and silent: correct in the ledger, and the milestone
            # waiting on it does not learn about it. See the handoff's open gaps.
            _in_process(
                "from local_first_agent_os.dbos_app import launch_dbos\n"
                "from local_first_agent_os.work_units import service\n"
                "launch_dbos()\n"
                f"service.submit_work_unit_decision({work_unit_id!r}, {request_id!r},"
                f" 'APPROVED', 'idem-{request_id}')\n"
            )

            _await(
                lambda: _view()["status"] == WorkUnitStatus.SUCCEEDED.value,
                timeout=300,
                what="the WorkUnit to reach SUCCEEDED",
            )


@given("a disposable coordination ledger and DBOS system database")
@given("the golden path design doc is compiled and started")
@when("the enqueue drainer and the resident dispatcher are running")
@then("the first milestone reaches a real dispatch intent")
@then("the local junior delegate answers it")
@then("the milestone records its artifact")
@when("the operator approves the review milestone")
@then("the WorkUnit reaches SUCCEEDED")
def _covered_by_the_integration_test() -> None:
    """The happy-path scenario is the integration test above, step for step.

    Written this way rather than re-driven here: the whole point of that test is
    that it runs in real subprocesses against disposable databases, and a second
    in-process implementation of the same scenario would pass while proving none
    of it.
    """

    if os.environ.get("LOCAL_AGENT_RUN_POSTGRES_INTEGRATION") != "1":
        pytest.skip("set LOCAL_AGENT_RUN_POSTGRES_INTEGRATION=1 to run the full drive")


# --------------------------------------------------------------------------- #
# The edge cases, in the ordinary lane
# --------------------------------------------------------------------------- #


def _running_milestone(design_doc_id: str = "golden_path_edges") -> str:
    compiled = compile_acceptance_doc(design_doc_id=design_doc_id)
    assert compiled.compiled_plan_revision_id is not None
    started = repo.start_work_unit(compiled.compiled_plan_revision_id, title="golden path")
    work_unit_id = started.work_unit.work_unit_id
    for status in (MilestoneExecutionStatus.READY, MilestoneExecutionStatus.RUNNING):
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase.PLAN,
                milestone_key=FIRST_MILESTONE,
                status=status,
                attempt=1,
            ),
        )
    return work_unit_id


def _milestone(work_unit_id: str) -> repo.MilestoneExecutionRow:
    return next(
        item
        for item in repo.list_milestone_executions(work_unit_id)
        if item.stable_key == FIRST_MILESTONE
    )


@pytest.fixture()
def world() -> dict[str, Any]:
    return {}


@given("a milestone waiting on a dispatch intent that has already settled")
def _already_settled(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    world["reads"] = 0
    world["slept"] = 0.0

    def _read(_intent_id: str) -> dict[str, Any]:
        world["reads"] += 1
        return {"intent_id": "intent-1", "status": DispatchIntentStatus.DONE.value}

    monkeypatch.setattr("local_first_agent_os.work_units.execution.dispatch_intent_row", _read)
    monkeypatch.setattr(
        "local_first_agent_os.work_units.execution.time.sleep",
        lambda seconds: world.__setitem__("slept", world["slept"] + seconds),
    )


@given("a milestone waiting on a dispatch intent")
def _waiting(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    world["row"] = {"intent_id": "intent-1", "status": DispatchIntentStatus.CLAIMED.value}
    world["slept"] = 0.0
    monkeypatch.setattr(
        "local_first_agent_os.work_units.execution.dispatch_intent_row",
        lambda _intent_id: world["row"],
    )
    monkeypatch.setattr(
        "local_first_agent_os.work_units.execution.time.sleep",
        lambda seconds: world.__setitem__("slept", world["slept"] + seconds),
    )


@when("the milestone waits")
def _waits(world: dict[str, Any]) -> None:
    world["result"] = DispatchBackedExecutorRuntime(poll_interval_seconds=0.0).poll_until_stopped(
        "intent-1", 1800.0
    )


@then("it reads the outcome without waiting out its bound")
def _no_wait(world: dict[str, Any]) -> None:
    assert isinstance(world["result"], DispatchSettled)
    assert world["reads"] == 1
    assert world["slept"] == 0.0


@when("the intent pauses at a checkpoint")
def _pauses(world: dict[str, Any]) -> None:
    world["row"] = {
        "intent_id": "intent-1",
        "status": DispatchIntentStatus.PAUSED.value,
        "checkpoint_id": "cp-1",
    }
    world["result"] = DispatchBackedExecutorRuntime(poll_interval_seconds=0.0).poll_until_stopped(
        "intent-1", 1800.0
    )


@then(parsers.parse('the milestone is blocked with failure code "{code}"'))
def _blocked_with(world: dict[str, Any], code: str) -> None:
    assert code == "dispatch_paused"
    assert isinstance(world["result"], DispatchParked)
    assert world["slept"] == 0.0


@given("a running milestone whose dispatch intent has an open execution lease")
def _milestone_with_lease(world: dict[str, Any], work_unit_ledger: Path) -> None:
    from local_first_agent_os.coordination.dispatch import submit_dispatch_intent
    from local_first_agent_os.coordination.execution import open_execution_lease

    work_unit_id = _running_milestone("golden_path_cancel")
    intent = submit_dispatch_intent(tier="junior", prompt="do a thing", kind="advisory")
    intent_id = str(intent["intent_id"])
    repo.record_fact(
        work_unit_id,
        DispatchIntentCreated(
            phase=LifecyclePhase.PLAN,
            milestone_key=FIRST_MILESTONE,
            attempt=1,
            dispatch_intent_id=intent_id,
            tier="junior",
            kind=DispatchKind.ADVISORY,
        ),
    )
    lease = open_execution_lease(
        idempotency_key=f"lease:{intent_id}",
        worker_id="golden-path",
        intent_id=intent_id,
        agent_tier="junior",
        agent_name="pi",
        timeout_seconds=60,
    )
    world["work_unit_id"] = work_unit_id
    world["intent_id"] = intent_id
    world["lease_id"] = str(lease["lease"]["lease_id"])


@when("the WorkUnit is cancelled")
def _cancelled(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from local_first_agent_os.work_units import cancellation

    stopped: list[str] = []
    monkeypatch.setattr(
        cancellation,
        "_stop_execution_lease",
        lambda lease_id, reason: stopped.append(lease_id) or _stop_attempt(lease_id),
    )
    monkeypatch.setattr(cancellation, "_stop_dbos_workflow", lambda _workflow_id: None)
    cancellation.run_cancellation_cascade(world["work_unit_id"], reason="test")
    world["stopped_leases"] = stopped


def _stop_attempt(lease_id: str) -> Any:
    from local_first_agent_os.work_units.cancellation import (
        StopAttempt,
        StopTargetKind,
        StopVerdict,
    )

    return StopAttempt(
        kind=StopTargetKind.EXECUTION_LEASE,
        identifier=lease_id,
        verdict=StopVerdict.STOPPED,
    )


@then("the lease is asked to stop")
def _lease_stopped(world: dict[str, Any]) -> None:
    assert world["stopped_leases"] == [world["lease_id"]]


@given("a WorkUnit whose execution died")
def _crashed(world: dict[str, Any], work_unit_ledger: Path) -> None:
    work_unit_id = _running_milestone("golden_path_crash")
    world["work_unit_id"] = work_unit_id
    world["workflow_id"] = execution_workflow_id(
        repo.get_work_unit(work_unit_id).root_workflow_id,
        repo.execution_epoch(work_unit_id),
    )


@when("two crash reconcilers sweep")
def _two_sweeps(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    class _Status:
        status = "ERROR"

    class _Dbos:
        def get_workflow_status(self, workflow_id: str) -> Any:
            return _Status() if workflow_id == world["workflow_id"] else None

    patcher = pytest.MonkeyPatch()
    patcher.setattr("local_first_agent_os._dbos_runtime.DBOS", _Dbos(), raising=False)
    patcher.setattr("local_first_agent_os.dbos_app.is_dbos_active", lambda: True)
    try:
        for _ in range(2):
            CrashReconciler(resume=lambda _id: {"delivered": True}).poll_once()
    finally:
        patcher.undo()


@then("exactly one automatic crash recovery is recorded")
def _one_recovery(world: dict[str, Any]) -> None:
    assert repo.automatic_crash_recovery_count(world["work_unit_id"]) == 1


@given("a milestone blocked on its last permitted attempt")
def _blocked_last_attempt(world: dict[str, Any], work_unit_ledger: Path) -> None:
    compiled = compile_acceptance_doc(design_doc_id="golden_path_budget")
    assert compiled.compiled_plan_revision_id is not None
    started = repo.start_work_unit(compiled.compiled_plan_revision_id, title="budget")
    work_unit_id = started.work_unit.work_unit_id
    # Derived from the declaration, because the scenario says "last
    # permitted" rather than a number: a literal here silently stopped
    # meaning "last permitted" the day the budget was raised.
    for attempt in range(1, _BOUNDED_RETRY.max_charged_failures + 1):
        for status in (MilestoneExecutionStatus.READY, MilestoneExecutionStatus.RUNNING):
            repo.record_fact(
                work_unit_id,
                MilestoneTransition(
                    phase=LifecyclePhase.PLAN,
                    milestone_key=FIRST_MILESTONE,
                    status=status,
                    attempt=attempt,
                ),
            )
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase.PLAN,
                milestone_key=FIRST_MILESTONE,
                status=MilestoneExecutionStatus.BLOCKED,
                attempt=attempt,
                failure_code="the agent could not finish",
                failure_class=FailureClass.CORRECTABLE,
            ),
        )
    world["work_unit_id"] = work_unit_id


@when("the WorkUnit is resumed")
def _resumed(world: dict[str, Any]) -> None:
    world["resume"] = service.resume_work_unit(
        world["work_unit_id"], delivery=EnqueueDelivery.DURABLE
    )


@then("the milestone is not made ready again")
def _not_ready(world: dict[str, Any]) -> None:
    milestone = _milestone(world["work_unit_id"])
    assert milestone.status is MilestoneExecutionStatus.BLOCKED
    assert milestone.attempt == _BOUNDED_RETRY.max_charged_failures


@then("an operator override decision is waiting")
def _override_waiting(world: dict[str, Any]) -> None:
    request_id = service.retry_override_request_id(world["work_unit_id"], FIRST_MILESTONE)
    pending = service.pending_operator_decisions(world["work_unit_id"])
    assert request_id in {item["request_id"] for item in pending}


# --- unit tests: one per decision variable on the golden path -----------------


def test_the_golden_path_document_compiles_to_a_runnable_plan(work_unit_ledger: Path) -> None:
    """The document the integration test drives, checked without needing DBOS.

    A compile failure here would make that test fail for a reason that has
    nothing to do with the resident loops, in the one lane that is expensive to
    run and rare to run.
    """

    result = service.compile_design_doc_text(
        GOLDEN_PATH_DOC.read_text(encoding="utf-8"), design_doc_id="golden_path"
    )

    assert result.runnable is True, result.diagnostics
    assert result.compiled_plan_revision_id is not None


def test_every_golden_path_milestone_asks_for_evidence_its_executor_can_produce(
    work_unit_ledger: Path,
) -> None:
    """The reason this document exists rather than the acceptance one.

    `source_patch` needs non-empty `changed_files` and `test_result` needs
    verification output; a bounded advisory turn produces neither, so a document
    asking for them can only pass by weakening the gate.
    """

    result = service.compile_design_doc_text(
        GOLDEN_PATH_DOC.read_text(encoding="utf-8"), design_doc_id="golden_path_evidence"
    )
    assert result.compiled_plan_revision_id is not None
    plan = repo.get_compiled_plan_revision(result.compiled_plan_revision_id).plan

    required = {
        item for milestone in plan.ordered_milestones() for item in milestone.required_artifacts
    }
    assert "source_patch" not in required
    assert "test_result" not in required


def test_the_golden_path_document_still_gates_on_an_operator(
    work_unit_ledger: Path,
) -> None:
    """An unattended path that never asks a person is not the path this system wants."""

    result = service.compile_design_doc_text(
        GOLDEN_PATH_DOC.read_text(encoding="utf-8"), design_doc_id="golden_path_gate"
    )
    assert result.compiled_plan_revision_id is not None
    plan = repo.get_compiled_plan_revision(result.compiled_plan_revision_id).plan

    gated = [
        milestone for milestone in plan.ordered_milestones() if milestone.approval_policy.required
    ]
    assert [milestone.stable_key for milestone in gated] == [REVIEW_MILESTONE]


def test_the_ordinary_suite_cannot_run_the_full_drive() -> None:
    """The constraint that shaped this file, asserted rather than assumed.

    `conftest` pins the DBOS flag before the package is imported, so no fixture
    can turn it on afterwards: the decorators have already bound. A test that
    believed otherwise would silently exercise the identity-decorator path and
    report a green DBOS integration that never touched DBOS.
    """

    from local_first_agent_os.dbos_app import is_dbos_active

    assert os.environ["LOCAL_AGENT_USE_DBOS"] == "false"
    assert is_dbos_active() is False


def test_the_resident_loops_this_test_starts_are_the_ones_the_runtime_starts() -> None:
    """Same subcommands as `scripts/start-agent-runtime.sh`, not a reimplementation."""

    script = (REPO_ROOT / "scripts" / "start-agent-runtime.sh").read_text(encoding="utf-8")
    assert "run_enqueue_drainer" in script
    assert "run_ledger_dispatcher" in script


def test_a_cancelled_work_unit_reaches_the_lease_its_intent_started(
    work_unit_ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole chain the NULL `dispatch_intent_id` column used to break.

    Milestone -> intent -> lease -> the process. With the column unwritten the
    cascade stopped DBOS workflows and left the agent running.
    """

    from local_first_agent_os.coordination.dispatch import submit_dispatch_intent
    from local_first_agent_os.coordination.execution import (
        live_execution_leases_for_intent,
        open_execution_lease,
    )

    work_unit_id = _running_milestone("golden_path_chain")
    intent_id = str(
        submit_dispatch_intent(tier="junior", prompt="do a thing", kind="advisory")["intent_id"]
    )
    repo.record_fact(
        work_unit_id,
        DispatchIntentCreated(
            phase=LifecyclePhase.PLAN,
            milestone_key=FIRST_MILESTONE,
            attempt=1,
            dispatch_intent_id=intent_id,
            tier="junior",
            kind=DispatchKind.ADVISORY,
        ),
    )
    open_execution_lease(
        idempotency_key=f"lease:{intent_id}",
        worker_id="golden-path",
        intent_id=intent_id,
        agent_tier="junior",
        agent_name="pi",
        timeout_seconds=60,
    )

    linked = _milestone(work_unit_id).dispatch_intent_id
    assert linked == intent_id, "the column, not just the event"
    assert [item["lease_id"] for item in live_execution_leases_for_intent(intent_id)]


def test_a_terminal_work_unit_refuses_a_resume(work_unit_ledger: Path) -> None:
    """The end of the path is an end.

    A SUCCEEDED WorkUnit that could be resumed would be a WorkUnit whose outcome
    is not final, which is the property everything downstream reads it for.
    """

    work_unit_id = _running_milestone("golden_path_terminal")
    repo.record_fact(work_unit_id, WorkUnitTransition(status=WorkUnitStatus.BLOCKED, reason="stop"))
    repo.record_fact(work_unit_id, WorkUnitTransition(status=WorkUnitStatus.FAILED))

    with pytest.raises(repo.WorkUnitError, match="cannot be resumed"):
        service.resume_work_unit(work_unit_id, delivery=EnqueueDelivery.DURABLE)
