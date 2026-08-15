# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What the entry points to the local stack resolve to before Docker is involved.

Two facts about `docker-compose.yml` are asserted here rather than described
somewhere: the project every entry point lands in, and which services each named
stack is made of. Both used to live outside the compose file, copied into each
caller, and both failed the same way when a copy was missed.

The project-name tests need the Docker CLI to resolve a project and skip without
it. The stack-membership scenarios in `features/compose_profiles.feature` need no
daemon at all: they apply Compose's own profile selection rule to the file, and
read the argv the scripts hand to a recording stand-in for `docker`.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from pytest_bdd import given, parsers, scenarios, then, when

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
START_INFRA = REPOSITORY_ROOT / "scripts" / "start-docker-compose-infra.sh"
STOP_INFRA = REPOSITORY_ROOT / "scripts" / "stop-docker-compose-infra.sh"
COMPOSE_FILE = REPOSITORY_ROOT / "docker-compose.yml"

scenarios("features/compose_profiles.feature")

# The one project every entry point has to land in. Compose otherwise names the
# project after the invoking directory, and a worktree is a different directory.
EXPECTED_PROJECT = "local_first_agent_os"

needs_docker = pytest.mark.skipif(
    shutil.which("docker") is None, reason="resolving a project name requires the docker CLI"
)


def _resolved_project_name(working_directory: Path) -> str:
    """The project Compose would use for this repository's file, run from there."""

    completed = subprocess.run(
        ["docker", "compose", "config"],
        cwd=working_directory,
        check=True,
        capture_output=True,
        text=True,
    )
    for line in completed.stdout.splitlines():
        if line.startswith("name:"):
            return line.removeprefix("name:").strip()
    raise AssertionError("`docker compose config` reported no project name")


def _worktree_like(tmp_path: Path) -> Path:
    """A copy of the compose file under a directory named the way a worktree is.

    The name is deliberately one a branch would generate, because that is the
    input that used to break: the project took the directory's name and the
    containers outlived the checkout.
    """

    worktree = tmp_path / "some-branch-a1b2c3"
    worktree.mkdir()
    shutil.copy(COMPOSE_FILE, worktree / "docker-compose.yml")
    return worktree


@needs_docker
def test_the_project_name_does_not_follow_the_directory(tmp_path: Path) -> None:
    """A worktree must not get a project, and therefore a container set, of its own.

    Every service pins `container_name`, and those names are global to the daemon.
    So a second project cannot run alongside the first; it can only collide with
    it, or strand it when the checkout is deleted.
    """

    assert _resolved_project_name(_worktree_like(tmp_path)) == EXPECTED_PROJECT


@needs_docker
def test_the_canonical_checkout_and_a_worktree_agree(tmp_path: Path) -> None:
    """Both spellings of "run it here" have to mean the same stack.

    Asserting the two resolve equal is what actually rules out the split, rather
    than asserting each separately against a constant that could drift with them.
    """

    assert _resolved_project_name(_worktree_like(tmp_path)) == _resolved_project_name(
        REPOSITORY_ROOT
    )


@needs_docker
def test_an_operator_can_still_ask_for_a_private_stack(tmp_path: Path) -> None:
    """The pin is a default, not a lock, and the escape hatch is what makes it safe.

    Pinning in the compose file rather than in each script only stays defensible
    while an explicit request still wins, so that is asserted rather than assumed.
    """

    worktree = _worktree_like(tmp_path)
    completed = subprocess.run(
        ["docker", "compose", "config"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "COMPOSE_PROJECT_NAME": "deliberately_separate"},
    )

    assert "name: deliberately_separate" in completed.stdout


@needs_docker
def test_the_cli_agrees_a_bare_invocation_selects_nothing(tmp_path: Path) -> None:
    """Compose's own resolution of the file with no profile active is empty.

    The scenarios below apply the selection rule in pure code; this is the one
    place the real CLI is asked to confirm the rule reads the file the same way.
    `config` resolves client-side, so no daemon is required. The practical edge
    this pins: a bare `docker compose down` selects nothing, does nothing, and
    exits 0, which is why the compose file documents `--profile "*"` as the
    whole-project teardown.
    """

    worktree = _worktree_like(tmp_path)
    bare = subprocess.run(
        ["docker", "compose", "config", "--services"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    assert bare.stdout.strip() == ""

    core = subprocess.run(
        ["docker", "compose", "--profile", "core", "config", "--services"],
        cwd=worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    assert core.stdout.split() == ["postgres"]


def _docker_stub(directory: Path, *, body: str) -> Path:
    """A `docker` on PATH whose whole behavior is the given shell body."""

    directory.mkdir(parents=True, exist_ok=True)
    executable = directory / "docker"
    executable.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def _failing_docker(directory: Path) -> Path:
    return _docker_stub(directory, body="exit 1")


def _path_with_stub_docker(directory: Path) -> str:
    return os.pathsep.join((str(directory), "/usr/bin", "/bin", "/usr/sbin", "/sbin"))


def test_launchd_mode_silences_expected_docker_absence(tmp_path: Path) -> None:
    _failing_docker(tmp_path)
    environment = {
        **os.environ,
        "PATH": _path_with_stub_docker(tmp_path),
        "LOCAL_AGENT_QUIET_DOCKER_UNAVAILABLE": "true",
    }

    completed = subprocess.run(
        ["/bin/bash", str(START_INFRA), "postgres"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_interactive_mode_explains_docker_absence(tmp_path: Path) -> None:
    _failing_docker(tmp_path)
    environment = {**os.environ, "PATH": _path_with_stub_docker(tmp_path)}
    environment.pop("LOCAL_AGENT_QUIET_DOCKER_UNAVAILABLE", None)

    completed = subprocess.run(
        ["/bin/bash", str(START_INFRA), "postgres"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == (
        "Docker is unavailable; start Docker before requesting local infrastructure.\n"
    )


# --- Scenario steps for `features/compose_profiles.feature` -----------------


@dataclass(frozen=True, slots=True)
class _StackRequest:
    """One command an operator can type at the infrastructure scripts.

    The script and the stack name are one value because neither half is
    meaningful alone: a stack name on its own is a string, and a script that has
    not been asked for anything has no behavior to assert. Keeping them together
    is what lets the whole operator surface be one list that a new stack has to
    be added to before it can be tested at all.
    """

    script: Path
    stack: str

    @property
    def workspace_name(self) -> str:
        return f"{self.script.stem}-{self.stack}"


# Every way an operator can name a stack at the two compose infrastructure
# scripts. That pair is the whole surface this list speaks for; the scripts that
# still keep their own service lists are covered by the copy tripwire scenario
# instead. A new arm in either `case` statement that is not added here is
# untested, which is the point of writing it out.
INFRASTRUCTURE_SURFACE: tuple[_StackRequest, ...] = (
    _StackRequest(START_INFRA, "postgres"),
    _StackRequest(START_INFRA, "observability-minimal"),
    _StackRequest(START_INFRA, "observability"),
    _StackRequest(STOP_INFRA, "postgres"),
    _StackRequest(STOP_INFRA, "observability"),
)

RECORDED_ARGV_VARIABLE = "LOCAL_AGENT_RECORDED_DOCKER_ARGV"


def _compose_services() -> Mapping[str, Mapping[str, Any]]:
    """The services stanza, or a loud failure.

    A compose file this cannot read is a broken compose file, not a reason to
    fall back to an empty mapping. Every assertion below is of the form "this
    set is exactly that set", and an empty mapping satisfies rather more of them
    than it should.
    """

    document: Any = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    services = document["services"]
    assert isinstance(services, dict), "docker-compose.yml declares no services mapping"
    return services


def _declared_profiles() -> Mapping[str, frozenset[str]]:
    return {name: frozenset(body.get("profiles", ())) for name, body in _compose_services().items()}


def _selected_services(profiles: Iterable[str]) -> frozenset[str]:
    """Compose's own selection rule, applied to the file rather than by the daemon.

    A service with no `profiles:` key is selected by every command; one with
    profiles is selected only when a profile it declares is active. Modelling the
    rule rather than pinning the outcome is what makes the teardown asymmetry
    testable: delete postgres's `core` profile and it becomes profile-less, and
    this starts returning it for `--profile observability`, which is the exact
    breakage that profile exists to prevent.
    """

    active = frozenset(profiles)
    return frozenset(
        service
        for service, declared in _declared_profiles().items()
        if not declared or (declared & active)
    )


def _recording_docker(directory: Path) -> Path:
    """A `docker` that agrees to everything and writes down what it was asked for.

    The subject is the script, not the daemon, and there is no daemon in a test
    run. Recording argv keeps the assertions about what a script asks Compose
    for, so rewriting the script in a different shell idiom that still asks for
    the right stack goes on passing.
    """

    return _docker_stub(directory, body=f'echo "$*" >> "${RECORDED_ARGV_VARIABLE}"\nexit 0')


def _compose_argv(request: _StackRequest, workspace: Path) -> tuple[str, ...]:
    """The single `docker compose` command one operator command produces."""

    _recording_docker(workspace)
    recording = workspace / "recorded-argv"
    completed = subprocess.run(
        ["/bin/bash", str(request.script), request.stack],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": _path_with_stub_docker(workspace),
            RECORDED_ARGV_VARIABLE: str(recording),
        },
    )

    assert completed.returncode == 0, completed.stderr
    invocations = [line.split() for line in recording.read_text(encoding="utf-8").splitlines()]
    compose_invocations = [argv for argv in invocations if argv[:1] == ["compose"]]
    assert len(compose_invocations) == 1, (
        f"{request.script.name} {request.stack} ran {len(compose_invocations)} compose commands; "
        "one stack is one command, or the mapping in the design doc no longer holds"
    )
    return tuple(compose_invocations[0])


def _requested_profiles(argv: Sequence[str]) -> tuple[str, ...]:
    return tuple(value for flag, value in zip(argv, argv[1:], strict=False) if flag == "--profile")


def _without_profile_arguments(argv: Iterable[str]) -> tuple[str, ...]:
    """The argv with every `--profile <name>` pair removed.

    The one place argv is split into "profile arguments" and "everything else".
    `app` is both a profile and a service in the same file, so any reading that
    intersects raw argv with service names accuses `--profile app` of naming a
    service; parsing positionally is what keeps the two namespaces apart.
    """

    remaining: list[str] = []
    tokens = iter(argv)
    for token in tokens:
        if token == "--profile":
            next(tokens, None)
        else:
            remaining.append(token)
    return tuple(remaining)


_STACK_ARRAY_ASSIGNMENT = (
    r"^[ \t]*(?:readonly[ \t]+|declare[ \t]+-[A-Za-z]+[ \t]+)?{array}=\(([^)]*)\)"
)


def _copied_stack_list(script: Path, array: str) -> frozenset[str] | None:
    """A service list still hard-coded in a shell script, if one is left.

    `None` means the copy is gone from the script entirely, which is the outcome
    the scenario reading this is waiting for. A name that is still present but
    no longer parses is neither of those things: it means this tripwire has been
    reformatted out of its own view, so it fails loudly instead of disarming.

    `[^)]*` rather than `.*` so the one-line and the one-per-line spellings of a
    bash array both read the same, since which one a script uses says nothing
    about whether its contents have drifted.
    """

    text = script.read_text(encoding="utf-8")
    match = re.search(_STACK_ARRAY_ASSIGNMENT.format(array=re.escape(array)), text, re.MULTILINE)
    if match is not None:
        return frozenset(match.group(1).split())
    if re.search(rf"\b{re.escape(array)}\b", text):
        pytest.fail(
            f"{script.name} still mentions {array} but no array assignment parses; "
            "the drift tripwire cannot see the copy it exists to watch. Restore a "
            f"plain `{array}=( ... )` assignment, or remove the copy entirely."
        )
    return None


@dataclass(frozen=True, slots=True)
class _RecordedInvocation:
    """The one compose command a script run produced, and what it selects.

    A `Then` can only hold one of these because the `When` that ran the script
    returned it as the scenario's fixture, so "assert before anything ran" is a
    missing-fixture error from the framework rather than a state this module has
    to police with runtime checks.
    """

    argv: tuple[str, ...]

    @property
    def subcommand(self) -> tuple[str, ...]:
        return _without_profile_arguments(self.argv)

    @property
    def selected(self) -> frozenset[str]:
        return _selected_services(_requested_profiles(self.argv))


@pytest.fixture(scope="session")
def infrastructure_surface(
    tmp_path_factory: pytest.TempPathFactory,
) -> Mapping[_StackRequest, _RecordedInvocation]:
    """Every operator command's argv, recorded once for the whole run.

    Session-scoped because the scripts and the compose file are the same files
    in every scenario; each script run is a `/bin/bash` subprocess, and paying
    five of them per scenario bought nothing.
    """

    workspace = tmp_path_factory.mktemp("infrastructure-surface")
    return {
        request: _RecordedInvocation(_compose_argv(request, workspace / request.workspace_name))
        for request in INFRASTRUCTURE_SURFACE
    }


def _expected_services(listed: str) -> frozenset[str]:
    return frozenset(name.strip() for name in listed.split(",") if name.strip())


@given("the compose file and the infrastructure scripts as they ship")
def _the_repository_as_it_ships() -> None:
    """No fixture stands in for either file.

    A copy of the compose file with profiles added by the test would assert that
    profiles work, which nobody doubts, rather than that this repository uses
    them.
    """


@when(parsers.parse('the start script is asked for "{stack}"'), target_fixture="invocation")
def _ask_start(tmp_path: Path, stack: str) -> _RecordedInvocation:
    request = _StackRequest(START_INFRA, stack)
    return _RecordedInvocation(_compose_argv(request, tmp_path / request.workspace_name))


@when(parsers.parse('the stop script is asked for "{stack}"'), target_fixture="invocation")
def _ask_stop(tmp_path: Path, stack: str) -> _RecordedInvocation:
    request = _StackRequest(STOP_INFRA, stack)
    return _RecordedInvocation(_compose_argv(request, tmp_path / request.workspace_name))


@then(parsers.parse('the services brought up are "{listed}"'))
def _brought_up(invocation: _RecordedInvocation, listed: str) -> None:
    assert invocation.subcommand == ("compose", "up", "-d"), (
        f"the start script ran `docker {' '.join(invocation.argv)}`; "
        "a stack is brought up by profile, with nothing else on the command line"
    )
    assert invocation.selected == _expected_services(listed)


@then(parsers.parse('the services taken down are "{listed}"'))
def _taken_down(invocation: _RecordedInvocation, listed: str) -> None:
    assert invocation.subcommand == ("compose", "rm", "--stop", "--force"), (
        f"the stop script ran `docker {' '.join(invocation.argv)}`; "
        "teardown is stop-then-remove, and dropping `--stop` would force-remove "
        "running containers, one of which holds the coordination ledger"
    )
    assert invocation.selected == _expected_services(listed)


@then(parsers.parse('"{service}" is spared'))
def _spared(invocation: _RecordedInvocation, service: str) -> None:
    assert service not in invocation.selected, (
        f"{service} was selected by `docker {' '.join(invocation.argv)}`; "
        "a service with no profile of its own is selected by every profile-scoped command"
    )


@then("the compose infrastructure scripts name no service on a compose command line")
def _scripts_name_no_service(
    infrastructure_surface: Mapping[_StackRequest, _RecordedInvocation],
) -> None:
    services = frozenset(_declared_profiles())
    for request, invocation in infrastructure_surface.items():
        named = services & frozenset(_without_profile_arguments(invocation.argv))
        assert not named, (
            f"{request.script.name} {request.stack} passes {sorted(named)} to Compose; "
            "which services a stack contains belongs in docker-compose.yml"
        )


@then("every stack an infrastructure script asks for is declared in the compose file")
def _stacks_exist(
    infrastructure_surface: Mapping[_StackRequest, _RecordedInvocation],
) -> None:
    declared = frozenset().union(*_declared_profiles().values())
    for request, invocation in infrastructure_surface.items():
        for profile in _requested_profiles(invocation.argv):
            assert profile in declared, (
                f"{request.script.name} {request.stack} asks for profile {profile!r}, "
                "which no service declares; Compose would select nothing and succeed"
            )


@then("every service declares at least one stack")
def _no_profileless_service() -> None:
    homeless = sorted(name for name, profiles in _declared_profiles().items() if not profiles)
    assert not homeless, f"{homeless} would be selected by every profile-scoped command"


@then("a compose command that activates no stack selects no services")
def _bare_invocation_selects_nothing() -> None:
    assert _selected_services(()) == frozenset(), (
        "a profile-less compose command selects every profile-less service; "
        "the services above have stopped declaring stacks"
    )


@then(parsers.parse('"{service}" is in no stack an infrastructure script can ask for'))
def _outside_every_stack(
    infrastructure_surface: Mapping[_StackRequest, _RecordedInvocation],
    service: str,
) -> None:
    for request, invocation in infrastructure_surface.items():
        assert service not in invocation.selected, (
            f"{request.script.name} {request.stack} would start {service}"
        )


@then(parsers.parse('the suite\'s database autostart names "{service}" and asks for no stack'))
def _suite_autostart_names_its_service(service: str) -> None:
    """The mitigation that makes `postgres-test` safe to keep out of every stack.

    The compose file can put the suite's database in a profile nothing activates
    only because the autostart path names the service explicitly, which starts a
    service whether or not its profile is active. This pins both halves of that:
    the fixture declares this service, and the starter names its service with no
    profile in sight. Refactoring either to a profile-based start breaks every
    database test in the suite, so doing it must fail here first and be decided
    on purpose.
    """

    conftest = (REPOSITORY_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert f'compose_service="{service}"' in conftest, (
        f"tests/conftest.py no longer declares {service!r} as the suite's managed database"
    )
    starter = (REPOSITORY_ROOT / "tests" / "postgres_server.py").read_text(encoding="utf-8")
    assert '["docker", "compose", "up", "--detach", service]' in starter, (
        "tests/postgres_server.py no longer starts the suite's database by naming "
        "its service explicitly; if it now asks by stack, activate the `test` "
        "profile deliberately and update this pin"
    )
    assert "--profile" not in starter


@then(parsers.parse('the smoke script starts "{service}" by name and asks for no stack'))
def _smoke_script_names_its_service(service: str) -> None:
    smoke = (REPOSITORY_ROOT / "scripts" / "run_dbos_postgres_smoke.sh").read_text(encoding="utf-8")
    assert f"docker compose up -d {service}" in smoke, (
        f"run_dbos_postgres_smoke.sh no longer starts {service!r} by name; if it "
        "asks by stack now, its selection depends on the profile table above and "
        "this pin should be rethought rather than deleted"
    )
    assert "--profile" not in smoke


@then(parsers.parse('the "{array}" list in "{script}" is exactly the "{stacks}" stacks'))
def _copy_still_agrees(array: str, script: str, stacks: str) -> None:
    path = REPOSITORY_ROOT / "scripts" / script
    copied = _copied_stack_list(path, array)
    if copied is None:
        return
    wanted = _selected_services(name.strip() for name in stacks.split(","))
    assert copied == wanted, (
        f"{script} still keeps its own {array} list and it has drifted from the "
        f"{stacks} stacks in docker-compose.yml"
    )
