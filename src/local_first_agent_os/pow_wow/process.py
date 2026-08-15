# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""External process capture and execution-lease conversion operations."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import signal
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from ..project_center import LinkedProject
from ..toolchains import project_environment
from .types import (
    CommandRunCapture,
    DispatchKind,
    ExecutionLeaseStatus,
    PowWowTaskSpec,
)

type FrontierFallbackReason = Literal["timeout", "usage_limit", "authentication_failed"]


def _decode_timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _describe_command_timeout(value: str | bytes | None, timeout_seconds: int) -> str:
    return f"{_decode_timeout_output(value)}Command timed out after {timeout_seconds}s"


def _run_reaped_process_group(
    command: str | list[str],
    cwd: Path,
    *,
    shell: bool,
    environment: Mapping[str, str],
    timeout_seconds: int,
    display_command: str,
) -> CommandRunCapture:
    """Run the command as its own process group; on timeout, reap the whole group.

    `subprocess.run` kills only its direct child on timeout. With `shell=True`
    that child is the shell, and with `shell=False` it is a launcher like `uv`,
    so the process doing the work survives the kill as an orphan. An orphan
    holding the output pipe blocks the post-kill drain until it exits on its own
    schedule, which means a command whose clock has fired can hold the executor
    for as long as the orphan pleases. A suite whose lingering threads keep the
    interpreter alive after the summary prints is the recorded case: see the
    incident in docs/completed/verification_gate_environment_design.md. The group kill is
    what makes the timeout an actual bound rather than a request.
    """

    try:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            shell=shell,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return CommandRunCapture(
            command=display_command,
            cwd=str(cwd),
            stdout="",
            stderr=str(exc),
            exit_code=127,
        )
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # Something outside the group still holds the pipes; return with
            # what the timeout carried rather than waiting on a stranger.
            proc.kill()
            stdout = _decode_timeout_output(exc.stdout)
            stderr = _decode_timeout_output(exc.stderr)
        return CommandRunCapture(
            command=display_command,
            cwd=str(cwd),
            stdout=stdout,
            stderr=_describe_command_timeout(stderr, timeout_seconds),
            exit_code=124,
        )
    return CommandRunCapture(
        command=display_command,
        cwd=str(cwd),
        stdout=stdout,
        stderr=stderr,
        exit_code=proc.returncode,
    )


def run_captured_command(
    command: Sequence[str],
    cwd: Path,
    *,
    timeout_seconds: int,
    env: Mapping[str, str] | None = None,
) -> CommandRunCapture:
    return _run_reaped_process_group(
        [str(part) for part in command],
        cwd,
        shell=False,
        environment=project_environment(cwd, env),
        timeout_seconds=timeout_seconds,
        display_command=shlex.join(str(part) for part in command),
    )


def run_captured_shell_command(
    command: str,
    cwd: Path,
    *,
    timeout_seconds: int,
    environment: Mapping[str, str] | None = None,
) -> CommandRunCapture:
    return _run_reaped_process_group(
        command,
        cwd,
        shell=True,
        environment=environment if environment is not None else project_environment(cwd),
        timeout_seconds=timeout_seconds,
        display_command=command,
    )


def extract_agent_cli_output(stdout: str) -> str:
    """Pull the agent's response text out of a headless CLI's stdout.

    Both frontier CLIs run in JSONL mode under the streaming supervisor. Older
    single-JSON and plain-text captures remain supported for stored leases and
    tests.

    It took a ``harness`` argument that the body never read, which made this look
    like a per-harness dispatch point while being a union of every shape either
    CLI emits. A parameter that names a decision nobody makes is worse than no
    parameter: the next reader trusts it. If PI output ever needs its own
    parsing, that is where a real ``FrontierHarness`` argument comes back.
    """
    candidates: list[str] = []
    for line in stdout.splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(line)
            if not isinstance(data, dict):
                continue
            result = data.get("result") or data.get("text")
            if isinstance(result, str) and result.strip():
                candidates.append(result.strip())
            item = data.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    candidates.append(text.strip())
            message = data.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str) and text.strip():
                            candidates.append(text.strip())
    if candidates:
        return candidates[-1]
    return stdout.strip()


_USAGE_LIMIT_PATTERNS = (
    "usage limit",
    "session limit",
    "hit your session limit",
    "rate limit",
    "quota exceeded",
    "too many requests",
    "429",
    "limit reached",
    "exceeded your current quota",
)

# Deliberately narrower than `classify_failure`'s authentication markers, which
# include a bare "unauthorized". That word is fine for labelling a failure and
# wrong for triggering one of these: an agent working on authorization code
# prints it in ordinary output, and the cost of a false match here is a spurious
# provider swap rather than a mislabelled row. Every pattern below names the CLI
# failing to authenticate itself.
_AUTHENTICATION_FAILED_PATTERNS = (
    "authentication invalid",
    "authentication expired",
    "not authenticated",
    "invalid api key",
    "invalid bearer token",
    "oauth token expired",
    "401 unauthorized",
    "http 401",
    "please run `claude auth login`",
    "please run `codex login`",
)

# Reasons whose remedy is the other provider rather than another attempt at this
# one. A timeout is absent on purpose: it says this run was too slow, not that
# this provider cannot serve, and the fallback still runs for it while the
# ledger records no provider-replacement policy.
_PROVIDER_SWAP_REASONS = frozenset({"usage_limit", "authentication_failed"})


def warrants_provider_swap(reason: FrontierFallbackReason | None) -> bool:
    """Whether this failure is the other provider's problem to solve.

    One place decides, because the ledger records the decision twice - as a
    `next_action` and as a `replacement_policy` - and two copies of the same
    condition are two places for it to drift.
    """

    return reason in _PROVIDER_SWAP_REASONS


def infer_frontier_fallback_reason(capture: CommandRunCapture) -> FrontierFallbackReason | None:
    if capture.exit_code == 124:
        return "timeout"
    combined = f"{capture.stdout}\n{capture.stderr}".lower()
    if any(pattern in combined for pattern in _USAGE_LIMIT_PATTERNS):
        return "usage_limit"
    # A credential that died mid-run is the same shape of problem as a quota that
    # ran out: this provider cannot continue, the other one can, and the work is
    # unchanged. It was absent here for a while, so the one failure the start-time
    # probe is best at catching was also the one the runtime could not route
    # around when it happened after the probe had already passed.
    if any(pattern in combined for pattern in _AUTHENTICATION_FAILED_PATTERNS):
        return "authentication_failed"
    return None


def _truncate_text_to_tail(value: str, *, max_chars: int = 4000) -> str:
    if len(value) <= max_chars:
        return value
    return value[-max_chars:]


def build_command_capture_lease_payload(capture: CommandRunCapture) -> dict[str, Any]:
    return {
        "command": capture.command,
        "cwd": capture.cwd,
        "exit_code": capture.exit_code,
        "stdout_tail": _truncate_text_to_tail(capture.stdout),
        "stderr_tail": _truncate_text_to_tail(capture.stderr),
        "stdout_truncated": len(capture.stdout) > 4000,
        "stderr_truncated": len(capture.stderr) > 4000,
    }


def build_command_capture_from_lease_result(
    result: Mapping[str, Any] | None,
    *,
    fallback_command: Sequence[str],
    cwd: Path,
    status: str | None,
) -> CommandRunCapture:
    payload = result.get("command_capture") if result else None
    if isinstance(payload, Mapping):
        return CommandRunCapture(
            command=str(payload.get("command") or shlex.join(str(p) for p in fallback_command)),
            cwd=str(payload.get("cwd") or cwd),
            stdout=str(payload.get("stdout") or payload.get("stdout_tail") or ""),
            stderr=str(payload.get("stderr") or payload.get("stderr_tail") or ""),
            exit_code=int(payload.get("exit_code") or 0),
        )
    exit_code = 0 if status == "COMPLETED" else 124 if status == "TIMED_OUT" else 1
    return CommandRunCapture(
        command=shlex.join(str(part) for part in fallback_command),
        cwd=str(cwd),
        stdout="",
        stderr=f"Reused terminal execution lease with status={status}",
        exit_code=exit_code,
    )


def classify_execution_lease_status(capture: CommandRunCapture) -> ExecutionLeaseStatus:
    fallback_reason = infer_frontier_fallback_reason(capture)
    if fallback_reason == "timeout":
        return "TIMED_OUT"
    if capture.exit_code == 130:
        combined = f"{capture.stdout}\n{capture.stderr}".lower()
        if "cancel" in combined:
            return "CANCELED"
    return "COMPLETED" if capture.exit_code == 0 else "FAILED"


def describe_execution_lease_error(
    capture: CommandRunCapture,
    *,
    fallback_reason: FrontierFallbackReason | None,
    status: ExecutionLeaseStatus,
    timeout_seconds: int,
) -> str | None:
    if status == "COMPLETED":
        return None
    if status == "TIMED_OUT":
        return f"process timed out after {timeout_seconds}s"
    if status == "CANCELED":
        return "process canceled"
    if fallback_reason == "usage_limit":
        return "usage_limit"
    if capture.stderr.strip():
        return _truncate_text_to_tail(capture.stderr.strip())
    return f"process exited {capture.exit_code}"


def build_execution_attempt_idempotency_key(
    *,
    pow_wow_id: str,
    target_project: LinkedProject,
    task: PowWowTaskSpec,
    harness: str,
    model: str | None,
    dispatch_kind: DispatchKind,
) -> str:
    payload = {
        "pow_wow_id": pow_wow_id,
        "target_project_id": target_project.id,
        "task_name": task.task_name,
        "role": task.role,
        "harness": harness,
        "model": model,
        "dispatch_kind": dispatch_kind,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return f"cli_attempt:{digest}"
