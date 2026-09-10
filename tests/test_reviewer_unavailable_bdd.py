# SPDX-License-Identifier: AGPL-3.0-or-later
"""Executable review recovery contracts with deterministic local fault ports.

Native containment conformance is exercised separately. Here the production
worker, launch renderer, scheduler, verdict parser and revision policy consume
actual local process failures, without opening a model or credential session.
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, scenarios, then, when
from test_pow_wow_executor import _context, _review_loop_fixture, _review_loop_target, _seated

from local_first_agent_os import codex_review_launch as launch
from local_first_agent_os.capabilities import Capability
from local_first_agent_os.pow_wow import CliPowWowExecutor
from local_first_agent_os.pow_wow.process import extract_agent_cli_output
from local_first_agent_os.pow_wow.protocol import ReviewDisposition, ReviewVerdict
from local_first_agent_os.process_containment import ContainedProcess
from local_first_agent_os.sandbox_runtime import SandboxRuntimeInstallation
from local_first_agent_os.spawn_authority import SpawnAuthority

scenarios("features/reviewer_unavailable.feature")

_AUTHORITY = SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.INVOKE_MODEL))


@pytest.fixture
def world() -> dict[str, Any]:
    return {}


@given(parsers.parse('a local reviewer fixture fails at "{failure_point}"'))
def _failed_fixture(world, tmp_path, monkeypatch, failure_point):
    repo = tmp_path / "inspection"
    repo.mkdir()
    (repo / "canary.txt").write_text("repository read canary\n")
    source = tmp_path / "invalid-runtime"
    source.mkdir()
    (source / "package.json").write_text('{"version":"invalid-containment-canary"}')
    installation = SandboxRuntimeInstallation(source, Path(sys.executable), "fixture")
    service = tmp_path / "local_rpc_service.py"
    service.write_text(
        "import json, pathlib, sys\n"
        f"failure_point = {failure_point!r}\n"
        "if failure_point == 'startup':\n"
        "    print('startup-canary: transport refused', file=sys.stderr)\n"
        "    raise SystemExit(71)\n"
        "for raw in sys.stdin:\n"
        "    message = json.loads(raw)\n"
        "    method = message['method']\n"
        "    if method == 'initialized':\n"
        "        continue\n"
        "    if method == 'initialize':\n"
        "        result = {'sessionId': 'local-fault-canary'}\n"
        "    elif method == 'environment/info':\n"
        "        result = {'shell': {'name': 'sh', 'path': '/bin/sh'}}\n"
        "    elif method == 'environment/status':\n"
        "        result = {'status': 'ready'}\n"
        "    elif method == 'fs/getMetadata':\n"
        "        result = {'isDirectory': True, 'isFile': False, 'size': 0}\n"
        "    elif method == 'fs/readDirectory':\n"
        "        print(json.dumps({'id': message['id'], 'error':\n"
        "              {'code': -32003, 'message': 'tool-evidence-canary: read denied'}}),\n"
        "              flush=True)\n"
        "        continue\n"
        "    else:\n"
        "        raise AssertionError(method)\n"
        "    print(json.dumps({'id': message['id'], 'result': result}), flush=True)\n"
    )

    class LocalFaultBoundary:
        def __init__(self, _installation, repository):
            self.repository = repository

        @contextmanager
        def contain_service(self, command):
            assert Path(command[0]) == service
            assert "exec-server" in command
            yield ContainedProcess(
                command=(sys.executable, str(service)),
                environment={},
                scratch_path=tmp_path,
                posture="local_fault_fixture",
            )

    # Containment rejection exercises the real installed-runtime validator.
    # Transport faults replace only its external service with a local canary;
    # the production CodexToolWorker still owns the entire RPC/readiness path.
    if failure_point != "containment":
        monkeypatch.setattr(launch, "ReadOnlyToolWorker", LocalFaultBoundary)
    invocation = launch.CodexReviewInvocation(
        repository=repo,
        model="fixture-model",
        prompt="Review the assigned change.",
        authority=_AUTHORITY,
        installation=installation,
        codex_bin=service,
        auth_file=tmp_path / "credential-access-must-not-happen",
    )
    world.update(invocation=invocation, failure_point=failure_point)


def _preflight(world, repository, _binary, authority):
    invocation = world["invocation"]
    asyncio.run(
        launch.preflight_readonly_codex_runtime(
            installation=invocation.installation,
            repository=repository,
            codex_bin=invocation.codex_bin,
            authority=authority,
        )
    )


@when("the production executor checks reviewer readiness")
def _preflight_dispatch(world, tmp_path):
    repo, implementer, reviewer, tasks = _review_loop_fixture(tmp_path, codex_verdicts=["APPROVE"])
    target = _review_loop_target(repo)
    world["worktrees"] = tmp_path / "worktrees"
    world["run"] = CliPowWowExecutor(
        worktree_root=world["worktrees"],
        readonly_codex_preflight=lambda *args: _preflight(world, *args),
        **_seated(implementer=implementer, reviewer=reviewer),
    ).dispatch_pow_wow("bdd-preflight", target, tasks, _context(target))


@then("the run is blocked with REVIEW_UNAVAILABLE")
def _blocked(world):
    run = world["run"]
    assert run.status == "BLOCKED"
    assert run.tasks[0].failure.error_code == "REVIEW_UNAVAILABLE"
    assert run.tasks[0].failure.category.value == "INFRASTRUCTURE"


@then("no implementer or revision is started")
def _no_implementation(world):
    assert not world["run"].external_agents_started
    assert not world["worktrees"].exists()
    assert not any("revision_r" in task.task_name for task in world["run"].tasks)


@when("the prepared review runner encounters that failure")
def _render_failure(world, monkeypatch, capsys):
    # Sealed invocation parsing has independent tamper tests. The fault enters
    # after that admission; the production _run and main renderer are unchanged.
    monkeypatch.setattr(launch, "_read_invocation", lambda *_: world["invocation"])

    async def local_evidence_check(*, worker, **_):
        await worker.read_repository({"operation": "list_directory", "path": "."})
        pytest.fail("unavailable evidence must not reach a model connection")

    monkeypatch.setattr(launch, "run_read_only_review", local_evidence_check)
    world["exit_code"] = launch.main(["--request", "fixture", "--request-sha256", "fixture"])
    world["wire_report"] = capsys.readouterr().out
    world["review_text"] = extract_agent_cli_output(world["wire_report"])


@then("its report says CANNOT_REVIEW and parses as UNAVAILABLE")
def _unavailable_report(world):
    assert world["exit_code"] == 125
    assert json.loads(world["wire_report"])["failure_code"] == "REVIEW_UNAVAILABLE"
    assert world["review_text"].startswith("CANNOT_REVIEW:")
    assert ReviewVerdict.parse(world["review_text"]).disposition is ReviewDisposition.UNAVAILABLE
    expected = {
        "containment": "SRT",
        "startup": "worker disconnected",
        "tool-evidence": "tool-evidence-canary",
    }
    assert expected[world["failure_point"]] in world["review_text"]
    assert not world["invocation"].auth_file.exists()


@when("an implemented change receives that failed review report")
def _late_failure_dispatch(world, tmp_path):
    repo, implementer, reviewer, tasks = _review_loop_fixture(
        tmp_path, codex_verdicts=[world["review_text"]]
    )
    fixture = Path(reviewer)
    fixture.write_text(
        fixture.read_text().replace(
            "    emit(verdicts[min(n, len(verdicts) - 1)])\n",
            "    emit(verdicts[min(n, len(verdicts) - 1)])\n    raise SystemExit(125)\n",
        )
    )
    environment = {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "HOME": str(tmp_path)}

    class LocalFixtureContainer:
        @contextmanager
        def contain(self, command, cwd, **_):
            assert Path(command[0]).resolve().is_relative_to(tmp_path)
            yield ContainedProcess(tuple(command), environment, cwd, "local_fixture_only")

    @contextmanager
    def prepared(request):
        assert request.codex_bin.resolve().is_relative_to(tmp_path)
        yield ContainedProcess(
            (str(request.codex_bin), request.prompt),
            environment,
            request.repository,
            "local_fixture_only",
        )

    target = _review_loop_target(repo)
    world["run"] = CliPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        process_container=LocalFixtureContainer(),
        readonly_codex_launcher=prepared,
        readonly_codex_preflight=lambda *_: None,
        max_review_rounds=4,
        **_seated(implementer=implementer, reviewer=reviewer),
    ).dispatch_pow_wow("bdd-late-review-failure", target, tasks, _context(target))


@then("the review artifact is unavailable and never request_changes")
def _unavailable_artifact(world):
    run = world["run"]
    implementation = next(task for task in run.tasks if task.task_name == "implement_next_step")
    assert implementation.status == "completed", implementation.to_payload()
    review = next(task for task in run.tasks if task.task_name == "review_next_step")
    assert review.status == "failed"
    artifact = next(
        item.content for item in review.artifacts if item.artifact_type == "review_result"
    )
    assert artifact["verdict"] == "unavailable"
    assert artifact["completion_status"] == "FAILED"
    world["review"] = review


@then("the original host failure is retained without an implementation revision")
def _no_revision(world):
    assert world["run"].status != "COMPLETED"
    assert not any("revision_r" in task.task_name for task in world["run"].tasks)
    capture = next(
        artifact.content
        for artifact in world["review"].artifacts
        if artifact.schema_version == "cli_agent_run.v1"
    )
    assert capture["command"]["exit_code"] == 125
    assert world["review"].failure is not None
    assert world["review"].failure.error_code == "UNKNOWN_FAILURE"
    assert world["review_text"] in world["review"].failure.message
    assert world["run"].tasks[-1].failure == world["review"].failure
