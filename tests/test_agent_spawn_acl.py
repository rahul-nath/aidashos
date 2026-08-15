# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What a spawned agent process is allowed to do.

The scenarios in ``features/agent_spawn_acl.feature`` cover the edge cases. The
unit tests below take one decision variable each along the same path rather than
their cross product: the capability set, the task's purpose, the intent's
ceiling and the three ways it can be missing, the harness, and which of the ten
executor kinds asked for the work.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from local_first_agent_os.capabilities import Capability, UnknownCapability
from local_first_agent_os.dispatcher_runner import intent_spawn_ceiling
from local_first_agent_os.pow_wow import CliPowWowExecutor, PowWowTaskSpec
from local_first_agent_os.pow_wow.protocol import TaskPurpose
from local_first_agent_os.spawn_authority import (
    ReadOnlyInspection,
    SpawnAuthority,
    SupervisedCommands,
    UnattendedImplementation,
    authority_for_purpose,
    describe_posture,
)
from local_first_agent_os.staffing import FrontierHarness
from local_first_agent_os.work_units.executors import EXECUTOR_REGISTRY, ExecutorKind

scenarios("features/agent_spawn_acl.feature")


_BYPASS_FLAGS = frozenset(
    {"--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox"}
)


def _executor(tmp_path: Path, *, ceiling: SpawnAuthority | None = None) -> CliPowWowExecutor:
    return CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        claude_bin="claude",
        codex_bin="codex",
        spawn_ceiling=ceiling,
    )


def _task(purpose: TaskPurpose, *, name: str = "do_the_thing") -> PowWowTaskSpec:
    return PowWowTaskSpec(
        task_name=name,
        role="engineer",
        description="do the thing",
        purpose=purpose,
    )


# --- gherkin steps ------------------------------------------------------------


@pytest.fixture()
def world(tmp_path: Path) -> dict[str, Any]:
    return {"tmp_path": tmp_path}


@when(parsers.parse('the capability set is "{capabilities}"'))
def _capability_set(world: dict[str, Any], capabilities: str) -> None:
    world["posture"] = SpawnAuthority.from_names(capabilities.split(",")).posture()


@then(parsers.parse('the spawn posture is "{posture}"'))
def _posture_is(world: dict[str, Any], posture: str) -> None:
    assert describe_posture(world["posture"]) == posture


@given(parsers.parse('a "{harness}" agent'))
def _agent_harness(world: dict[str, Any], harness: str) -> None:
    world["harness"] = FrontierHarness(harness)


@when(parsers.parse('the spawn posture is "{posture}"'))
def _build_with_posture(world: dict[str, Any], posture: str) -> None:
    world["command"] = _executor(world["tmp_path"])._build_agent_cli_command(
        world["harness"],
        None,
        "do the thing",
        {
            "read_only_inspection": ReadOnlyInspection(),
            "supervised_commands": SupervisedCommands(),
            "unattended_implementation": UnattendedImplementation(),
        }[posture],
    )


@then(parsers.parse("the command {bypass} a permission bypass"))
def _command_bypass(world: dict[str, Any], bypass: str) -> None:
    carries = bool(_BYPASS_FLAGS & set(world["command"]))
    assert carries is (bypass == "carries")


@given("a dispatch intent whose ceiling permits writing and running")
def _wide_ceiling(world: dict[str, Any]) -> None:
    world["intent"] = {
        "intent_id": "intent-1",
        "permitted_capabilities": json.dumps(
            [
                Capability.READ_REPOSITORY.value,
                Capability.WRITE_REPOSITORY.value,
                Capability.RUN_COMMAND.value,
            ]
        ),
    }


@given("a dispatch intent whose ceiling permits only reading")
def _narrow_ceiling(world: dict[str, Any]) -> None:
    world["intent"] = {
        "intent_id": "intent-1",
        "permitted_capabilities": json.dumps([Capability.READ_REPOSITORY.value]),
    }


@given("a dispatch intent with no capability set at all")
def _absent_ceiling(world: dict[str, Any]) -> None:
    world["intent"] = {"intent_id": "intent-1"}


@given("a dispatch intent whose capability set is malformed")
def _malformed_ceiling(world: dict[str, Any]) -> None:
    world["intent"] = {"intent_id": "intent-1", "permitted_capabilities": "{not json"}


@when(parsers.re(r"an? (?P<kind>\w+) task is spawned under it"))
def _spawn_task(world: dict[str, Any], kind: str) -> None:
    purpose = {
        "review": TaskPurpose.REVIEW,
        "implementation": TaskPurpose.IMPLEMENTATION,
    }[kind]
    executor = _executor(world["tmp_path"], ceiling=intent_spawn_ceiling(world["intent"]))
    authority = executor._task_spawn_authority(_task(purpose))
    world["authority"] = authority
    world["posture"] = authority.posture()
    world["command"] = executor._build_agent_cli_command(
        FrontierHarness.CLAUDE, None, "do the thing", world["posture"]
    )


@then("the recorded posture matches the command that was built")
def _recorded_matches(world: dict[str, Any]) -> None:
    recorded = describe_posture(world["posture"])
    carries = bool(_BYPASS_FLAGS & set(world["command"]))
    assert carries is (recorded == "unattended_implementation")


# --- unit tests: one per decision variable on the spawn path ------------------


# Variable 1: the capability set (the rule the ACL document states).
def test_a_bypass_is_earned_only_by_declaring_write_and_run() -> None:
    """The acceptance criterion, spelled once and asserted once."""

    both = SpawnAuthority.of((Capability.WRITE_REPOSITORY, Capability.RUN_COMMAND))
    assert isinstance(both.posture(), UnattendedImplementation)


def test_running_without_writing_is_its_own_posture() -> None:
    """The posture that did not exist.

    A test runner and a repository validator both need a shell and neither
    should be able to edit what they are checking. With two postures available
    they were given an implementer's.
    """

    runner = SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.RUN_COMMAND))
    assert isinstance(runner.posture(), SupervisedCommands)


def test_writing_without_running_does_not_earn_a_bypass() -> None:
    writer = SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.WRITE_REPOSITORY))
    assert isinstance(writer.posture(), ReadOnlyInspection)


def test_an_empty_authority_is_the_narrowest_posture() -> None:
    assert isinstance(SpawnAuthority.nothing().posture(), ReadOnlyInspection)


# Variable 2: the task's purpose (six values, three role bounds).
@pytest.mark.parametrize(
    ("purpose", "expected"),
    [
        (TaskPurpose.IMPLEMENTATION, UnattendedImplementation),
        (TaskPurpose.RECOVERY_REVISION, UnattendedImplementation),
        (TaskPurpose.DETERMINISTIC_CHECK, SupervisedCommands),
        (TaskPurpose.BROWSER_ACCEPTANCE, SupervisedCommands),
        (TaskPurpose.REVIEW, ReadOnlyInspection),
        (TaskPurpose.ADVISORY, ReadOnlyInspection),
    ],
)
def test_each_task_purpose_has_a_role_bound(purpose: TaskPurpose, expected: type) -> None:
    assert isinstance(authority_for_purpose(purpose).posture(), expected)


def test_every_task_purpose_is_accounted_for() -> None:
    """`authority_for_purpose` is exhaustive, so this is a check on the enum.

    It fails if a seventh purpose is added and the bound is not extended, rather
    than letting the new purpose silently receive whatever the ceiling holds.
    """

    for purpose in TaskPurpose:
        assert authority_for_purpose(purpose).capabilities is not None


# Variable 3: the ceiling, and the three ways it can be missing.
def test_a_declared_ceiling_is_parsed(work_unit_ledger: Path) -> None:
    ceiling = intent_spawn_ceiling(
        {"permitted_capabilities": json.dumps(["read_repository", "run_command"])}
    )
    assert ceiling.capabilities == {Capability.READ_REPOSITORY, Capability.RUN_COMMAND}


def test_an_absent_ceiling_is_nothing_not_everything() -> None:
    """The inversion that matters.

    Absence used to mean "not a review", which meant the sandbox came off. It now
    means the narrowest authority, so an intent from before the column - or from
    a producer that declares nothing - gets a read-only spawn.
    """

    assert intent_spawn_ceiling({}).capabilities == frozenset()


def test_an_empty_ceiling_is_nothing() -> None:
    assert intent_spawn_ceiling({"permitted_capabilities": "[]"}).capabilities == frozenset()


def test_a_malformed_ceiling_is_nothing() -> None:
    """Widening on a value nobody can parse is how a permission model decays."""

    assert intent_spawn_ceiling({"permitted_capabilities": "{not json"}).capabilities == frozenset()


def test_a_json_object_is_not_reinterpreted_as_a_capability_array() -> None:
    ceiling = intent_spawn_ceiling(
        {"permitted_capabilities": json.dumps({"write_repository": True})}
    )
    assert ceiling.capabilities == frozenset()


def test_a_ceiling_naming_an_unknown_capability_is_refused() -> None:
    """A grant for a capability that does not exist can never be satisfied."""

    with pytest.raises(UnknownCapability, match="become_root"):
        intent_spawn_ceiling(
            {"permitted_capabilities": json.dumps(["read_repository", "become_root"])}
        )


# Variable 4: the intersection of ceiling and role.
def test_the_role_cannot_exceed_the_ceiling(tmp_path: Path) -> None:
    executor = _executor(tmp_path, ceiling=SpawnAuthority.of((Capability.READ_REPOSITORY,)))
    authority = executor._task_spawn_authority(_task(TaskPurpose.IMPLEMENTATION))
    assert authority.capabilities == {Capability.READ_REPOSITORY}


def test_the_ceiling_cannot_widen_the_role(tmp_path: Path) -> None:
    """A reviewer under an implementer's ceiling is still a reviewer.

    One milestone's intent fans out into an implementer, a reviewer, and a
    junior, and union would hand the reviewer write access.
    """

    executor = _executor(
        tmp_path,
        ceiling=SpawnAuthority.of(
            (
                Capability.READ_REPOSITORY,
                Capability.WRITE_REPOSITORY,
                Capability.RUN_COMMAND,
            )
        ),
    )
    authority = executor._task_spawn_authority(_task(TaskPurpose.REVIEW))
    assert Capability.WRITE_REPOSITORY not in authority.capabilities
    assert isinstance(authority.posture(), ReadOnlyInspection)


def test_a_task_declaring_its_own_capabilities_is_still_bounded(tmp_path: Path) -> None:
    executor = _executor(tmp_path, ceiling=SpawnAuthority.of((Capability.READ_REPOSITORY,)))
    task = PowWowTaskSpec(
        task_name="t",
        role="engineer",
        description="d",
        purpose=TaskPurpose.ADVISORY,
        capabilities=("read_repository", "write_repository", "run_command"),
    )
    assert executor._task_spawn_authority(task).capabilities == {Capability.READ_REPOSITORY}


# Variable 5: the harness (two values, three postures each).
@pytest.mark.parametrize(
    ("posture", "expected"),
    [
        (ReadOnlyInspection(), "read-only"),
        (SupervisedCommands(), "workspace-write"),
    ],
)
def test_codex_sandbox_modes_are_real_values(tmp_path: Path, posture: Any, expected: str) -> None:
    """`codex exec -s` accepts read-only|workspace-write|danger-full-access.

    Checked against the installed CLI rather than assumed; a wrong value is an
    immediate non-zero exit inside a leased worktree.
    """

    command = _executor(tmp_path)._build_agent_cli_command(
        FrontierHarness.CODEX, None, "prompt", posture
    )
    assert command[command.index("-s") + 1] == expected


def test_claude_names_the_tools_a_reader_may_not_use(tmp_path: Path) -> None:
    command = _executor(tmp_path)._build_agent_cli_command(
        FrontierHarness.CLAUDE, None, "prompt", ReadOnlyInspection()
    )
    disallowed = command[command.index("--disallowedTools") + 1]
    assert set(disallowed.split(",")) == {"Edit", "Write", "NotebookEdit", "Bash"}


def test_claude_lets_a_supervised_task_run_but_not_write(tmp_path: Path) -> None:
    command = _executor(tmp_path)._build_agent_cli_command(
        FrontierHarness.CLAUDE, None, "prompt", SupervisedCommands()
    )
    disallowed = command[command.index("--disallowedTools") + 1].split(",")
    assert "Bash" not in disallowed
    assert "Write" in disallowed


def test_the_prompt_stays_the_last_argument_under_every_posture(tmp_path: Path) -> None:
    """A variadic option before the prompt would swallow it.

    `--disallowedTools` is declared variadic on the CLI, so the value is passed
    as one comma-joined argument rather than as several.
    """

    executor = _executor(tmp_path)
    for harness in FrontierHarness:
        for posture in (ReadOnlyInspection(), SupervisedCommands(), UnattendedImplementation()):
            command = executor._build_agent_cli_command(harness, None, "THE PROMPT", posture)
            assert command[-1] == "THE PROMPT"


# Variable 6: which of the ten executor kinds asked for the work.
def test_every_executor_kind_maps_to_a_posture() -> None:
    """The ACL document's verification clause, over all ten kinds.

    "the command built for a milestone of that kind contains a bypass flag if
    and only if the kind declares both write_repository and run_command."
    """

    for kind in ExecutorKind:
        declared = frozenset(EXECUTOR_REGISTRY[kind].permitted_tools)
        authority = SpawnAuthority.for_executor(kind)
        assert authority.capabilities == declared
        earns_bypass = {Capability.WRITE_REPOSITORY, Capability.RUN_COMMAND} <= declared
        assert isinstance(authority.posture(), UnattendedImplementation) is earns_bypass


def test_a_planning_milestone_no_longer_gets_an_unsandboxed_shell() -> None:
    """The concrete over-grant, named.

    `plan.implementation` declares read_repository and invoke_model. It was
    launched with `--dangerously-skip-permissions` because its task was not a
    review.
    """

    authority = SpawnAuthority.for_executor(ExecutorKind.PLAN_IMPLEMENTATION)
    assert isinstance(authority.posture(), ReadOnlyInspection)


def test_a_test_runner_gets_a_shell_without_write_access() -> None:
    authority = SpawnAuthority.for_executor(ExecutorKind.VERIFY_TESTS)
    assert isinstance(authority.posture(), SupervisedCommands)


def test_an_implementer_still_gets_what_it_declares() -> None:
    authority = SpawnAuthority.for_executor(ExecutorKind.IMPLEMENT_CODE_CHANGE)
    assert isinstance(authority.posture(), UnattendedImplementation)


def test_the_milestone_executor_submits_the_ceiling_it_compiled(
    work_unit_ledger: Path,
) -> None:
    """The compiled ceiling survives a real persisted and claimed intent row."""

    from work_unit_support import compile_acceptance_doc

    from local_first_agent_os.coordination.dispatch import claim_next_dispatch_intent
    from local_first_agent_os.work_units import repository as repo
    from local_first_agent_os.work_units.execution import (
        DispatchBackedExecutorRuntime,
        MilestoneContext,
    )

    compiled = compile_acceptance_doc(design_doc_id="spawn_acl")
    assert compiled.compiled_plan_revision_id is not None
    plan = repo.get_compiled_plan_revision(compiled.compiled_plan_revision_id).plan
    milestone = plan.ordered_milestones()[0]
    intent_id = DispatchBackedExecutorRuntime(
        target_project_id="local_first_agent_os",
        fact_recorder=lambda *_: None,
    ).submit(
        MilestoneContext(
            work_unit_id="wu-1",
            root_workflow_id="work-unit:wu-1",
            child_workflow_id="work-unit:wu-1:milestone:a:1",
            milestone=milestone,
            attempt=1,
            design_doc_revision_id="ddr-1",
            compiled_plan_hash=plan.plan_hash(),
        )
    )

    claimed = claim_next_dispatch_intent("test-dispatcher", tier="senior")
    assert claimed["ok"] is True
    assert claimed["intent"]["intent_id"] == intent_id
    claimed_intent = claimed["intent"]
    expected = tuple(sorted(milestone.tool_policy.permitted_tools))
    assert tuple(json.loads(claimed_intent["permitted_capabilities"])) == expected
    assert intent_spawn_ceiling(claimed_intent).to_names() == expected


def test_a_spawned_agent_may_fan_out_into_its_own_subagents(tmp_path: Path) -> None:
    """Harness-local fan-out is permitted, and the permission is a decision.

    `Task` is absent from every disallow row on purpose. A spawned agent may run
    its own in-process subagents: they inherit this process's tool permissions,
    and the whole fan-out still reaches the world through one leased worktree
    whose diff faces the same verification and review gates. Withholding it here
    would also be harness-specific, since `codex exec` has no equivalent, and
    this repository keeps its boundaries where every harness meets them.

    Pinned because permission by silence is how a decision nobody made becomes
    the behaviour. Reversing it should be an edit to this test first.
    """

    executor = _executor(tmp_path)
    for posture in (ReadOnlyInspection(), SupervisedCommands(), UnattendedImplementation()):
        command = executor._build_agent_cli_command(FrontierHarness.CLAUDE, None, "prompt", posture)
        if "--disallowedTools" in command:
            assert "Task" not in command[command.index("--disallowedTools") + 1].split(",")


def test_the_mutating_editor_tools_are_named_once(tmp_path: Path) -> None:
    """Both restricted postures withhold the same editors, from one list.

    "Which tools mutate a file" is one fact. Spelled per row, a Claude release
    adding a fourth editor tool needs both rows edited and nothing fails if only
    one is, so the read-only posture would keep its guarantee while the
    supervised one quietly lost it.
    """

    executor = _executor(tmp_path)

    def disallowed(posture: Any) -> set[str]:
        command = executor._build_agent_cli_command(FrontierHarness.CLAUDE, None, "prompt", posture)
        return set(command[command.index("--disallowedTools") + 1].split(","))

    read_only = disallowed(ReadOnlyInspection())
    supervised = disallowed(SupervisedCommands())

    assert supervised < read_only
    assert read_only - supervised == {"Bash"}


def test_the_doctrine_tells_the_agent_the_same_boundary(tmp_path: Path) -> None:
    """The flags permit fan-out; the doctrine says what it is answerable for.

    A permission the agent has to infer from the absence of a flag is one it can
    misread in either direction, so the contract states the trade in prose: your
    subagents are yours, and the diff you leave is what the system judges.
    """

    from local_first_agent_os.engineering_doctrine import CURRENT_ENGINEERING_DOCTRINE

    text = CURRENT_ENGINEERING_DOCTRINE.text
    assert "Execution boundary:" in text
    assert "subagents" in text
    assert "leased worktree" in text
    assert "only the host may create" in text


def test_spawn_authority_imports_first_in_a_fresh_process() -> None:
    """The import cycle that only fired depending on who imported first.

    `spawn_authority` needed `TaskPurpose` from `pow_wow.protocol`, and importing
    anything under `pow_wow` executes the package `__init__`, which imports the
    whole executor, which imports `spawn_authority` back. So the module worked in
    any process that had touched `pow_wow` first - the full suite is one - and
    crashed on a half-initialized module in any process that had not, which is
    how `pytest tests/test_staffing.py` alone could not collect while 1846 tests
    passed. The fix is a lazy import at `TaskPurpose`'s one runtime use; this
    subprocess is the reproduction, pinned.
    """

    import subprocess
    import sys

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "from local_first_agent_os.spawn_authority import SpawnAuthority, "
            "authority_for_purpose;"
            "from local_first_agent_os.pow_wow.protocol import TaskPurpose;"
            "assert authority_for_purpose(TaskPurpose.REVIEW) is not None;"
            "print('ok')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "ok"
