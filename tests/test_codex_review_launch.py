# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from local_first_agent_os import codex_review_launch as launch
from local_first_agent_os.capabilities import Capability
from local_first_agent_os.codex_review_failure import inspection_process_failure
from local_first_agent_os.contracts import ArtifactRef
from local_first_agent_os.coordination.outcomes import TerminalOutcome
from local_first_agent_os.execution_admission import ExecutionAdmissionRefusal
from local_first_agent_os.pow_wow.process import extract_agent_cli_output
from local_first_agent_os.process_containment import ContainedProcess, ProcessContainmentUnavailable
from local_first_agent_os.sandbox_runtime import SandboxRuntimeInstallation
from local_first_agent_os.spawn_authority import SpawnAuthority


@pytest.fixture
def invocation(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = tmp_path / "codex"
    executable.write_text("fixture executable")
    executable.with_name("codex-code-mode-host").write_text("fixture interpreter")
    auth = tmp_path / "auth.json"
    auth.write_text('{"secret":"must-not-enter-launch-request"}')
    installation = SandboxRuntimeInstallation(tmp_path / "runtime", Path("/bin/sh"), "runtime-id")
    monkeypatch.setattr(launch, "_validate_codex", lambda path, scratch: launch._sha256(path))

    async def preflight(**kwargs):
        assert kwargs["repository"] == repo

    monkeypatch.setattr(launch, "preflight_readonly_codex_runtime", preflight)
    monkeypatch.setattr(
        SandboxRuntimeInstallation,
        "inspect",
        classmethod(lambda cls, source, node: installation),
    )
    return launch.CodexReviewInvocation(
        repository=repo,
        model="gpt-5.4",
        prompt="Review the assigned change.",
        authority=SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.INVOKE_MODEL)),
        installation=installation,
        codex_bin=executable,
        auth_file=auth,
        effort="high",
    )


def test_prepared_launch_is_typed_sealed_and_credential_free(invocation, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-secret")
    monkeypatch.setenv("LOCAL_AGENT_OPERATOR_TOKEN", "operator-secret")
    with launch.prepare_readonly_codex_launch(invocation) as contained:
        assert contained.command[1:3] == ("-m", "local_first_agent_os.codex_review_launch")
        request_path = Path(contained.command[4])
        digest = contained.command[6]
        encoded = request_path.read_bytes()
        assert digest == hashlib.sha256(encoded).hexdigest()
        assert b"must-not-enter-launch-request" not in encoded
        assert b"ambient-secret" not in encoded
        assert b"operator-secret" not in encoded
        assert "OPENAI_API_KEY" not in contained.environment
        assert "LOCAL_AGENT_OPERATOR_TOKEN" not in contained.environment
        assert contained.environment["PYTHONNOUSERSITE"] == "1"
        assert request_path.stat().st_mode & 0o777 == 0o600
        decoded = launch._read_invocation(request_path, digest)
        assert decoded == invocation
    assert not request_path.exists()


def test_interpreter_drift_invalidates_prepared_launch(invocation):
    with launch.prepare_readonly_codex_launch(invocation) as contained:
        invocation.codex_bin.with_name("codex-code-mode-host").write_text("changed interpreter")
        with pytest.raises(ProcessContainmentUnavailable, match="Code Mode executable changed"):
            launch._read_invocation(Path(contained.command[4]), contained.command[6])


@pytest.mark.parametrize(
    "capabilities",
    [
        (),
        (Capability.READ_REPOSITORY,),
        (Capability.READ_REPOSITORY, Capability.INVOKE_MODEL, Capability.RUN_COMMAND),
        (Capability.READ_REPOSITORY, Capability.INVOKE_MODEL, Capability.WRITE_REPOSITORY),
    ],
)
def test_invalid_invocation_authority_cannot_be_prepared(invocation, capabilities):
    with pytest.raises(ValueError, match="authority"):
        replace(invocation, authority=SpawnAuthority.of(capabilities))


def test_preflight_failure_never_yields_a_command(invocation, monkeypatch):
    async def unavailable(**kwargs):
        raise ProcessContainmentUnavailable("fixture worker disconnected")

    monkeypatch.setattr(launch, "preflight_readonly_codex_runtime", unavailable)
    with (
        pytest.raises(ProcessContainmentUnavailable, match="disconnected"),
        launch.prepare_readonly_codex_launch(invocation),
    ):
        pytest.fail("no command should escape failed preflight")


def test_request_tampering_rejected_before_runtime_inspection(invocation, monkeypatch):
    with launch.prepare_readonly_codex_launch(invocation) as contained:
        request_path = Path(contained.command[4])
        digest = contained.command[6]
        request_path.write_text("{}")
        monkeypatch.setattr(
            SandboxRuntimeInstallation, "inspect", lambda *_: pytest.fail("runtime touched")
        )
        with pytest.raises(ProcessContainmentUnavailable, match="request identity changed"):
            launch._read_invocation(request_path, digest)


def test_binary_drift_rejected(invocation):
    with launch.prepare_readonly_codex_launch(invocation) as contained:
        invocation.codex_bin.write_text("changed executable")
        with pytest.raises(ProcessContainmentUnavailable, match="Codex executable changed"):
            launch._read_invocation(Path(contained.command[4]), contained.command[6])


def test_runtime_drift_rejected(invocation, monkeypatch):
    with launch.prepare_readonly_codex_launch(invocation) as contained:
        monkeypatch.setattr(
            SandboxRuntimeInstallation,
            "inspect",
            classmethod(
                lambda cls, source, node: replace(
                    invocation.installation, identity_sha256="changed"
                )
            ),
        )
        with pytest.raises(ProcessContainmentUnavailable, match="sandbox installation changed"):
            launch._read_invocation(Path(contained.command[4]), contained.command[6])


def test_failed_runner_emits_unavailable_not_approval(invocation, monkeypatch, capsys):
    async def fail(_):
        raise ProcessContainmentUnavailable("fixture native worker closed")

    monkeypatch.setattr(launch, "_run", fail)
    with launch.prepare_readonly_codex_launch(invocation) as contained:
        status = launch.main(contained.command[3:])
    assert status == 125
    captured = capsys.readouterr().out
    assert json.loads(captured)["failure_code"] == "REVIEW_UNAVAILABLE"
    assert extract_agent_cli_output(captured).startswith("CANNOT_REVIEW:")
    assert "fixture native worker closed" in captured
    failure = inspection_process_failure(contained.command, stdout=captured, exit_code=status)
    assert failure is not None
    assert failure.error_code == TerminalOutcome.REVIEW_UNAVAILABLE


@pytest.mark.parametrize(
    ("error", "expected", "exit_code"),
    [
        (ValueError("invalid host contract"), TerminalOutcome.UNKNOWN_FAILURE, 125),
        (asyncio.CancelledError(), TerminalOutcome.OPERATOR_CANCELED, 130),
    ],
)
def test_host_bug_and_cancellation_are_not_reviewer_unavailability(
    invocation, monkeypatch, capsys, error, expected, exit_code
):
    async def fail(_):
        raise error

    monkeypatch.setattr(launch, "_run", fail)
    with launch.prepare_readonly_codex_launch(invocation) as contained:
        status = launch.main(contained.command[3:])
    assert status == exit_code
    failure = inspection_process_failure(
        contained.command, stdout=capsys.readouterr().out, exit_code=status
    )
    assert failure is not None
    assert failure.error_code == expected


def test_successful_runner_preserves_agent_jsonl_shape(invocation, monkeypatch, capsys):
    async def run(_):
        launch._emit(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "APPROVE\nFixture proof only."},
            }
        )
        launch._emit(
            {
                "type": "codex.app_server.turn.completed",
                "threadId": "fixture-thread",
                "turnId": "fixture-turn",
                "usage_notification": None,
            }
        )

    monkeypatch.setattr(launch, "_run", run)
    with launch.prepare_readonly_codex_launch(invocation) as contained:
        assert launch.main(contained.command[3:]) == 0
    assert extract_agent_cli_output(capsys.readouterr().out) == "APPROVE\nFixture proof only."


def test_no_model_preflight_exercises_native_read_without_auth(tmp_path, monkeypatch):
    calls = []

    class Worker:
        def __init__(self, boundary, codex, authority):
            calls.append((boundary.repository, codex, authority))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            calls.append("closed")

        async def require_ready(self):
            calls.append("ready")

        async def read_repository(self, request):
            calls.append(request)
            return {"entries": []}

    monkeypatch.setattr(launch, "CodexToolWorker", Worker)

    async def interpreter(boundary, codex):
        calls.append("interpreter-proven")

    monkeypatch.setattr(launch, "preflight_code_mode_runtime", interpreter)
    asyncio.run(
        launch.preflight_readonly_codex_runtime(
            installation=SandboxRuntimeInstallation(tmp_path, Path("/bin/sh"), "fixture"),
            repository=tmp_path,
            codex_bin=Path("/fixture/codex"),
            authority=SpawnAuthority.of((Capability.READ_REPOSITORY,)),
        )
    )
    assert "ready" in calls
    assert {"operation": "list_directory", "path": "."} in calls
    assert calls[-2:] == ["closed", "interpreter-proven"]


def test_configured_preflight_rejects_version_before_runtime_or_model(tmp_path, monkeypatch):
    executable = tmp_path / "codex"
    executable.write_text("fixture binary")
    calls = []

    def version(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="codex-cli 0.146.0\n")

    monkeypatch.setattr(launch.subprocess, "run", version)
    monkeypatch.setattr(
        launch, "_configured_installation", lambda: pytest.fail("runtime must not start")
    )
    monkeypatch.setattr(launch, "run_read_only_review", lambda **_: pytest.fail("no model call"))
    with pytest.raises(ProcessContainmentUnavailable, match="version differs"):
        launch.preflight_configured_codex_review(
            tmp_path,
            executable,
            SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.INVOKE_MODEL)),
        )
    assert calls == [(str(executable), "--version")]


@pytest.mark.parametrize("stage", ["ready", "read"])
def test_native_preflight_error_is_typed_unavailable(tmp_path, monkeypatch, stage):
    closed = []
    native_error = launch.NativeWorkerError({"code": -32600, "message": "Operation not permitted"})

    class Worker:
        def __init__(self, *_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            closed.append(True)

        async def require_ready(self):
            if stage == "ready":
                raise native_error

        async def read_repository(self, request):
            raise native_error

    monkeypatch.setattr(launch, "CodexToolWorker", Worker)
    with pytest.raises(ProcessContainmentUnavailable, match="preflight failed") as caught:
        asyncio.run(
            launch.preflight_readonly_codex_runtime(
                installation=SandboxRuntimeInstallation(tmp_path, Path("/bin/sh"), "fixture"),
                repository=tmp_path,
                codex_bin=Path("/fixture/codex"),
                authority=SpawnAuthority.of((Capability.READ_REPOSITORY,)),
            )
        )
    assert caught.value.__cause__ is native_error
    assert closed == [True]


def test_executor_inspection_uses_typed_launcher_not_legacy_container(invocation, tmp_path):
    from local_first_agent_os.pow_wow.executor import CliPowWowExecutor
    from local_first_agent_os.spawn_authority import ReadOnlyInspection

    calls = []

    @contextmanager
    def prepared(request):
        calls.append(request)
        yield ContainedProcess(
            (sys.executable, "-c", "print('fixture prepared path')"), {}, tmp_path, "fixture"
        )

    class LegacyContainer:
        def contain(self, *args, **kwargs):
            pytest.fail("readonly Codex must not enter legacy nested containment")

    executor = CliPowWowExecutor(
        worktree_root=tmp_path / "worktrees",
        process_container=LegacyContainer(),
        readonly_codex_launcher=prepared,
        readonly_codex_preflight=lambda *_: None,
    )
    request = launch.CodexInspectionRequest(
        invocation.repository,
        invocation.model,
        invocation.prompt,
        invocation.authority,
        invocation.codex_bin,
        invocation.effort,
    )
    captured, _ = executor._run_frontier_command(
        ("never-execute-this-raw-command",),
        invocation.repository,
        execution_attempt=None,
        harness="codex",
        env=None,
        source_repo_path=invocation.repository,
        base_head_sha=None,
        saga_id="fixture",
        pow_wow_id="fixture",
        task_contract=invocation.prompt,
        posture=ReadOnlyInspection(),
        inspection_request=request,
    )
    assert captured.exit_code == 0
    assert captured.stdout.strip() == "fixture prepared path"
    assert calls == [request]


def test_executor_rejects_raw_readonly_codex_route(invocation, tmp_path):
    from local_first_agent_os.pow_wow.executor import CliPowWowExecutor
    from local_first_agent_os.spawn_authority import ReadOnlyInspection

    executor = CliPowWowExecutor(worktree_root=tmp_path / "worktrees")
    captured, _ = executor._run_frontier_command(
        ("never-execute-this-raw-command",),
        invocation.repository,
        execution_attempt=None,
        harness="codex",
        env=None,
        source_repo_path=invocation.repository,
        base_head_sha=None,
        saga_id="fixture",
        pow_wow_id="fixture",
        task_contract=invocation.prompt,
        posture=ReadOnlyInspection(),
    )
    assert captured.exit_code == 125
    assert "typed inspection request" in captured.stderr


def test_wrong_driver_proof_cannot_reach_inspection_host_preparation(
    invocation: launch.CodexReviewInvocation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = launch.admit_execution(
        launch.ExecutionContract(launch.ExecutionDriver.PLANNED_DISPATCH), invocation.authority
    )
    assert isinstance(admission, launch.AuthorizedExecution)
    object.__setattr__(invocation, "authorization", admission)
    monkeypatch.setattr(launch, "_validate_codex", lambda *_: pytest.fail("host touched"))
    with (
        pytest.raises(ValueError, match="cannot authorize codex_inspection"),
        launch.prepare_readonly_codex_launch(invocation),
    ):
        pytest.fail("wrong-driver proof must not yield a command")


def test_wire_verify_authority_is_refused_before_runtime_identity_inspection(
    invocation: launch.CodexReviewInvocation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with launch.prepare_readonly_codex_launch(invocation) as contained:
        request_path = Path(contained.command[4])
        payload = json.loads(request_path.read_bytes())
        payload["capabilities"] = ["read_repository", "run_command"]
        encoded = json.dumps(payload).encode()
        request_path.write_bytes(encoded)
        monkeypatch.setattr(
            SandboxRuntimeInstallation, "inspect", lambda *_: pytest.fail("runtime touched")
        )
        with pytest.raises(launch.ExecutionAdmissionError) as caught:
            launch._read_invocation(request_path, hashlib.sha256(encoded).hexdigest())
        assert isinstance(caught.value.refusal, ExecutionAdmissionRefusal)
        assert caught.value.refusal.missing_capabilities == frozenset((Capability.INVOKE_MODEL,))
        assert caught.value.refusal.forbidden_capabilities == frozenset((Capability.RUN_COMMAND,))


def test_request_contract_refuses_original_m6_authority_before_any_host_call(
    invocation: launch.CodexReviewInvocation,
) -> None:
    with pytest.raises(launch.ExecutionAdmissionError):
        launch.CodexInspectionRequest(
            repository=invocation.repository,
            model=invocation.model,
            prompt=invocation.prompt,
            authority=SpawnAuthority.of((Capability.READ_REPOSITORY, Capability.RUN_COMMAND)),
            codex_bin=invocation.codex_bin,
        )


def test_final_encoded_request_bound_is_typed_and_never_yields(invocation):
    # The Unicode prompt itself fits; the exact JSON transport expands it.
    prompt = "\u263a" * 400_000
    assert len(prompt.encode()) < launch._MAX_REQUEST_BYTES
    with (
        pytest.raises(launch.CodexInspectionRequestTooLarge, match="exceeds the bound"),
        launch.prepare_readonly_codex_launch(replace(invocation, prompt=prompt)),
    ):
        pytest.fail("oversized encoded request yielded a provider command")


@pytest.mark.parametrize("owned", [False, True])
def test_request_bound_refusal_crosses_executor_without_agent_fault(tmp_path, monkeypatch, owned):
    from local_first_agent_os.coordination.outcomes import (
        AgentStatus,
        PersistenceStatus,
        SupervisorStatus,
    )
    from local_first_agent_os.pow_wow.executor import CliPowWowExecutor, _harness_failure
    from local_first_agent_os.pow_wow.types import ExecutionAttemptLease
    from local_first_agent_os.spawn_authority import ReadOnlyInspection

    @contextmanager
    def refused(*args, **kwargs):
        raise launch.CodexInspectionRequestTooLarge("encoded request exceeded bound")
        yield  # pragma: no cover - context-manager contract

    class ForbiddenArtifacts:
        def write_text(
            self,
            *,
            role: str,
            text: str,
            workflow_id: str | None,
            schema_version: str,
            mime_type: str = "text/plain",
        ) -> ArtifactRef:
            pytest.fail("prelaunch refusal wrote an execution artifact")

        def read_text(self, artifact_id: str) -> str:
            pytest.fail("prelaunch refusal read an execution artifact")

    executor = CliPowWowExecutor(worktree_root=tmp_path)
    attempt = None
    if owned:
        executor.coordination_command = lambda command: pytest.fail(
            "prelaunch refusal wrote a checkpoint"
        )
        executor.artifact_writer = ForbiddenArtifacts()
        attempt = ExecutionAttemptLease(
            idempotency_key="bound", worker_id="fixture", task_id="task", lease_id="lease"
        )
    monkeypatch.setattr(executor, "_prepared_frontier_process", refused)
    capture, result = executor._run_frontier_command(
        ("codex", "exec", "fixture prompt"),
        tmp_path,
        execution_attempt=attempt,
        harness="codex",
        env={},
        source_repo_path=tmp_path,
        base_head_sha="a" * 40,
        saga_id="saga",
        pow_wow_id="pow",
        task_contract="fixture",
        posture=ReadOnlyInspection(),
    )
    assert capture.exit_code == 125 and result is not None
    assert result.agent_status is AgentStatus.PENDING
    assert result.supervisor_status is SupervisorStatus.PENDING
    assert result.persistence_status is PersistenceStatus.PENDING
    assert result.checkpoint_id is None and result.event_count == 0
    assert not result.allows_task_completion
    failure = _harness_failure(
        capture, operation="run_code_review", supervised_result=result
    ).failure
    assert failure.error_code == failure.terminal_outcome == "REVIEW_UNAVAILABLE"
