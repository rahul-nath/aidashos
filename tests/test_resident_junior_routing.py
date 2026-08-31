# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Where a junior task actually runs.

The gherkin scenarios in ``features/resident_junior_routing.feature`` cover the
edge cases; the unit tests below cover one decision variable each along the same
path, rather than their cross product. The variables are: the bench harness, the
presence of a delegate, the presence of a judgment role, whether the tier is
staffed at all, review versus implement, model set or unset, effort set or
unset, which delegate builder is used, and which alternate the fallback picks.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from local_first_agent_os.contracts import WorkflowType
from local_first_agent_os.coordination import DispatchKind
from local_first_agent_os.local_delegate import (
    build_directive_local_delegate,
    build_resident_local_delegate,
    resident_delegate_workflow_id,
)
from local_first_agent_os.pow_wow import (
    CliPowWowExecutor,
    PowWowExecutionContext,
    PowWowTaskSpec,
)
from local_first_agent_os.project_access import AccessMode, ProjectAccessPolicy
from local_first_agent_os.project_center import LinkedProject
from local_first_agent_os.spawn_authority import (
    ReadOnlyInspection,
    SpawnPosture,
    UnattendedImplementation,
)
from local_first_agent_os.staffing import (
    BenchSlot,
    FrontierHarness,
    Harness,
    JudgmentRole,
    LocalHarness,
    classify_harness,
)
from local_first_agent_os.vocabulary import DispatchTier

scenarios("features/resident_junior_routing.feature")


# --- fixtures and builders ----------------------------------------------------


def _target(path: Path) -> LinkedProject:
    path.mkdir(parents=True, exist_ok=True)
    return LinkedProject(
        id="ai_business_portfolio",
        kind="business_factory",
        path=path,
        status="active_product_repo",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="portfolio repo",
        verification_commands=[f'{shlex.quote(sys.executable)} -c "pass"'],
    )


def _context(target: LinkedProject) -> PowWowExecutionContext:
    return PowWowExecutionContext(
        saga_id="saga-junior-routing",
        goal="Answer a bounded question",
        directive="/pow-wow answer a bounded question",
        target_project_id=target.id,
        target_project_path=str(target.expanded_path),
        target_project_kind=target.kind,
        target_project_status=target.status,
        target_project_read_only=target.read_only,
        verification_commands=tuple(target.verification_commands),
    )


def _junior_task(name: str = "junior_context") -> PowWowTaskSpec:
    return PowWowTaskSpec(
        task_name=name,
        role="analyst",
        judgment=JudgmentRole(name="analyst", tier=DispatchTier.JUNIOR),
        dispatch_kind=DispatchKind.ADVISORY,
        description="summarise the target",
    )


class _RecordingDelegate:
    """A delegate that records its calls and answers successfully."""

    def __init__(self, output: str = "the local model answered") -> None:
        self.calls: list[dict[str, Any]] = []
        self.output = output

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        return {"ok": True, "output": self.output, "error": None}


def _bench(junior: Harness = Harness.PI) -> dict[DispatchTier, BenchSlot]:
    return {
        DispatchTier.JUNIOR: BenchSlot(harness=junior, model="gemma4", capacity=4),
        DispatchTier.SENIOR: BenchSlot(harness=Harness.CLAUDE, capacity=1),
        DispatchTier.STAFF: BenchSlot(harness=Harness.CODEX, capacity=1),
    }


def _executor(
    tmp_path: Path,
    *,
    delegate: Any | None = None,
    junior: Harness = Harness.PI,
    claude_bin: str = "claude-should-not-run",
    codex_bin: str = "codex-should-not-run",
) -> CliPowWowExecutor:
    return CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        bench=_bench(junior),
        delegate_fn=delegate,
        claude_bin=claude_bin,
        codex_bin=codex_bin,
    )


def _posture_for(is_review: bool) -> SpawnPosture:
    """The posture the old `is_review` boolean stood in for.

    These tests are about harness selection, not about authority; this keeps
    each scenario asking one question.
    """

    return ReadOnlyInspection() if is_review else UnattendedImplementation()


def _route(executor: CliPowWowExecutor, target: LinkedProject, task: PowWowTaskSpec):
    return executor._run_scheduled_task(
        pow_wow_id="pow-junior-routing",
        target_project=target,
        task=task,
        context=_context(target),
        dependency_results=(),
        code_worktrees={},
        code_worktree_lock=threading.Lock(),
    )


# --- gherkin steps ------------------------------------------------------------


@pytest.fixture()
def world(tmp_path: Path) -> dict[str, Any]:
    return {"tmp_path": tmp_path, "junior_harness": Harness.PI}


@given('a bench that staffs junior with the local harness and model "gemma4"')
def _junior_is_local(world: dict[str, Any]) -> None:
    world["junior_harness"] = Harness.PI


@given("a bench that staffs senior with claude and staff with codex")
def _frontiers_are_configured(world: dict[str, Any]) -> None:
    # `_bench` binds these; the step exists so the feature file states the
    # assumption a reader needs rather than leaving it in Python.
    assert _bench()[DispatchTier.SENIOR].harness is Harness.CLAUDE
    assert _bench()[DispatchTier.STAFF].harness is Harness.CODEX


@given("a bench that staffs junior with claude instead")
def _junior_is_claude(world: dict[str, Any]) -> None:
    world["junior_harness"] = Harness.CLAUDE


@given("a dispatcher runner built the way the resident loop builds one")
def _resident_runner(world: dict[str, Any]) -> None:
    world["delegate"] = _RecordingDelegate()


@given("an executor built with no delegate callback")
def _no_delegate(world: dict[str, Any]) -> None:
    world["delegate"] = None


@when("a junior task is routed")
def _route_junior(world: dict[str, Any]) -> None:
    executor = _executor(
        world["tmp_path"],
        delegate=world.get("delegate"),
        junior=world["junior_harness"],
    )
    target = _target(world["tmp_path"] / "repo")
    task = _junior_task()
    world["executor"] = executor
    if world["junior_harness"] is Harness.CLAUDE:
        # The claude path would spawn a process; the command is the observable.
        slot = executor.bench[DispatchTier.JUNIOR]
        frontier = executor._resolve_frontier_harness(slot)
        assert isinstance(frontier, FrontierHarness)
        world["command"] = executor._build_agent_cli_command(
            frontier, slot.model, "prompt", UnattendedImplementation()
        )
        return
    world["result"] = _route(executor, target, task)


@when(parsers.parse('the frontier command builder is asked for "{harness}" with review "{review}"'))
def _build_for(world: dict[str, Any], harness: str, review: str) -> None:
    executor = _executor(world["tmp_path"], claude_bin="claude", codex_bin="codex")
    world["command"] = executor._build_agent_cli_command(
        FrontierHarness(harness), None, "do the thing", _posture_for(review == "yes")
    )


@when("the frontier command builder is asked for the local harness")
def _build_for_local(world: dict[str, Any]) -> None:
    world["classification"] = classify_harness(Harness.PI)


@when("claude fails with a usage limit")
def _claude_failed(world: dict[str, Any]) -> None:
    executor = _executor(world["tmp_path"])
    world["alternate"] = executor._select_alternate_frontier_slot(FrontierHarness.CLAUDE)


@when("codex fails with a usage limit")
def _codex_failed(world: dict[str, Any]) -> None:
    executor = _executor(world["tmp_path"])
    world["alternate"] = executor._select_alternate_frontier_slot(FrontierHarness.CODEX)


@then("the task runs on the local delegate")
def _ran_on_delegate(world: dict[str, Any]) -> None:
    delegate = world["delegate"]
    assert isinstance(delegate, _RecordingDelegate)
    assert len(delegate.calls) == 1
    assert world["result"].status == "completed"


@then("no frontier CLI is spawned")
def _no_cli(world: dict[str, Any]) -> None:
    # The binaries are named so they cannot exist; a spawn would raise or fail
    # with a shell error rather than silently succeed.
    assert world["executor"].claude_bin == "claude-should-not-run"
    assert world["executor"].codex_bin == "codex-should-not-run"
    for artifact in world["result"].artifacts:
        assert artifact.artifact_type != "cli_agent_run"


@then("the task fails")
def _task_failed(world: dict[str, Any]) -> None:
    assert world["result"].status == "failed"


@then("the failure names the local harness it could not call")
def _failure_names_harness(world: dict[str, Any]) -> None:
    assert any("'pi'" in risk for risk in world["result"].risks)
    assert any("delegate" in risk for risk in world["result"].risks)


@then("it is a type error, and at runtime the classification refuses it")
def _local_is_refused(world: dict[str, Any]) -> None:
    assert isinstance(world["classification"], LocalHarness)
    assert world["classification"].harness is Harness.PI


@then("a claude command is built")
def _claude_built(world: dict[str, Any]) -> None:
    assert world["command"][0] == "claude-should-not-run"
    assert "--model" in world["command"]


@then(parsers.parse('the command starts with the "{harness}" binary'))
def _starts_with(world: dict[str, Any], harness: str) -> None:
    assert world["command"][0] == harness


@then(parsers.parse('the command contains "{flag}"'))
def _contains(world: dict[str, Any], flag: str) -> None:
    assert flag in world["command"]


@then("the alternate frontier selected is codex")
def _alternate_is_codex(world: dict[str, Any]) -> None:
    assert world["alternate"] is not None
    assert world["alternate"][0] is FrontierHarness.CODEX


@then("the alternate frontier selected is claude")
def _alternate_is_claude(world: dict[str, Any]) -> None:
    assert world["alternate"] is not None
    assert world["alternate"][0] is FrontierHarness.CLAUDE


@then("a workflow run exists for the pow-wow the task belonged to")
def _workflow_registered(world: dict[str, Any]) -> None:
    delegate = world["delegate"]
    assert isinstance(delegate, _RecordingDelegate)
    assert delegate.calls[0]["pow_wow_id"] == "pow-junior-routing"


@then("the model call is recorded against that workflow")
def _model_call_scoped(world: dict[str, Any]) -> None:
    # The recording delegate stands in for the model call; the real scoping is
    # asserted against a live repository in
    # `test_a_resident_delegate_registers_the_workflow_its_model_call_needs`.
    assert world["result"].status == "completed"


# --- unit tests: one per decision variable on the routing path ----------------


# Variable 1: the bench harness (three values).
def test_the_local_harness_is_classified_as_local() -> None:
    classified = classify_harness(Harness.PI)
    assert isinstance(classified, LocalHarness)
    assert classified.harness is Harness.PI


def test_claude_is_classified_as_a_frontier_harness() -> None:
    assert classify_harness(Harness.CLAUDE) is FrontierHarness.CLAUDE


def test_codex_is_classified_as_a_frontier_harness() -> None:
    assert classify_harness(Harness.CODEX) is FrontierHarness.CODEX


# Variable 2: whether a delegate exists (two values).
def test_a_junior_task_with_a_delegate_runs_on_the_local_model(tmp_path: Path) -> None:
    delegate = _RecordingDelegate()
    executor = _executor(tmp_path, delegate=delegate)
    result = _route(executor, _target(tmp_path / "repo"), _junior_task())
    assert result.status == "completed"
    assert delegate.calls[0]["model"] == "gemma4"
    assert delegate.calls[0]["tier"] == "junior"


def test_a_junior_task_without_a_delegate_fails_instead_of_becoming_a_claude_command(
    tmp_path: Path,
) -> None:
    """The live defect, as an assertion.

    Before this, a `pi`/`gemma4` slot with no delegate produced
    `claude --model gemma4` and a 401. The requirement is not that it succeed -
    there is nothing to run it on - but that it say so.
    """

    executor = _executor(tmp_path, delegate=None)
    result = _route(executor, _target(tmp_path / "repo"), _junior_task())
    assert result.status == "failed"
    assert any("'pi'" in risk for risk in result.risks)
    assert all("claude" not in risk for risk in result.risks)


# Variable 3: whether the task carries a judgment role (two values).
def test_a_task_with_no_judgment_role_is_not_a_local_task(tmp_path: Path) -> None:
    executor = _executor(tmp_path, delegate=_RecordingDelegate())
    task = PowWowTaskSpec(task_name="plan", role="planner", description="plan it")
    assert executor._local_harness_for(task) is None


def test_a_junior_judgment_role_is_a_local_task(tmp_path: Path) -> None:
    executor = _executor(tmp_path, delegate=_RecordingDelegate())
    assert executor._local_harness_for(_junior_task()) is not None


# Variable 4: whether the tier is staffed at all (two values).
def test_an_unstaffed_tier_is_not_reported_as_local(tmp_path: Path) -> None:
    """An unstaffed tier is a question for the path that needs the slot.

    The predicate is asked of every task by the scheduler, so answering it with
    a KeyError would turn an unstaffed tier into a crash in the scheduling loop
    rather than a message from `resolve_bench` where the slot is used.
    """

    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        bench={DispatchTier.SENIOR: BenchSlot(harness=Harness.CLAUDE, capacity=1)},
        delegate_fn=_RecordingDelegate(),
    )
    assert executor._local_harness_for(_junior_task()) is None


def test_a_staffed_local_tier_is_reported_as_local(tmp_path: Path) -> None:
    executor = _executor(tmp_path, delegate=_RecordingDelegate())
    local = executor._local_harness_for(_junior_task())
    assert local is not None
    assert "no CLI to spawn" in local.describe()


# Variable 5: review versus implement (two values, per harness).
def test_a_codex_review_gets_the_read_only_sandbox(tmp_path: Path) -> None:
    command = _executor(tmp_path, codex_bin="codex")._build_agent_cli_command(
        FrontierHarness.CODEX, None, "review", ReadOnlyInspection()
    )
    assert "-s" in command and "read-only" in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command


def test_a_claude_implementation_gets_the_permission_bypass(tmp_path: Path) -> None:
    command = _executor(tmp_path, claude_bin="claude")._build_agent_cli_command(
        FrontierHarness.CLAUDE, None, "implement", UnattendedImplementation()
    )
    assert "--dangerously-skip-permissions" in command


# Variable 6: whether a model is set (two values).
def test_a_command_omits_the_model_flag_when_the_slot_names_no_model(tmp_path: Path) -> None:
    command = _executor(tmp_path, claude_bin="claude")._build_agent_cli_command(
        FrontierHarness.CLAUDE, None, "go", UnattendedImplementation()
    )
    assert "--model" not in command


def test_a_command_carries_the_model_flag_when_the_slot_names_one(tmp_path: Path) -> None:
    command = _executor(tmp_path, claude_bin="claude")._build_agent_cli_command(
        FrontierHarness.CLAUDE, "opus-5", "go", UnattendedImplementation()
    )
    assert command[command.index("--model") + 1] == "opus-5"


# Variable 7: whether a reasoning effort is set (two values).
def test_a_command_omits_the_effort_flag_when_the_slot_names_no_effort(tmp_path: Path) -> None:
    command = _executor(tmp_path, claude_bin="claude")._build_agent_cli_command(
        FrontierHarness.CLAUDE, None, "go", UnattendedImplementation()
    )
    assert "--effort" not in command


def test_a_command_carries_the_effort_flag_when_the_slot_names_one(tmp_path: Path) -> None:
    command = _executor(tmp_path, claude_bin="claude")._build_agent_cli_command(
        FrontierHarness.CLAUDE, None, "go", UnattendedImplementation(), reasoning_effort="max"
    )
    assert command[command.index("--effort") + 1] == "max"


# Variable 8: which delegate builder (two values).
def test_a_directive_delegate_scopes_model_calls_to_the_caller_s_workflow(runtime) -> None:
    from local_first_agent_os.contracts import IngressEvent, SourceType, WorkspaceId

    event = IngressEvent(
        event_id="evt-directive-delegate",
        source_type=SourceType.FILE,
        source_uri="test://directive",
        event_type="test.directive",
        workspace_id=WorkspaceId.GENERAL.value,
        content_sha256="0" * 64,
        payload={},
    )
    runtime.repository.register_ingress_event(event)
    runtime.repository.start_workflow_run(
        workflow_id="directive-1",
        workflow_type=WorkflowType.MODEL_DIRECTIVE.value,
        workspace_id=WorkspaceId.GENERAL.value,
        input_event_id=event.event_id,
    )
    delegate = build_directive_local_delegate(runtime, workflow_id="directive-1")
    payload = delegate(prompt="say something", task_name="t", pow_wow_id="ignored")
    assert payload["ok"] is True
    # The caller's workflow is used verbatim; the pow-wow is not consulted.
    assert not runtime.repository.workflow_run_exists(resident_delegate_workflow_id("ignored"))


def test_a_resident_delegate_registers_the_workflow_its_model_call_needs(runtime) -> None:
    """`model_invocations.workflow_id` is NOT NULL REFERENCES workflow_runs.

    A resident dispatcher has no workflow to borrow, so a delegate that does not
    open one cannot record that it called a model at all.
    """

    delegate = build_resident_local_delegate(runtime)
    workflow_id = resident_delegate_workflow_id("pow-42")
    assert not runtime.repository.workflow_run_exists(workflow_id)
    payload = delegate(prompt="say something", task_name="t", pow_wow_id="pow-42")
    assert payload["ok"] is True
    assert runtime.repository.workflow_run_exists(workflow_id)
    state = runtime.repository.get_workflow_run_state(workflow_id)
    assert state is not None
    assert state.workflow_type is WorkflowType.RESIDENT_LOCAL_DELEGATE


def test_a_resident_delegate_registers_one_workflow_per_pow_wow(runtime) -> None:
    delegate = build_resident_local_delegate(runtime)
    for index in range(3):
        delegate(prompt="q", task_name=f"t{index}", pow_wow_id="pow-shared")
    delegate(prompt="q", task_name="other", pow_wow_id="pow-other")
    assert runtime.repository.workflow_run_exists(resident_delegate_workflow_id("pow-shared"))
    assert runtime.repository.workflow_run_exists(resident_delegate_workflow_id("pow-other"))


def test_a_resident_delegate_registers_its_workflow_once_under_concurrency(runtime) -> None:
    """Junior tasks run on a pool sized by the bench slot's capacity.

    `start_workflow_run` is read-then-insert rather than an upsert, so several
    threads reaching the same unregistered pow-wow at once is a duplicate-key
    race unless the first registration is serialised.
    """

    delegate = build_resident_local_delegate(runtime)
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def call() -> None:
        try:
            barrier.wait(timeout=10)
            delegate(prompt="q", task_name="t", pow_wow_id="pow-race")
        except BaseException as exc:  # noqa: BLE001 - the assertion is "none of these"
            errors.append(exc)

    threads = [threading.Thread(target=call) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    assert runtime.repository.workflow_run_exists(resident_delegate_workflow_id("pow-race"))


# Variable 9: which alternate the cross-provider fallback picks (three values).
def test_the_fallback_for_claude_is_codex(tmp_path: Path) -> None:
    alternate = _executor(tmp_path)._select_alternate_frontier_slot(FrontierHarness.CLAUDE)
    assert alternate is not None and alternate[0] is FrontierHarness.CODEX


def test_the_fallback_for_codex_is_claude(tmp_path: Path) -> None:
    alternate = _executor(tmp_path)._select_alternate_frontier_slot(FrontierHarness.CODEX)
    assert alternate is not None and alternate[0] is FrontierHarness.CLAUDE


def test_there_is_no_fallback_when_the_bench_staffs_only_one_frontier(tmp_path: Path) -> None:
    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        bench={DispatchTier.SENIOR: BenchSlot(harness=Harness.CLAUDE, capacity=1)},
    )
    assert executor._select_alternate_frontier_slot(FrontierHarness.CLAUDE) is None


# The wiring the live failure came through, asserted at its own boundary.
def test_the_dispatcher_runner_default_is_a_working_delegate_not_absence(runtime) -> None:
    """`build_dispatcher_runner(runtime)` used to produce a runner with none.

    That call is what `run_ledger_dispatcher` makes, and "no delegate" does not
    mean "runs no junior work" - it means junior work is launched on a frontier
    CLI. So the default has to be a delegate.
    """

    from local_first_agent_os.dispatcher_runner import build_dispatcher_runner

    runner = build_dispatcher_runner(runtime)
    assert runner.delegate_fn is not None


def test_the_resident_dispatcher_loop_builds_a_runner_that_has_a_delegate(
    monkeypatch: pytest.MonkeyPatch, runtime
) -> None:
    """The resident loop's own construction path, not a reimplementation of it."""

    from local_first_agent_os.coordination import dispatcher_loop

    built: dict[str, Any] = {}

    class _Dispatcher:
        def __init__(self, runner: Any, **_: Any) -> None:
            built["runner"] = runner
            self.last_outcomes: list[Any] = []

        def dispatch_pending_intents(self, **_: Any) -> int:
            return 0

    monkeypatch.setattr("local_first_agent_os.runtime.build_runtime", lambda: runtime)
    monkeypatch.setattr("local_first_agent_os.dispatcher.LedgerDispatcher", _Dispatcher)
    result = dispatcher_loop.run_ledger_dispatcher(max_polls=0, name="test-dispatcher")
    assert result["ok"] is True
    assert built["runner"].delegate_fn is not None


def test_a_dead_process_is_never_the_answer_for_a_local_harness(tmp_path: Path) -> None:
    """No configuration of the bench can make a local slot spawn a CLI.

    The command builder is typed on `FrontierHarness`, so the only way to reach
    it is through `_resolve_frontier_harness`, and for a local slot that answers
    with the refusal instead.
    """

    executor = _executor(tmp_path)
    resolved = executor._resolve_frontier_harness(executor.bench[DispatchTier.JUNIOR])
    assert isinstance(resolved, LocalHarness)


def test_a_slotless_task_still_defaults_to_claude(tmp_path: Path) -> None:
    """The historical default for a task with no judgment role is unchanged."""

    assert _executor(tmp_path)._resolve_frontier_harness(None) is FrontierHarness.CLAUDE


def test_the_binaries_named_by_these_tests_do_not_exist() -> None:
    """Guards the `no frontier CLI is spawned` assertions above.

    They are only evidence if running the named binary would fail loudly.
    """

    for name in ("claude-should-not-run", "codex-should-not-run"):
        with pytest.raises(FileNotFoundError):
            subprocess.run([name, "--version"], capture_output=True, check=False)
