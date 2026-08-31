# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Whether a spawned agent may use what its plan gave it.

The scenarios in ``features/agent_caller_gate.feature`` cover the edge cases. The
unit tests below take one decision variable each along the same path rather than
their cross product: whether the capability is gated, whether it has been
revoked, whether the revocation is scoped to this pow-wow, which spawn path is
running, and what a denial does to the task.

The first test in this file is the one that matters most: it asserts the gate can
say no. Before this change `check_capability` returned granted for every
capability regardless of the ledger, because it asked the policy engine whether
`write_repository` was a forbidden *tool name* and no `Capability` value appears
in any of the policy's tool-name sets.
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when
from staffing_support import repo_bench, seat_agent_name

from local_first_agent_os.capabilities import (
    Capability,
    gated_capabilities,
    violation_cleared_by,
)
from local_first_agent_os.capability_gate import (
    AgentCaller,
    CapabilityDenied,
    CapabilityGranted,
    check_capability,
    granted_violations_for,
    revoked_capabilities_for,
)
from local_first_agent_os.coordination import DispatchKind
from local_first_agent_os.coordination.pow_wows import (
    grant_tool_permission,
    restore_tool_permission,
    revoke_tool_permission,
)
from local_first_agent_os.coordination.store import tx
from local_first_agent_os.pow_wow import (
    CliPowWowExecutor,
    PowWowExecutionContext,
    PowWowTaskSpec,
)
from local_first_agent_os.pow_wow.protocol import TaskPurpose
from local_first_agent_os.project_access import AccessMode, ProjectAccessPolicy
from local_first_agent_os.project_center import LinkedProject
from local_first_agent_os.settings import get_settings
from local_first_agent_os.spawn_authority import SpawnAuthority
from local_first_agent_os.staffing import (
    JudgmentRole,
)
from local_first_agent_os.vocabulary import DispatchTier

scenarios("features/agent_caller_gate.feature")


AGENT = seat_agent_name(DispatchTier.SENIOR)
"""Whichever harness the repo's staffing config seats as the implementer.

These scenarios are about grants and revocations for a principal that may write,
so they need the principal `POLICIES.md` permits to write. Naming the vendor made
every one of them fail the day the bench was reseated, with a denial that was
correct and had nothing to do with what they check.

Reading `DEFAULT_BENCH` instead was the same mistake one level down: the gate
resolves this name against `configs/staffing.toml`, and the fallback bench is
free to disagree with it, so these scenarios failed again the next reseating
with the same irrelevant denial.
"""

# An agent that is genuinely not AGENT. The staff seat's vendor is the natural
# choice, but an outage staffing can put one vendor in both frontier seats, and
# this test's premise is per-agent scoping, not the current seating.
_OTHER_AGENT = (
    seat_agent_name(DispatchTier.STAFF)
    if seat_agent_name(DispatchTier.STAFF) != AGENT
    else ("codex" if AGENT == "claude" else "claude")
)
"""The other frontier vendor, for the scopes that must not reach `AGENT`."""


@pytest.fixture()
def pow_wows(work_unit_ledger: Path) -> tuple[str, str]:
    """Two real pow-wows, because the grant is keyed to one by a foreign key.

    `tool_permission_requests.pow_wow_id` REFERENCES `pow_wows`, so a scope this
    test invented would be rejected by the database rather than by the gate. That
    FK is the schema already saying what the ACL says: a permission belongs to a
    piece of work, not to an agent in general.
    """

    from local_first_agent_os.coordination.pow_wows import create_pow_wow
    from local_first_agent_os.coordination.projects import create_saga

    saga = create_saga(goal="acl test", budget_tokens=1000, budget_seconds=60)
    made = [
        create_pow_wow(
            saga_id=saga["saga_id"],
            stage="IMPLEMENTATION",
            goal=f"acl scope {index}",
            exit_criteria="none",
        )["pow_wow_id"]
        for index in range(2)
    ]
    return made[0], made[1]


def _grant(
    pow_wow_id: str,
    capability: Capability,
    *,
    agent: str = AGENT,
    status: str = "GRANTED",
) -> str:
    """A row in the given status, so there is something to take back or grant.

    The request_id is fresh per call: the regrant scenarios need a second row
    for the same scope, agent, and tool, which is exactly what the real
    request-and-grant path writes beside a REVOKED row.
    """

    request_id = f"req-{uuid.uuid4()}"
    with tx() as c:
        c.execute(
            """
            INSERT INTO tool_permission_requests(
                request_id, session_id, agent_name, task_id, pow_wow_id,
                tool_name, reason, status, granted_by, created_at, resolved_at
            ) VALUES (?, NULL, ?, NULL, ?, ?, ?, ?, 'test', 0, 0)
            """,
            (
                request_id,
                agent,
                pow_wow_id,
                capability.value,
                "declared by the compiled plan",
                status,
            ),
        )
    return request_id


def _revoke(pow_wow_id: str, capability: Capability, *, agent: str = AGENT) -> None:
    _grant(pow_wow_id, capability, agent=agent)
    result = revoke_tool_permission(
        pow_wow_id=pow_wow_id,
        agent_name=agent,
        tool_name=capability.value,
        revoked_by="operator",
    )
    assert result["ok"] is True, result


def _target(path: Path) -> LinkedProject:
    path.mkdir(parents=True, exist_ok=True)
    return LinkedProject(
        id="target",
        kind="test_repo",
        path=path,
        status="active",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="acl target",
    )


def _context(target: LinkedProject) -> PowWowExecutionContext:
    return PowWowExecutionContext(
        saga_id="saga-acl",
        goal="do the thing",
        directive="/pow-wow do the thing",
        target_project_id=target.id,
        target_project_path=str(target.expanded_path),
        target_project_kind=target.kind,
        target_project_status=target.status,
        target_project_read_only=target.read_only,
    )


def _executor(tmp_path: Path, *, ceiling: SpawnAuthority) -> CliPowWowExecutor:
    """The executor under the seating the gate resolves principals against.

    Naming the vendors here was a second copy of the seating, one that could
    disagree with the config `capability_gate` reads, in the very tests that
    exist to check what a seat may do. The binaries below say what these
    scenarios are: a denial that let a process start would be the bug.
    """

    return CliPowWowExecutor(
        worktree_root=tmp_path / "wt",
        bench=repo_bench(),
        claude_bin="claude-should-not-run",
        codex_bin="codex-should-not-run",
        spawn_ceiling=ceiling,
    )


def _task(purpose: TaskPurpose, tier: DispatchTier = DispatchTier.SENIOR) -> PowWowTaskSpec:
    return PowWowTaskSpec(
        task_name="do_the_thing",
        role="implementer",
        description="do the thing",
        purpose=purpose,
        judgment=JudgmentRole(name="implementer", tier=tier),
        dispatch_kind=(
            DispatchKind.ADVISORY if purpose is TaskPurpose.ADVISORY else DispatchKind.CODE
        ),
    )


# --- gherkin steps ------------------------------------------------------------


@pytest.fixture()
def world(tmp_path: Path) -> dict[str, Any]:
    return {"tmp_path": tmp_path}


@given(parsers.parse('the capability "{name}" is revoked for this pow-wow'))
def _revoked_here(world: dict[str, Any], name: str, pow_wows: tuple[str, str]) -> None:
    world["pow_wow"], world["other"] = pow_wows
    _revoke(world["pow_wow"], Capability(name))


@given(parsers.parse('the capability "{name}" is revoked for a different pow-wow'))
def _revoked_elsewhere(world: dict[str, Any], name: str, pow_wows: tuple[str, str]) -> None:
    world["pow_wow"], world["other"] = pow_wows
    _revoke(world["other"], Capability(name))


@given("nothing has been revoked")
def _nothing_revoked(world: dict[str, Any], pow_wows: tuple[str, str]) -> None:
    world["pow_wow"], world["other"] = pow_wows
    assert revoked_capabilities_for(AGENT, world["pow_wow"]) == set()


@when(parsers.parse('the gate is asked about "{name}"'))
def _ask(world: dict[str, Any], name: str) -> None:
    world["verdict"] = check_capability(
        agent_name=AGENT,
        agent_role="implementer",
        capability=Capability(name),
        pow_wow_id=world["pow_wow"],
    )


@then(parsers.parse('the gate says "{verdict}"'))
def _says(world: dict[str, Any], verdict: str) -> None:
    expected = CapabilityDenied if verdict == "denied" else CapabilityGranted
    assert isinstance(world["verdict"], expected), world["verdict"]


@given("a code task whose plan permits writing and running")
def _code_task(world: dict[str, Any], pow_wows: tuple[str, str]) -> None:
    world["pow_wow"], world["other"] = pow_wows
    world["task"] = _task(TaskPurpose.IMPLEMENTATION)
    world["ceiling"] = SpawnAuthority.of(
        (Capability.READ_REPOSITORY, Capability.WRITE_REPOSITORY, Capability.RUN_COMMAND)
    )


@given("an advisory task whose plan permits only reading")
def _advisory_task(world: dict[str, Any], pow_wows: tuple[str, str]) -> None:
    world["pow_wow"], world["other"] = pow_wows
    world["task"] = _task(TaskPurpose.ADVISORY)
    world["ceiling"] = SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.INVOKE_MODEL))


@given(parsers.parse('"{name}" is revoked for its pow-wow'))
def _revoked_for_its(world: dict[str, Any], name: str) -> None:
    _revoke(world["pow_wow"], Capability(name))


@when("the task is routed")
def _route(world: dict[str, Any]) -> None:
    executor = _executor(world["tmp_path"], ceiling=world["ceiling"])
    target = _target(world["tmp_path"] / "repo")
    world["denial"] = executor._authorize_spawn(
        world["task"], pow_wow_id=world["pow_wow"], agent_name=AGENT
    )
    if world["denial"] is not None:
        world["result"] = executor._build_capability_denied_result(
            world["task"],
            target_project=target,
            agent_name=AGENT,
            denial=world["denial"],
        )


@then("the task fails without spawning a process")
def _failed_unspawned(world: dict[str, Any]) -> None:
    assert world["denial"] is not None
    assert world["result"].status == "failed"


@then("the failure names the capability and how to restore it")
def _names_remedy(world: dict[str, Any]) -> None:
    risk = world["result"].risks[0]
    assert "write_repository" in risk
    assert "restore_tool_permission" in risk


@then("the task is not refused by the gate")
def _not_refused(world: dict[str, Any]) -> None:
    assert world["denial"] is None


# --- unit tests: one per decision variable on the gate path -------------------


# Variable 1: whether the gate can refuse at all.
def test_the_gate_can_say_no(pow_wows: tuple[str, str]) -> None:
    pow_wow, _ = pow_wows
    """The defect, as an assertion.

    `check_capability` asked the policy engine whether `write_repository` was a
    forbidden *tool name*. The policy matches names like `send_email` and
    `git_merge`, and no `Capability` value is in any of those sets, so every
    capability was allowed always. It looked like enforcement because it called
    the policy engine.
    """

    before = check_capability(
        agent_name=AGENT,
        agent_role="implementer",
        capability=Capability.WRITE_REPOSITORY,
        pow_wow_id=pow_wow,
    )
    assert isinstance(before, CapabilityGranted)

    _revoke(pow_wow, Capability.WRITE_REPOSITORY)

    after = check_capability(
        agent_name=AGENT,
        agent_role="implementer",
        capability=Capability.WRITE_REPOSITORY,
        pow_wow_id=pow_wow,
    )
    assert isinstance(after, CapabilityDenied)


# Variable 2: whether the capability is gated.
@pytest.mark.parametrize("capability", sorted(gated_capabilities()))
def test_every_gated_capability_can_be_revoked(
    pow_wows: tuple[str, str], capability: Capability
) -> None:
    pow_wow, _ = pow_wows
    """All five, not just the one that was easy to test."""

    _revoke(pow_wow, capability)

    verdict = check_capability(
        agent_name=AGENT,
        agent_role="implementer",
        capability=capability,
        pow_wow_id=pow_wow,
    )
    assert isinstance(verdict, CapabilityDenied)
    assert capability.value in verdict.reason


@pytest.mark.parametrize(
    "capability",
    sorted(set(Capability) - gated_capabilities()),
)
def test_an_ungated_capability_has_nothing_to_revoke(
    pow_wows: tuple[str, str], capability: Capability
) -> None:
    pow_wow, _ = pow_wows
    """`_CLEARS` is the definition of gated, and the gate honours it.

    Revoking something that implies no approval class is a no-op rather than a
    denial, because there was never an approval standing behind it.
    """

    _revoke(pow_wow, capability, agent="unlisted-agent")

    # An agent with no section in POLICIES.md, so the written document defers to
    # the compiled plan and this test asks only what it means to ask: whether the
    # *revocation* check refuses an ungated capability. A principal with a `May:`
    # allowlist would be refused here by the document, which is the document
    # working rather than the revocation check failing.
    verdict = check_capability(
        agent_name="unlisted-agent",
        agent_role="analyst",
        capability=capability,
        pow_wow_id=pow_wow,
    )
    assert isinstance(verdict, CapabilityGranted)


# Variable 3: whether the revocation is scoped to this pow-wow.
def test_a_revocation_elsewhere_does_not_reach_this_pow_wow(
    pow_wows: tuple[str, str],
) -> None:
    pow_wow, other = pow_wows
    """The scope is the whole reason `AgentCaller.pow_wow_id` is required."""

    _revoke(other, Capability.WRITE_REPOSITORY)

    verdict = check_capability(
        agent_name=AGENT,
        agent_role="implementer",
        capability=Capability.WRITE_REPOSITORY,
        pow_wow_id=pow_wow,
    )
    assert isinstance(verdict, CapabilityGranted)
    assert revoked_capabilities_for(AGENT, other) == {Capability.WRITE_REPOSITORY}


def test_a_revocation_is_scoped_to_the_agent_too(pow_wows: tuple[str, str]) -> None:
    pow_wow, _ = pow_wows
    _revoke(pow_wow, Capability.WRITE_REPOSITORY, agent=_OTHER_AGENT)

    verdict = check_capability(
        agent_name=AGENT,
        agent_role="implementer",
        capability=Capability.WRITE_REPOSITORY,
        pow_wow_id=pow_wow,
    )
    assert isinstance(verdict, CapabilityGranted)


def test_the_principal_cannot_be_built_without_a_scope() -> None:
    """The fail-open, closed in the type.

    `pow_wow_id` defaulted to `None`, and `granted_violations_for` widened to
    every grant that agent name ever received in any pow-wow when none was given.
    A caller believed it was asking "may this agent do this here".
    """

    with pytest.raises(TypeError):
        AgentCaller(agent_name=AGENT, agent_role="implementer")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="pow_wow_id is required"):
        AgentCaller(agent_name=AGENT, agent_role="implementer", pow_wow_id="")
    with pytest.raises(ValueError, match="pow_wow_id is required"):
        AgentCaller(
            agent_name=AGENT,
            agent_role="implementer",
            pow_wow_id=None,  # type: ignore[arg-type]
        )

    caller = AgentCaller(agent_name=AGENT, agent_role="implementer", pow_wow_id="pow-1")
    assert caller.pow_wow_id == "pow-1"


def test_an_unscoped_grant_read_is_refused(pow_wows: tuple[str, str]) -> None:
    """The query cannot widen when a caller violates the typed boundary.

    The real row matters: without it, returning an accidental union and
    returning an intentionally empty result would look identical.
    """

    pow_wow, other = pow_wows
    capability = Capability.WRITE_REPOSITORY
    cleared_violation = violation_cleared_by(capability)
    assert cleared_violation is not None
    _grant(other, capability)

    assert cleared_violation not in granted_violations_for(AGENT, pow_wow)
    with pytest.raises(ValueError, match="pow_wow_id is required"):
        granted_violations_for(AGENT, None)  # type: ignore[arg-type]


# Variable 4: which spawn path is running.
def test_a_revoked_capability_stops_a_code_spawn(pow_wows: tuple[str, str], tmp_path: Path) -> None:
    pow_wow, _ = pow_wows
    _revoke(pow_wow, Capability.WRITE_REPOSITORY)
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

    denial = executor._authorize_spawn(
        _task(TaskPurpose.IMPLEMENTATION), pow_wow_id=pow_wow, agent_name=AGENT
    )

    assert denial is not None
    assert denial.capability is Capability.WRITE_REPOSITORY


def test_a_revocation_the_task_never_needed_does_not_stop_it(
    pow_wows: tuple[str, str], tmp_path: Path
) -> None:
    """A reviewer is not stopped by a writer's revocation.

    `narrowed_to` already removed `write_repository` from an advisory task, so it
    is not among the capabilities the gate is asked about.
    """

    pow_wow, _ = pow_wows
    _revoke(pow_wow, Capability.WRITE_REPOSITORY)
    executor = _executor(
        tmp_path,
        ceiling=SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.INVOKE_MODEL)),
    )

    denial = executor._authorize_spawn(
        _task(TaskPurpose.ADVISORY), pow_wow_id=pow_wow, agent_name=AGENT
    )

    assert denial is None


def test_the_local_delegate_lane_is_gated_too(pow_wows: tuple[str, str], tmp_path: Path) -> None:
    pow_wow, _ = pow_wows
    """The check belongs at every way in, not only the ones that spawn a process."""

    _revoke(pow_wow, Capability.RUN_COMMAND, agent="pi")
    executor = _executor(
        tmp_path,
        ceiling=SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.RUN_COMMAND)),
    )

    denial = executor._authorize_spawn(
        _task(TaskPurpose.DETERMINISTIC_CHECK, tier=DispatchTier.JUNIOR),
        pow_wow_id=pow_wow,
        agent_name="pi",
    )

    assert denial is not None
    assert denial.capability is Capability.RUN_COMMAND


# Variable 5: what a denial does to the task.
def test_a_denial_is_a_recorded_failure_not_a_crash(
    pow_wows: tuple[str, str], tmp_path: Path
) -> None:
    """An exception here would discard every sibling task's durable work.

    `ensure_capability` raising is right for a tool call inside one workflow. The
    caller here is a scheduler holding the results of a whole pow-wow.
    """

    pow_wow, _ = pow_wows
    _revoke(pow_wow, Capability.RUN_COMMAND)
    executor = _executor(
        tmp_path,
        ceiling=SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.RUN_COMMAND)),
    )
    task = _task(TaskPurpose.IMPLEMENTATION)
    denial = executor._authorize_spawn(task, pow_wow_id=pow_wow, agent_name=AGENT)
    assert denial is not None

    result = executor._build_capability_denied_result(
        task,
        target_project=_target(tmp_path / "repo"),
        agent_name=AGENT,
        denial=denial,
    )

    assert result.status == "failed"
    assert result.artifacts[0].artifact_type == "agent_capability_denied"
    assert result.artifacts[0].content["capability"] == "run_command"


def test_a_revocation_denial_names_the_restore_that_would_lift_it(
    pow_wows: tuple[str, str], tmp_path: Path
) -> None:
    """The remedy must be the one that works.

    This denial used to print the request-and-grant path, which cannot lift a
    revocation: following it wrote a second row beside the REVOKED row that
    kept refusing, and the printed way out was a dead end. The message now
    names the exact restore command, arguments included.
    """

    pow_wow, _ = pow_wows
    _revoke(pow_wow, Capability.WRITE_REPOSITORY)
    executor = _executor(
        tmp_path,
        ceiling=SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.WRITE_REPOSITORY)),
    )
    task = _task(TaskPurpose.IMPLEMENTATION)
    denial = executor._authorize_spawn(task, pow_wow_id=pow_wow, agent_name=AGENT)
    assert denial is not None

    assert "restore_tool_permission" in denial.remedy
    assert pow_wow in denial.remedy
    assert AGENT in denial.remedy
    assert Capability.WRITE_REPOSITORY.value in denial.remedy
    assert "request_tool_permission" not in denial.remedy


# Variable 6: the revocation command itself.
def test_revoking_something_never_granted_is_refused(pow_wows: tuple[str, str]) -> None:
    pow_wow, _ = pow_wows
    """Reporting success for a row that was not there would be a report that
    cannot say no."""

    result = revoke_tool_permission(
        pow_wow_id=pow_wow,
        agent_name=AGENT,
        tool_name=Capability.WRITE_REPOSITORY.value,
        revoked_by="operator",
    )

    assert result["ok"] is False
    assert result["error"] == "not_granted"


def test_revoking_an_unknown_capability_is_refused(pow_wows: tuple[str, str]) -> None:
    pow_wow, _ = pow_wows
    result = revoke_tool_permission(
        pow_wow_id=pow_wow,
        agent_name=AGENT,
        tool_name="become_root",
        revoked_by="operator",
    )

    assert result["ok"] is False
    assert result["error"] == "unknown_capability"


def test_revoking_twice_is_refused_the_second_time(pow_wows: tuple[str, str]) -> None:
    pow_wow, _ = pow_wows
    _grant(pow_wow, Capability.WRITE_REPOSITORY)
    first = revoke_tool_permission(
        pow_wow_id=pow_wow,
        agent_name=AGENT,
        tool_name=Capability.WRITE_REPOSITORY.value,
        revoked_by="operator",
    )
    second = revoke_tool_permission(
        pow_wow_id=pow_wow,
        agent_name=AGENT,
        tool_name=Capability.WRITE_REPOSITORY.value,
        revoked_by="operator",
    )

    assert first["ok"] is True
    assert second["ok"] is False


def test_an_outage_seated_implementer_is_authorized_by_its_plan_seat(
    pow_wows: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The 2026-08-29 milestone denial, replayed through the spawn path.

    The static staffing file declares the cross-vendor pairing; a fallback
    seating has claude implementing anyway. The task arrives with role
    ``implementer`` and judgment tier SENIOR. The gate used to hear only the
    role, fall to the bench, resolve claude to its static seat ``staff``, and
    refuse ``run_command`` to the implementer its own plan had granted it
    (work unit e5d41f8805f4f955d7b1e832cc7fd4ee, milestone b). The spawn path
    now declares the plan's seat, so the bench is never asked.
    """

    pow_wow, _ = pow_wows
    configs = tmp_path / "configs"
    configs.mkdir(exist_ok=True)
    (configs / "staffing.toml").write_text(
        'seated_pairing = "cross-vendor"\n'
        "\n"
        "[pairings.cross-vendor.senior]\n"
        'harness = "codex"\n'
        'model = "gpt-test"\n'
        "capacity = 2\n"
        "\n"
        "[pairings.cross-vendor.staff]\n"
        'harness = "claude"\n'
        'model = "claude-test"\n'
        "capacity = 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCAL_AGENT_CONFIG_DIR", str(configs))
    get_settings.cache_clear()

    # Seat-blind, the bench misfiles the outage implementer as the reviewer.
    # This is the live denial, verbatim but for the ids.
    seat_blind = check_capability(
        agent_name="claude",
        agent_role="implementer",
        capability=Capability.RUN_COMMAND,
        pow_wow_id=pow_wow,
    )
    assert isinstance(seat_blind, CapabilityDenied)
    assert "principal 'staff'" in seat_blind.reason

    ceiling = SpawnAuthority.of(
        (Capability.READ_REPOSITORY, Capability.WRITE_REPOSITORY, Capability.RUN_COMMAND)
    )
    executor = _executor(tmp_path, ceiling=ceiling)
    task = _task(TaskPurpose.IMPLEMENTATION)

    denial = executor._authorize_spawn(task, pow_wow_id=pow_wow, agent_name="claude")

    assert denial is None


# Variable 7: the restore command, the operator-held key to the one-way door.
def test_a_regrant_does_not_lift_a_revocation(pow_wows: tuple[str, str]) -> None:
    pow_wow, _ = pow_wows
    """The dead end, as an assertion.

    The old refusal's own printed remedy was request again, grant again. That
    writes a fresh GRANTED row beside the REVOKED one, and the REVOKED row is
    what the gate reads: written policy outranks grants, and a revocation is
    the operator's written change of mind, so it keeps refusing until an
    operator restores it rather than until somebody grants around it.
    """

    _revoke(pow_wow, Capability.WRITE_REPOSITORY)
    _grant(pow_wow, Capability.WRITE_REPOSITORY)

    verdict = check_capability(
        agent_name=AGENT,
        agent_role="implementer",
        capability=Capability.WRITE_REPOSITORY,
        pow_wow_id=pow_wow,
    )
    assert isinstance(verdict, CapabilityDenied)
    assert "restore_tool_permission" in verdict.remedy


def test_granting_over_a_standing_revocation_says_so(pow_wows: tuple[str, str]) -> None:
    pow_wow, _ = pow_wows
    """The grant is not refused, but it must not be silent about changing nothing."""

    _revoke(pow_wow, Capability.WRITE_REPOSITORY)
    request_id = _grant(pow_wow, Capability.WRITE_REPOSITORY, status="PENDING")

    granted = grant_tool_permission(request_id, granted_by="operator")

    assert granted["ok"] is True
    assert granted["standing_revocation"] is True
    assert "restore_tool_permission" in granted["warning"]


def test_a_restored_revocation_no_longer_refuses(pow_wows: tuple[str, str], tmp_path: Path) -> None:
    pow_wow, _ = pow_wows
    _revoke(pow_wow, Capability.WRITE_REPOSITORY)

    restored = restore_tool_permission(
        pow_wow_id=pow_wow,
        agent_name=AGENT,
        tool_name=Capability.WRITE_REPOSITORY.value,
        restored_by="operator",
        reason="false alarm; the agent's write was the planned one",
    )
    assert restored["ok"] is True, restored
    assert restored["status"] == "RESTORED"
    _grant(pow_wow, Capability.WRITE_REPOSITORY)

    verdict = check_capability(
        agent_name=AGENT,
        agent_role="implementer",
        capability=Capability.WRITE_REPOSITORY,
        pow_wow_id=pow_wow,
    )
    assert isinstance(verdict, CapabilityGranted)

    executor = _executor(
        tmp_path,
        ceiling=SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.WRITE_REPOSITORY)),
    )
    denial = executor._authorize_spawn(
        _task(TaskPurpose.IMPLEMENTATION), pow_wow_id=pow_wow, agent_name=AGENT
    )
    assert denial is None


def test_restoring_nothing_revoked_is_refused(pow_wows: tuple[str, str]) -> None:
    pow_wow, _ = pow_wows
    """A restore that cannot say no would let an operator believe a refusal was
    lifted when there was never one standing."""

    result = restore_tool_permission(
        pow_wow_id=pow_wow,
        agent_name=AGENT,
        tool_name=Capability.WRITE_REPOSITORY.value,
        restored_by="operator",
        reason="nothing to lift",
    )

    assert result["ok"] is False
    assert result["error"] == "not_revoked"


def test_restoring_twice_is_refused_the_second_time(pow_wows: tuple[str, str]) -> None:
    pow_wow, _ = pow_wows
    _revoke(pow_wow, Capability.WRITE_REPOSITORY)
    first = restore_tool_permission(
        pow_wow_id=pow_wow,
        agent_name=AGENT,
        tool_name=Capability.WRITE_REPOSITORY.value,
        restored_by="operator",
        reason="lifting the refusal",
    )
    second = restore_tool_permission(
        pow_wow_id=pow_wow,
        agent_name=AGENT,
        tool_name=Capability.WRITE_REPOSITORY.value,
        restored_by="operator",
        reason="lifting it again",
    )

    assert first["ok"] is True
    assert second["ok"] is False
    assert second["error"] == "not_revoked"


def test_restoring_an_unknown_capability_is_refused(pow_wows: tuple[str, str]) -> None:
    pow_wow, _ = pow_wows
    result = restore_tool_permission(
        pow_wow_id=pow_wow,
        agent_name=AGENT,
        tool_name="become_root",
        restored_by="operator",
        reason="no such door",
    )

    assert result["ok"] is False
    assert result["error"] == "unknown_capability"


def test_restoring_without_a_reason_is_refused(pow_wows: tuple[str, str]) -> None:
    pow_wow, _ = pow_wows
    """Lifting a refusal is the audited act; a blank reason says nothing."""

    _revoke(pow_wow, Capability.WRITE_REPOSITORY)
    result = restore_tool_permission(
        pow_wow_id=pow_wow,
        agent_name=AGENT,
        tool_name=Capability.WRITE_REPOSITORY.value,
        restored_by="operator",
        reason="   ",
    )

    assert result["ok"] is False
    assert result["error"] == "missing_reason"


def test_a_restore_can_itself_be_revoked_again(pow_wows: tuple[str, str]) -> None:
    pow_wow, _ = pow_wows
    """RESTORED is terminal and neutral: it does not grant, so revoking again
    needs a fresh GRANTED row to take back, exactly like the first time."""

    _revoke(pow_wow, Capability.WRITE_REPOSITORY)
    restore_tool_permission(
        pow_wow_id=pow_wow,
        agent_name=AGENT,
        tool_name=Capability.WRITE_REPOSITORY.value,
        restored_by="operator",
        reason="lifting the refusal",
    )
    _revoke(pow_wow, Capability.WRITE_REPOSITORY)

    verdict = check_capability(
        agent_name=AGENT,
        agent_role="implementer",
        capability=Capability.WRITE_REPOSITORY,
        pow_wow_id=pow_wow,
    )
    assert isinstance(verdict, CapabilityDenied)


def test_concurrent_gate_checks_do_not_crash(pow_wows: tuple[str, str]) -> None:
    pow_wow, _ = pow_wows
    """The gate is asked once per spawn, and junior tasks spawn on a pool."""

    _revoke(pow_wow, Capability.WRITE_REPOSITORY)
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def ask() -> None:
        try:
            barrier.wait(timeout=10)
            check_capability(
                agent_name=AGENT,
                agent_role="implementer",
                capability=Capability.WRITE_REPOSITORY,
                pow_wow_id=pow_wow,
            )
        except BaseException as exc:  # noqa: BLE001 - the assertion is "none of these"
            errors.append(exc)

    threads = [threading.Thread(target=ask) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
