# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Execution adapters for direct CLI queries and bounded local-model work.

This module does not dispatch WorkUnit or pow-wow tasks. The dispatcher claims
durable intents, ``dispatcher_runner`` materializes their records, and the
pow-wow executor schedules the resulting task DAG. These adapters own only the
last execution hop after a caller has already selected one runtime.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, assert_never

from .constants import (
    DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
    DEFAULT_AGENT_PROCESS_HEALTH_TIMEOUT_SECONDS,
    DEFAULT_AGENT_PROCESS_TERMINATION_GRACE_SECONDS,
)
from .contracts import AgentHarness, ArtifactRef, ArtifactRole, ModelCallRequest, ModelRole
from .coordination.failures import DurableFailureError, FailureV1

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .runtime import AppRuntime

CLAUDE_CLI_EXECUTABLE = "claude"
CODEX_CLI_EXECUTABLE = "codex"


class AgentRuntimeKind(StrEnum):
    """The execution runtime that produced an adapter result."""

    CLAUDE_CODE = "claude_code"
    CODEX_CLI = "codex_cli"
    LOCAL_MODEL = "local_model"


class CodexSandbox(StrEnum):
    """Documented Codex sandbox modes allowed for direct query execution."""

    READ_ONLY = "read-only"


@dataclass(frozen=True)
class AgentTask:
    """The fields every direct CLI execution consumes."""

    task_id: str
    prompt: str
    timeout_seconds: int | float = DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("agent task_id is required")
        if not self.prompt.strip():
            raise ValueError("agent prompt is required")
        if self.timeout_seconds <= 0:
            raise ValueError("agent timeout_seconds must be positive")


@dataclass(frozen=True)
class LocalModelTask:
    """A local-model task with no CLI-only or unconsumed policy fields."""

    task_id: str
    prompt: str
    model_role: ModelRole
    task_max_tokens: int
    timeout_seconds: int = DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS
    workflow_id: str | None = None
    model_params: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("local-model task_id is required")
        if not self.prompt.strip():
            raise ValueError("local-model prompt is required")
        if self.task_max_tokens <= 0:
            raise ValueError("local-model task_max_tokens must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("local-model timeout_seconds must be positive")
        if "max_tokens" in self.model_params:
            raise ValueError("use task_max_tokens, not model_params['max_tokens']")
        object.__setattr__(self, "model_params", MappingProxyType(dict(self.model_params)))


@dataclass(frozen=True)
class CliRunProvenance:
    runtime: AgentRuntimeKind
    model: str
    session_id: str | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "runtime": self.runtime.value,
            "model": self.model,
            "session_id": self.session_id,
        }


@dataclass(frozen=True)
class LocalModelRunProvenance:
    model_role: ModelRole
    workflow_id: str
    model_id: str | None = None
    invocation_id: str | None = None
    prompt_artifact: ArtifactRef | None = None
    output_artifact: ArtifactRef | None = None

    @property
    def runtime(self) -> AgentRuntimeKind:
        return AgentRuntimeKind.LOCAL_MODEL

    def to_payload(self) -> dict[str, object]:
        return {
            "runtime": self.runtime.value,
            "model_role": self.model_role.value,
            "workflow_id": self.workflow_id,
            "model_id": self.model_id,
            "invocation_id": self.invocation_id,
            "prompt_artifact_id": (
                self.prompt_artifact.artifact_id if self.prompt_artifact is not None else None
            ),
            "output_artifact_id": (
                self.output_artifact.artifact_id if self.output_artifact is not None else None
            ),
        }


type AgentRunProvenance = CliRunProvenance | LocalModelRunProvenance


@dataclass(frozen=True)
class AgentResult:
    """Normalized adapter output with typed execution provenance."""

    task_id: str
    success: bool
    output: str
    provenance: AgentRunProvenance
    error: str | FailureV1 | None = None
    tokens_used: int = 0

    def __post_init__(self) -> None:
        if self.success == (self.error is not None):
            raise ValueError("a successful agent result has no error; a failure must name one")
        if self.tokens_used < 0:
            raise ValueError("agent tokens_used cannot be negative")

    @property
    def error_text(self) -> str | None:
        """Project human guidance without discarding an owned failure's typed cause."""

        if isinstance(self.error, FailureV1):
            return self.error.message or self.error.error_code
        return self.error


@dataclass(frozen=True)
class _CompletedProcess:
    stdout: bytes
    stderr: bytes
    returncode: int


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Bound shutdown of an adapter child after timeout or cancellation."""

    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(
            process.wait(), timeout=DEFAULT_AGENT_PROCESS_TERMINATION_GRACE_SECONDS
        )
        return
    except TimeoutError:
        process.kill()
    try:
        await asyncio.wait_for(
            process.wait(), timeout=DEFAULT_AGENT_PROCESS_TERMINATION_GRACE_SECONDS
        )
    except TimeoutError:
        logger.error("agent_adapter_process_did_not_exit_after_kill pid=%s", process.pid)


async def _run_cli_process(
    command: tuple[str, ...],
    *,
    cwd: Path | None,
    timeout_seconds: int | float,
    environment: Mapping[str, str] | None = None,
) -> _CompletedProcess:
    """Run one already-built CLI command and bound its entire process lifetime."""

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=dict(environment) if environment is not None else None,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except BaseException:
        await _terminate_process(process)
        raise
    if process.returncode is None:
        raise RuntimeError("agent process communicate completed without a return code")
    return _CompletedProcess(stdout=stdout, stderr=stderr, returncode=process.returncode)


async def _cli_is_healthy(executable: str) -> bool:
    """Use the same bounded version probe for every CLI adapter."""

    try:
        result = await _run_cli_process(
            (executable, "--version"),
            cwd=None,
            timeout_seconds=DEFAULT_AGENT_PROCESS_HEALTH_TIMEOUT_SECONDS,
        )
    except (OSError, TimeoutError):
        return False
    return result.returncode == 0


def _required_model(model: str) -> str:
    resolved = model.strip()
    if not resolved:
        raise ValueError("an agent CLI model must be specified explicitly")
    return resolved


class ClaudeCodeAdapter:
    """Run one explicitly selected Claude model as a non-interactive query."""

    def __init__(self, *, model: str, cwd: str | Path | None = None) -> None:
        self.model = _required_model(model)
        self.cwd = Path(cwd) if cwd is not None else None

    def command_for(self, task: AgentTask) -> tuple[str, ...]:
        return (
            CLAUDE_CLI_EXECUTABLE,
            "--print",
            "--output-format",
            "json",
            "--model",
            self.model,
            task.prompt,
        )

    async def run(self, task: AgentTask) -> AgentResult:
        provenance = CliRunProvenance(AgentRuntimeKind.CLAUDE_CODE, self.model)
        try:
            completed = await _run_cli_process(
                self.command_for(task),
                cwd=self.cwd,
                timeout_seconds=task.timeout_seconds,
                environment={**os.environ},
            )
        except TimeoutError:
            return AgentResult(
                task_id=task.task_id,
                success=False,
                output="",
                error=f"Timeout after {task.timeout_seconds}s",
                provenance=provenance,
            )
        except Exception as exc:
            return AgentResult(
                task_id=task.task_id,
                success=False,
                output="",
                error=str(exc),
                provenance=provenance,
            )

        output_text = completed.stdout.decode("utf-8", errors="replace")
        session_id: str | None = None
        result_text = output_text
        tokens = 0
        try:
            data = json.loads(output_text)
            if isinstance(data, dict):
                result_text = str(data.get("result", output_text))
                usage = data.get("usage")
                if isinstance(usage, dict):
                    tokens = int(usage.get("output_tokens") or 0)
                raw_session_id = data.get("session_id")
                session_id = str(raw_session_id) if raw_session_id else None
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        provenance = CliRunProvenance(AgentRuntimeKind.CLAUDE_CODE, self.model, session_id)
        success = completed.returncode == 0
        return AgentResult(
            task_id=task.task_id,
            success=success,
            output=result_text,
            error=(
                completed.stderr.decode("utf-8", errors="replace") or "Claude CLI failed"
                if not success
                else None
            ),
            tokens_used=tokens,
            provenance=provenance,
        )

    async def health_check(self) -> bool:
        return await _cli_is_healthy(CLAUDE_CLI_EXECUTABLE)


def _codex_jsonl_result(stdout: bytes) -> tuple[str, int, str | None]:
    """Extract the final message, output usage, and session id from Codex JSONL."""

    output_parts: list[str] = []
    output_tokens = 0
    session_id: str | None = None
    for raw_line in stdout.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started" and event.get("thread_id"):
            session_id = str(event["thread_id"])
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            output_parts.append(item["text"])
        usage = event.get("usage")
        if isinstance(usage, dict):
            output_tokens = max(output_tokens, int(usage.get("output_tokens") or 0))
    fallback = stdout.decode("utf-8", errors="replace")
    return "\n".join(output_parts) or fallback, output_tokens, session_id


class CodexCLIAdapter:
    """Run one explicitly selected Codex model with the documented exec contract."""

    def __init__(self, *, model: str, cwd: str | Path | None = None) -> None:
        self.model = _required_model(model)
        self.cwd = Path(cwd) if cwd is not None else None

    def command_for(self, task: AgentTask) -> tuple[str, ...]:
        return (
            CODEX_CLI_EXECUTABLE,
            "exec",
            "--json",
            "--model",
            self.model,
            "--sandbox",
            CodexSandbox.READ_ONLY.value,
            task.prompt,
        )

    async def run(self, task: AgentTask) -> AgentResult:
        provenance = CliRunProvenance(AgentRuntimeKind.CODEX_CLI, self.model)
        try:
            completed = await _run_cli_process(
                self.command_for(task),
                cwd=self.cwd,
                timeout_seconds=task.timeout_seconds,
            )
        except TimeoutError:
            return AgentResult(
                task_id=task.task_id,
                success=False,
                output="",
                error=f"Timeout after {task.timeout_seconds}s",
                provenance=provenance,
            )
        except Exception as exc:
            return AgentResult(
                task_id=task.task_id,
                success=False,
                output="",
                error=str(exc),
                provenance=provenance,
            )

        output, tokens, session_id = _codex_jsonl_result(completed.stdout)
        provenance = CliRunProvenance(AgentRuntimeKind.CODEX_CLI, self.model, session_id)
        success = completed.returncode == 0
        return AgentResult(
            task_id=task.task_id,
            success=success,
            output=output,
            error=(
                completed.stderr.decode("utf-8", errors="replace") or "Codex CLI failed"
                if not success
                else None
            ),
            tokens_used=tokens,
            provenance=provenance,
        )

    async def health_check(self) -> bool:
        return await _cli_is_healthy(CODEX_CLI_EXECUTABLE)


def _model_output_text(payload: object) -> str:
    """Render the documented model-output JSON shapes and reject arbitrary objects."""

    if isinstance(payload, str):
        return payload
    if isinstance(payload, Mapping):
        output = payload.get("output", payload)
        if isinstance(output, str):
            return output
        if isinstance(output, Mapping):
            text = output.get("text")
            if isinstance(text, str):
                return text
            return json.dumps(dict(output), ensure_ascii=False, sort_keys=True)
        if output is None or isinstance(output, (bool, int, float, list)):
            return json.dumps(output, ensure_ascii=False, sort_keys=True)
    raise TypeError(f"model output must be JSON data, got {type(payload).__name__}")


class LocalModelAdapter:
    """Run a task through ModelManager's selected local-inference backend."""

    def __init__(self, runtime: AppRuntime) -> None:
        self.runtime = runtime

    async def run(self, task: LocalModelTask) -> AgentResult:
        workflow_id = task.workflow_id or f"agent-task-{task.task_id}"
        requested = LocalModelRunProvenance(task.model_role, workflow_id)
        try:
            if task.workflow_id and not self.runtime.repository.workflow_run_exists(workflow_id):
                raise ValueError(f"Local delegate workflow_id {workflow_id!r} is not registered")
            prompt_artifact = self.runtime.artifact_store.write_text(
                role=ArtifactRole.PROMPT.value,
                text=task.prompt,
                workflow_id=workflow_id,
                schema_version="agent_task_prompt.v1",
            )
            requested = replace(requested, prompt_artifact=prompt_artifact)
            model_params = dict(task.model_params)
            model_params["max_tokens"] = task.task_max_tokens
            request = ModelCallRequest(
                workflow_id=workflow_id,
                model_role=task.model_role,
                input_artifact_id=prompt_artifact.artifact_id,
                payload={"prompt": task.prompt},
                params=model_params,
                timeout_seconds=task.timeout_seconds,
            )
            model_result = await asyncio.to_thread(self.runtime.model_manager.call_model, request)
            output_payload = self.runtime.artifact_store.read_json(
                model_result.output_artifact.artifact_id
            )
            provenance = LocalModelRunProvenance(
                model_role=model_result.model_role,
                workflow_id=workflow_id,
                model_id=model_result.model_id,
                invocation_id=model_result.invocation_id,
                prompt_artifact=prompt_artifact,
                output_artifact=model_result.output_artifact,
            )
            return AgentResult(
                task_id=task.task_id,
                success=True,
                output=_model_output_text(output_payload),
                provenance=provenance,
            )
        except Exception as exc:
            return AgentResult(
                task_id=task.task_id,
                success=False,
                output="",
                error=exc.failure if isinstance(exc, DurableFailureError) else str(exc),
                provenance=requested,
            )


def runtime_kind_for_harness(harness: AgentHarness) -> AgentRuntimeKind:
    """Map the direct-query harness enum exhaustively without string keys."""

    match harness:
        case AgentHarness.CLAUDE_CODE:
            return AgentRuntimeKind.CLAUDE_CODE
        case AgentHarness.CODEX_CLI:
            return AgentRuntimeKind.CODEX_CLI
    assert_never(harness)


__all__ = [
    "AgentResult",
    "AgentRunProvenance",
    "AgentRuntimeKind",
    "AgentTask",
    "ClaudeCodeAdapter",
    "CliRunProvenance",
    "CodexCLIAdapter",
    "LocalModelAdapter",
    "LocalModelRunProvenance",
    "LocalModelTask",
    "runtime_kind_for_harness",
]
