# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed process entry point for the split Codex inspection environment.

The executor constructs this value before command rendering. An existing CLI
argv is never parsed into a weaker launch strategy. The prepared request holds
credential references, not credential bytes, and is sealed for the child.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .codex_code_mode import code_mode_executable, preflight_code_mode_runtime
from .codex_review_client import CodexSubscription, run_read_only_review
from .codex_review_failure import inspection_failure_event
from .codex_tool_worker import CodexToolWorker, NativeWorkerError
from .coordination.outcomes import TerminalOutcome
from .execution_admission import (
    AuthorizedExecution,
    ExecutionAdmissionError,
    ExecutionAdmissionRefusal,
    ExecutionContract,
    ExecutionDriver,
    admit_execution,
    require_authorized_execution,
)
from .process_containment import ContainedProcess, ProcessContainmentUnavailable
from .sandbox_runtime import ReadOnlyToolWorker, SandboxRuntimeInstallation
from .spawn_authority import SpawnAuthority

_REQUEST_VERSION = 2
_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_CODEX_VERSION = "codex-cli 0.153.4"
_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})


class CodexInspectionRequestTooLarge(ProcessContainmentUnavailable):
    """The complete encoded host request cannot fit the supported transport."""


@dataclass(frozen=True)
class CodexInspectionRequest:
    """Task intent before host runtime and subscription references are bound."""

    repository: Path
    model: str
    prompt: str
    authority: SpawnAuthority
    codex_bin: Path
    effort: str | None = None
    authorization: AuthorizedExecution = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "authorization", _inspection_authorization(self.authority))


def _inspection_authorization(authority: SpawnAuthority) -> AuthorizedExecution:
    admission = admit_execution(ExecutionContract(ExecutionDriver.CODEX_INSPECTION), authority)
    if isinstance(admission, ExecutionAdmissionRefusal):
        raise ExecutionAdmissionError(admission)
    return admission


def _configured_installation() -> SandboxRuntimeInstallation:
    home = Path.home()
    source = Path(
        os.environ.get(
            "LOCAL_AGENT_SANDBOX_RUNTIME_SOURCE",
            str(home / ".local-agent/vendor/sandbox-runtime-0.0.75/source"),
        )
    )
    node = Path(
        os.environ.get(
            "LOCAL_AGENT_SANDBOX_RUNTIME_NODE",
            str(home / ".nvm/versions/node/v22.19.0/bin/node"),
        )
    )
    return SandboxRuntimeInstallation.inspect(source, node)


def preflight_configured_codex_review(
    repository: Path,
    codex_bin: Path,
    authority: SpawnAuthority,
) -> None:
    _inspection_authorization(authority)
    with tempfile.TemporaryDirectory(prefix="aidashos-review-preflight-") as raw:
        _validate_codex(codex_bin, Path(raw).resolve())
        asyncio.run(
            preflight_readonly_codex_runtime(
                installation=_configured_installation(),
                repository=repository,
                codex_bin=codex_bin,
                authority=authority,
            )
        )


@contextmanager
def prepare_configured_codex_review(request: CodexInspectionRequest) -> Iterator[ContainedProcess]:
    authorization = require_authorized_execution(
        request.authorization, ExecutionDriver.CODEX_INSPECTION
    )
    if authorization.authority != request.authority:
        raise ValueError("inspection request authority differs from its admission")
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    invocation = CodexReviewInvocation(
        repository=request.repository,
        model=request.model,
        prompt=request.prompt,
        authority=request.authority,
        codex_bin=request.codex_bin,
        installation=_configured_installation(),
        auth_file=codex_home / "auth.json",
        effort=request.effort,
    )
    with prepare_readonly_codex_launch(invocation) as contained:
        yield contained


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@dataclass(frozen=True)
class CodexReviewInvocation:
    repository: Path
    model: str
    prompt: str
    authority: SpawnAuthority
    installation: SandboxRuntimeInstallation
    codex_bin: Path
    auth_file: Path
    effort: str | None = None
    authorization: AuthorizedExecution = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "authorization", _inspection_authorization(self.authority))
        if not self.model.strip() or not self.prompt.strip():
            raise ValueError("Codex inspection requires an explicit model and prompt")
        if self.effort is not None and self.effort not in _EFFORTS:
            raise ValueError("unsupported Codex reasoning effort")
        if not self.repository.is_absolute() or not self.codex_bin.is_absolute():
            raise ValueError("Codex inspection requires absolute host-owned paths")
        if not self.auth_file.is_absolute():
            raise ValueError("Codex subscription reference must be absolute")


def _driver_environment(scratch: Path) -> dict[str, str]:
    return {
        "HOME": str(scratch),
        "TMPDIR": str(scratch),
        "TMP": str(scratch),
        "TEMP": str(scratch),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "SHELL": "/bin/sh",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "UV_OFFLINE": "1",
        "UV_CACHE_DIR": str(scratch / "uv-cache"),
    }


def _validate_codex(codex_bin: Path, scratch: Path) -> str:
    executable = codex_bin.resolve(strict=True)
    result = subprocess.run(
        (str(codex_bin), "--version"),
        env=_driver_environment(scratch),
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    if result.stdout.strip() != _CODEX_VERSION:
        raise ProcessContainmentUnavailable(
            "Codex version differs from the verified worker protocol"
        )
    return _sha256(executable)


async def preflight_readonly_codex_runtime(
    *,
    installation: SandboxRuntimeInstallation,
    repository: Path,
    codex_bin: Path,
    authority: SpawnAuthority,
) -> None:
    """No model client, credential file, ledger, or paid invocation is opened."""
    admission = admit_execution(ExecutionContract(ExecutionDriver.CODEX_TOOL_PREFLIGHT), authority)
    if isinstance(admission, ExecutionAdmissionRefusal):
        raise ExecutionAdmissionError(admission)
    require_authorized_execution(admission, ExecutionDriver.CODEX_TOOL_PREFLIGHT)
    boundary = ReadOnlyToolWorker(installation, repository)
    try:
        async with CodexToolWorker(boundary, str(codex_bin), authority) as worker:
            await worker.require_ready()
            result = await worker.read_repository({"operation": "list_directory", "path": "."})
            if not isinstance(result.get("entries"), list):
                raise ProcessContainmentUnavailable(
                    "native repository read preflight was not usable"
                )
        await preflight_code_mode_runtime(boundary, str(codex_bin))
    except NativeWorkerError as error:
        raise ProcessContainmentUnavailable("native repository read preflight failed") from error


@contextmanager
def prepare_readonly_codex_launch(invocation: CodexReviewInvocation) -> Iterator[ContainedProcess]:
    authorization = require_authorized_execution(
        invocation.authorization, ExecutionDriver.CODEX_INSPECTION
    )
    if authorization.authority != invocation.authority:
        raise ValueError("inspection invocation authority differs from its admission")
    repository = invocation.repository.resolve(strict=True)
    if not repository.is_dir():
        raise ValueError("inspection repository is not a directory")
    # Resolve metadata only. Authentication contents remain owned by the trusted
    # model connection and are never copied into the execution request.
    auth_file = invocation.auth_file.resolve(strict=True)
    if not auth_file.is_file():
        raise ProcessContainmentUnavailable("Codex subscription reference is not a file")
    with tempfile.TemporaryDirectory(prefix="aidashos-review-launch-") as raw:
        scratch = Path(raw).resolve()
        codex_digest = _validate_codex(invocation.codex_bin, scratch)
        asyncio.run(
            preflight_readonly_codex_runtime(
                installation=invocation.installation,
                repository=repository,
                codex_bin=invocation.codex_bin,
                authority=invocation.authority,
            )
        )
        request = {
            "version": _REQUEST_VERSION,
            "repository": str(repository),
            "model": invocation.model,
            "prompt": invocation.prompt,
            "effort": invocation.effort,
            "capabilities": sorted(
                capability.value for capability in invocation.authority.capabilities
            ),
            "runtime": {
                "source": str(invocation.installation.source),
                "node": str(invocation.installation.node),
                "identity_sha256": invocation.installation.identity_sha256,
            },
            "codex_bin": str(invocation.codex_bin),
            "codex_sha256": codex_digest,
            "code_mode_sha256": _sha256(code_mode_executable(invocation.codex_bin)),
            "auth_file": str(auth_file),
        }
        encoded = json.dumps(request).encode()
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise CodexInspectionRequestTooLarge("Codex inspection request exceeds the bound")
        request_path = scratch / "request.json"
        request_path.write_bytes(encoded)
        request_path.chmod(0o600)
        yield ContainedProcess(
            command=(
                sys.executable,
                "-m",
                "local_first_agent_os.codex_review_launch",
                "--request",
                str(request_path),
                "--request-sha256",
                hashlib.sha256(encoded).hexdigest(),
            ),
            environment=_driver_environment(scratch),
            scratch_path=scratch,
            posture="codex_read_only_inspection",
        )


def _read_invocation(request_path: Path, request_sha256: str) -> CodexReviewInvocation:
    with request_path.open("rb") as stream:
        encoded = stream.read(_MAX_REQUEST_BYTES + 1)
    if len(encoded) > _MAX_REQUEST_BYTES or hashlib.sha256(encoded).hexdigest() != request_sha256:
        raise ProcessContainmentUnavailable("prepared inspection request identity changed")
    request = json.loads(encoded)
    keys = {
        "version",
        "repository",
        "model",
        "prompt",
        "effort",
        "capabilities",
        "runtime",
        "codex_bin",
        "codex_sha256",
        "code_mode_sha256",
        "auth_file",
    }
    if (
        not isinstance(request, dict)
        or set(request) != keys
        or request["version"] != _REQUEST_VERSION
    ):
        raise ValueError("unsupported inspection launch request")
    capability_names = request["capabilities"]
    if not isinstance(capability_names, list) or any(
        not isinstance(name, str) for name in capability_names
    ):
        raise TypeError("inspection capabilities must be a list of registered names")
    authority = SpawnAuthority.from_names(capability_names)
    _inspection_authorization(authority)
    runtime = request["runtime"]
    if not isinstance(runtime, dict) or set(runtime) != {"source", "node", "identity_sha256"}:
        raise ValueError("invalid runtime identity")
    installation = SandboxRuntimeInstallation.inspect(
        Path(runtime["source"]), Path(runtime["node"])
    )
    if installation.identity_sha256 != runtime["identity_sha256"]:
        raise ProcessContainmentUnavailable("prepared sandbox installation changed")
    codex_bin = Path(request["codex_bin"])
    if _sha256(codex_bin.resolve(strict=True)) != request["codex_sha256"]:
        raise ProcessContainmentUnavailable("prepared Codex executable changed")
    if _sha256(code_mode_executable(codex_bin)) != request["code_mode_sha256"]:
        raise ProcessContainmentUnavailable("prepared Code Mode executable changed")
    return CodexReviewInvocation(
        repository=Path(request["repository"]),
        model=request["model"],
        prompt=request["prompt"],
        authority=authority,
        installation=installation,
        codex_bin=codex_bin,
        auth_file=Path(request["auth_file"]),
        effort=request["effort"],
    )


def _emit(value: Mapping[str, Any]) -> None:
    print(json.dumps(value), flush=True)


async def _run(invocation: CodexReviewInvocation) -> None:
    task = asyncio.current_task()
    assert task is not None
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        boundary = ReadOnlyToolWorker(invocation.installation, invocation.repository)
        async with CodexToolWorker(
            boundary, str(invocation.codex_bin), invocation.authority
        ) as worker:
            await run_read_only_review(
                worker=worker,
                codex_bin=str(invocation.codex_bin),
                repository=invocation.repository,
                model=CodexSubscription(invocation.model, invocation.auth_file),
                prompt=invocation.prompt,
                effort=invocation.effort,
                emit=_emit,
            )
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a prepared AiDashOS Codex inspection")
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--request-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        invocation = _read_invocation(args.request, args.request_sha256)
        asyncio.run(_run(invocation))
        return 0
    except asyncio.CancelledError:
        _emit(
            inspection_failure_event(
                TerminalOutcome.OPERATOR_CANCELED, "CANNOT_REVIEW: inspection was cancelled"
            )
        )
        return 130
    except (ProcessContainmentUnavailable, NativeWorkerError) as exc:
        _emit(
            inspection_failure_event(
                TerminalOutcome.REVIEW_UNAVAILABLE, f"CANNOT_REVIEW: {type(exc).__name__}: {exc}"
            )
        )
        return 125
    except Exception as exc:  # noqa: BLE001 - process contract reports every failed launch
        _emit(
            inspection_failure_event(
                TerminalOutcome.UNKNOWN_FAILURE, f"CANNOT_REVIEW: {type(exc).__name__}: {exc}"
            )
        )
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
