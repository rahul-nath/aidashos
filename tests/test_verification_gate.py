# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Whether a run may leave a checkpoint behind.

The scenarios in ``features/verification_gate.feature`` cover the outcome space
and both ends of it: the pure classification, the executor that acts on it, and
the registry entry that could previously author the state at all.

The first scenario is the one that matters most. Before this change the gate was
``all(capture.exit_code == 0 for capture in verification_captures)``, which is
``True`` on an empty tuple, so a project declaring ``verification_commands = []``
produced a checkpoint commit and a completed task having verified nothing. There
was no test of any kind on that path.

``_parse_linked_project_record`` is imported directly rather than driven through
``load_project_center``: the rule belongs to the record parser, and reaching it
through a settings object and a temporary TOML file would test the loader's
plumbing instead of the refusal. That the real registry still loads is asserted
separately, because a rule that rejects the repository's own configuration would
be a worse defect than the one it fixes.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from local_first_agent_os.pow_wow import FakeProcessPowWowExecutor, PowWowTaskSpec
from local_first_agent_os.pow_wow.types import CommandRunCapture, PowWowExecutionContext
from local_first_agent_os.pow_wow.verification import (
    VerificationFailed,
    VerificationIncomplete,
    VerificationNotDeclared,
    VerificationPassed,
    checkpoint_permitted,
    classify_verification,
    uncertifiable_reason,
)
from local_first_agent_os.project_access import AccessMode, ProjectAccessPolicy
from local_first_agent_os.project_center import LinkedProject, _parse_linked_project_record

scenarios("features/verification_gate.feature")

PASSING_COMMAND = f'{shlex.quote(sys.executable)} -c "pass"'
FAILING_COMMAND = f'{shlex.quote(sys.executable)} -c "raise SystemExit(1)"'

_OUTCOME_NAMES = {
    VerificationNotDeclared: "not_declared",
    VerificationIncomplete: "incomplete",
    VerificationPassed: "passed",
    VerificationFailed: "failed",
}


@pytest.fixture
def state() -> dict[str, Any]:
    return {}


def _capture(exit_code: int) -> CommandRunCapture:
    return CommandRunCapture(
        command="verify",
        cwd="/tmp",
        stdout="",
        stderr="",
        exit_code=exit_code,
    )


def _init_git_repo(path: Path) -> None:
    import subprocess

    path.mkdir(parents=True)
    for argv in (
        ["git", "init"],
        ["git", "config", "user.email", "test@example.com"],
        ["git", "config", "user.name", "Test User"],
    ):
        subprocess.run(argv, cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("# target\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)


def _target(path: Path, verification_commands: list[str]) -> LinkedProject:
    return LinkedProject(
        id="verification_fixture_project",
        kind="business_factory",
        path=path,
        status="active_product_repo",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="fixture repo",
        verification_commands=verification_commands,
    )


def _context(target: LinkedProject) -> PowWowExecutionContext:
    return PowWowExecutionContext(
        saga_id="saga-verification",
        goal="verify the verification gate",
        directive="/saga verify the verification gate",
        target_project_id=target.id,
        target_project_path=str(target.expanded_path),
        target_project_kind=target.kind,
        target_project_status=target.status,
        target_project_read_only=target.read_only,
        verification_commands=tuple(target.verification_commands),
    )


def _run_code_task(tmp_path: Path, verification_commands: list[str]) -> Any:
    repo = tmp_path / "target"
    _init_git_repo(repo)
    target = _target(repo, verification_commands)
    task = PowWowTaskSpec(
        task_name="implement_fixture",
        role="implementation_agent",
        description="write fake output",
    )
    result = FakeProcessPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        cleanup_policy="remove",
    ).dispatch_pow_wow("pow-verification", target, (task,), _context(target))
    return result.tasks[0]


# --------------------------------------------------------------------------- #
# The classification itself
# --------------------------------------------------------------------------- #


@given(parsers.parse('"{declared:d}" verification commands are declared'))
def _declared(state: dict[str, Any], declared: int) -> None:
    state["declared"] = tuple(f"command-{index}" for index in range(declared))


@given(parsers.parse('"{count:d}" of them ran with "{results}"'))
def _ran(state: dict[str, Any], count: int, results: str) -> None:
    codes = [] if results == "none" else [int(part) for part in results.split(",")]
    assert len(codes) == count, "the example row must give one exit code per capture"
    state["captures"] = tuple(_capture(code) for code in codes)


@then(parsers.parse('the verification outcome is "{expected}"'))
def _outcome_is(state: dict[str, Any], expected: str) -> None:
    outcome = classify_verification(state["declared"], state["captures"])
    state["outcome"] = outcome
    assert _OUTCOME_NAMES[type(outcome)] == expected, outcome


@then(parsers.parse('a checkpoint is "{verdict}"'))
def _checkpoint_verdict(state: dict[str, Any], verdict: str) -> None:
    permitted = checkpoint_permitted(state["outcome"])
    assert permitted is (verdict == "permitted"), state["outcome"]


# --------------------------------------------------------------------------- #
# The executor acting on it
# --------------------------------------------------------------------------- #


@given("a code task whose target project declares no verification commands")
def _no_commands(state: dict[str, Any]) -> None:
    state["verification_commands"] = []


@given("a code task whose target project declares a passing verification command")
def _passing_command(state: dict[str, Any]) -> None:
    state["verification_commands"] = [PASSING_COMMAND]


@given("a code task whose target project declares a failing verification command")
def _failing_command(state: dict[str, Any]) -> None:
    state["verification_commands"] = [FAILING_COMMAND]


@when("the task runs and its agent command succeeds")
def _run_task(state: dict[str, Any], tmp_path: Path) -> None:
    state["task_result"] = _run_code_task(tmp_path, state["verification_commands"])


@then("no checkpoint is committed")
def _no_checkpoint(state: dict[str, Any]) -> None:
    artifact_types = {artifact.artifact_type for artifact in state["task_result"].artifacts}
    assert "worktree_commit_checkpoint" not in artifact_types, artifact_types


@then("a checkpoint is committed")
def _checkpoint_committed(state: dict[str, Any]) -> None:
    artifact_types = {artifact.artifact_type for artifact in state["task_result"].artifacts}
    assert "worktree_commit_checkpoint" in artifact_types, artifact_types


@then("the task is reported failed")
def _reported_failed(state: dict[str, Any]) -> None:
    assert state["task_result"].status == "failed", state["task_result"].status


@then("the task is reported completed")
def _reported_completed(state: dict[str, Any]) -> None:
    assert state["task_result"].status == "completed", state["task_result"].status


@then("the failure names the project and how to fix it")
def _failure_names_remedy(state: dict[str, Any]) -> None:
    risks = " ".join(state["task_result"].risks)
    assert "verification_fixture_project" in risks, risks
    assert "verification_commands" in risks, risks
    assert "read_only" in risks, risks


# --------------------------------------------------------------------------- #
# The registry entry that could author the state
# --------------------------------------------------------------------------- #


def _record(*, read_only: bool, verification_commands: list[str]) -> dict[str, Any]:
    return {
        "id": "fixture_project",
        "kind": "business_factory",
        "path": "~/fixture",
        "status": "active_product_repo",
        "description": "fixture",
        "read_only": read_only,
        "verification_commands": verification_commands,
    }


@given("a linked project registry entry that is writable with no verification commands")
def _writable_empty(state: dict[str, Any]) -> None:
    state["record"] = _record(read_only=False, verification_commands=[])


@given("a linked project registry entry that is read-only with no verification commands")
def _read_only_empty(state: dict[str, Any]) -> None:
    state["record"] = _record(read_only=True, verification_commands=[])


@then("loading the registry fails")
def _registry_fails(state: dict[str, Any]) -> None:
    with pytest.raises(ValueError) as excinfo:
        _parse_linked_project_record(state["record"])
    state["complaint"] = str(excinfo.value)


@then("the complaint names the project and both remedies")
def _complaint_names(state: dict[str, Any]) -> None:
    complaint = state["complaint"]
    assert "fixture_project" in complaint, complaint
    assert "verification_commands" in complaint, complaint
    assert "read_only" in complaint, complaint


@then("loading the registry succeeds")
def _registry_succeeds(state: dict[str, Any]) -> None:
    project = _parse_linked_project_record(state["record"])
    assert project.verification_commands == []
    assert project.read_only is True


# --------------------------------------------------------------------------- #
# Unit tests beside the scenarios
# --------------------------------------------------------------------------- #


def test_an_empty_capture_tuple_no_longer_certifies() -> None:
    """The exact expression that was wrong, asserted directly.

    ``all(())`` is ``True``; the outcome for the same inputs is not.
    """

    assert all(capture.exit_code == 0 for capture in ()) is True
    assert checkpoint_permitted(classify_verification((), ())) is False


def test_only_an_undeclared_verification_produces_an_operator_sentence() -> None:
    """A failure reports itself through its captures; silence has no other voice."""

    assert uncertifiable_reason(classify_verification((), ()), target_project_id="p") is not None
    passed = classify_verification(("c",), (_capture(0),))
    assert uncertifiable_reason(passed, target_project_id="p") is None
    failed = classify_verification(("c",), (_capture(1),))
    assert uncertifiable_reason(failed, target_project_id="p") is None
    incomplete = classify_verification(("c", "d"), (_capture(0),))
    assert uncertifiable_reason(incomplete, target_project_id="p") is None


def test_the_repositorys_own_registry_still_loads() -> None:
    """A rule that rejected this repository's configuration would be the worse defect."""

    from local_first_agent_os.project_center import load_project_center

    center = load_project_center()
    writable = [project for project in center.projects if not project.read_only]
    assert writable, "the registry must still contain writable projects"
    assert all(project.verification_commands for project in writable)
