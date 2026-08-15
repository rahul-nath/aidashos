# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Agent adapter layer.

Each adapter wraps a different agent runtime so the SagaCoordinator
can dispatch pow-wow tasks to heterogeneous agents without coupling
the coordination logic to any specific runtime.

Available adapters:
  ClaudeCodeAdapter   — claude CLI (subprocess, JSON stream)
  CodexCLIAdapter     — codex CLI (OpenAI Codex CLI)
  LocalLlamaAdapter   — local llama.cpp via ModelManager
  HermesAdapter       — Hermes agent over MCP/HTTP
  OpenCodeAdapter     — OpenCode agent over MCP/HTTP
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .constants import DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """Bound shutdown of an adapter child after timeout or cancellation."""
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
        return
    except TimeoutError:
        process.kill()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        logger.error("agent_adapter_process_did_not_exit_after_kill pid=%s", process.pid)


# ---------------------------------------------------------------------------
# Common task payload
# ---------------------------------------------------------------------------


@dataclass
class AgentTask:
    """Work unit dispatched to an agent adapter."""

    task_id: str
    pow_wow_id: str
    saga_id: str
    role: str
    prompt: str
    allowed_tools: list[str] = field(default_factory=list)
    forbidden_tools: list[str] = field(default_factory=list)
    input_artifacts: list[dict[str, Any]] = field(default_factory=list)
    max_tokens: int = 8192
    timeout_seconds: int = DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS
    session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    """Output from an agent adapter run."""

    task_id: str
    success: bool
    output: str
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    tokens_used: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class AgentAdapter(ABC):
    """Base class for all agent runtime adapters."""

    name: str = "base"

    @abstractmethod
    async def run(self, task: AgentTask) -> AgentResult:
        """Execute a task and return the result."""
        ...

    @abstractmethod
    async def stream(self, task: AgentTask) -> AsyncIterator[str]:
        """Execute a task and stream output deltas."""
        ...

    async def health_check(self) -> bool:
        """Return True if the underlying runtime is reachable."""
        return True


# ---------------------------------------------------------------------------
# Claude Code adapter
# ---------------------------------------------------------------------------


class ClaudeCodeAdapter(AgentAdapter):
    """Adapter for the Claude Code CLI.

    Spawns `claude --print --output-format json` as a subprocess.
    Requires `claude` to be on PATH and authenticated.
    """

    name = "claude_code"

    def __init__(
        self,
        claude_bin: str = "claude",
        model: str | None = None,
        cwd: str | None = None,
    ) -> None:
        self.claude_bin = claude_bin
        self.model = model
        self.cwd = cwd

    def _build_agent_command(self, task: AgentTask) -> list[str]:
        cmd = [self.claude_bin, "--print", "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]
        if task.allowed_tools:
            cmd += ["--allowedTools", ",".join(task.allowed_tools)]
        cmd += [task.prompt]
        return cmd

    async def run(self, task: AgentTask) -> AgentResult:
        cmd = self._build_agent_command(task)
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env={**os.environ, "AGENT_SESSION_ID": task.session_id or ""},
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=task.timeout_seconds
            )
            output_text = stdout.decode("utf-8", errors="replace")
            session_id: str | None = None
            try:
                data = json.loads(output_text)
                result_text = data.get("result", output_text)
                tokens = data.get("usage", {}).get("output_tokens", 0)
                # Claude Code names its transcript file after this id, so
                # carrying it out of the adapter is what makes the conversation
                # findable on disk later.
                raw_session_id = data.get("session_id")
                session_id = str(raw_session_id) if raw_session_id else None
            except json.JSONDecodeError:
                result_text = output_text
                tokens = 0

            success = proc.returncode == 0
            return AgentResult(
                task_id=task.task_id,
                success=success,
                output=result_text,
                tokens_used=tokens,
                error=stderr.decode() if not success else None,
                metadata={"session_id": session_id} if session_id else {},
            )
        except TimeoutError:
            if proc is not None:
                await _terminate_process(proc)
            return AgentResult(
                task_id=task.task_id,
                success=False,
                output="",
                error=f"Timeout after {task.timeout_seconds}s",
            )
        except Exception as exc:
            if proc is not None:
                await _terminate_process(proc)
            return AgentResult(
                task_id=task.task_id,
                success=False,
                output="",
                error=str(exc),
            )

    async def stream(self, task: AgentTask) -> AsyncIterator[str]:
        cmd = [self.claude_bin, "--output-format", "stream-json"]
        if self.model:
            cmd += ["--model", self.model]
        if task.allowed_tools:
            cmd += ["--allowedTools", ",".join(task.allowed_tools)]
        cmd += [task.prompt]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env={**os.environ, "AGENT_SESSION_ID": task.session_id or ""},
        )
        try:
            async with asyncio.timeout(task.timeout_seconds):
                assert proc.stdout is not None
                async for line in proc.stdout:
                    text = line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    try:
                        event = json.loads(text)
                        if event.get("type") == "assistant" and "message" in event:
                            for block in event["message"].get("content", []):
                                if block.get("type") == "text":
                                    yield block["text"]
                    except json.JSONDecodeError:
                        yield text
                await proc.wait()
        except TimeoutError:
            yield f"[Claude Code timeout after {task.timeout_seconds}s]"
        finally:
            await _terminate_process(proc)

    async def health_check(self) -> bool:
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                self.claude_bin,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            return proc.returncode == 0
        except Exception:
            if proc is not None:
                await _terminate_process(proc)
            return False


# ---------------------------------------------------------------------------
# Codex CLI adapter
# ---------------------------------------------------------------------------


class CodexCLIAdapter(AgentAdapter):
    """Adapter for OpenAI Codex CLI.

    Wraps `codex --quiet` for non-interactive batch execution.
    """

    name = "codex_cli"

    def __init__(
        self,
        codex_bin: str = "codex",
        model: str = "o4-mini",
        approval_policy: str = "on-failure",
        cwd: str | None = None,
    ) -> None:
        self.codex_bin = codex_bin
        self.model = model
        self.approval_policy = approval_policy
        self.cwd = cwd

    async def run(self, task: AgentTask) -> AgentResult:
        cmd = [
            self.codex_bin,
            "--quiet",
            "--model",
            self.model,
            "--approval-policy",
            self.approval_policy,
            task.prompt,
        ]
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=task.timeout_seconds
            )
            output = stdout.decode("utf-8", errors="replace")
            success = proc.returncode == 0
            return AgentResult(
                task_id=task.task_id,
                success=success,
                output=output,
                error=stderr.decode() if not success else None,
            )
        except TimeoutError:
            if proc is not None:
                await _terminate_process(proc)
            return AgentResult(
                task_id=task.task_id,
                success=False,
                output="",
                error=f"Timeout after {task.timeout_seconds}s",
            )
        except Exception as exc:
            if proc is not None:
                await _terminate_process(proc)
            return AgentResult(task_id=task.task_id, success=False, output="", error=str(exc))

    async def stream(self, task: AgentTask) -> AsyncIterator[str]:
        result = await self.run(task)
        if result.output:
            yield result.output

    async def health_check(self) -> bool:
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                self.codex_bin,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            return proc.returncode == 0
        except Exception:
            if proc is not None:
                await _terminate_process(proc)
            return False


# ---------------------------------------------------------------------------
# Local llama.cpp adapter (via ModelManager)
# ---------------------------------------------------------------------------


class LocalLlamaAdapter(AgentAdapter):
    """Adapter for local llama.cpp models via the existing ModelManager."""

    name = "local_llama"

    def __init__(self, runtime: Any, model_role: str = "general") -> None:
        self.runtime = runtime
        self.model_role = model_role

    def _resolve_model_role_for_task(self, task: AgentTask) -> Any:
        from .contracts import ModelRole

        role_value = task.metadata.get("model_role", self.model_role)
        return ModelRole(str(role_value))

    @staticmethod
    def _extract_output_text(payload: Any) -> str:
        output = payload.get("output", payload) if isinstance(payload, dict) else payload
        if isinstance(output, dict):
            text = output.get("text")
            if isinstance(text, str):
                return text
            return json.dumps(output, ensure_ascii=False, sort_keys=True)
        if isinstance(output, str):
            return output
        return json.dumps(output, ensure_ascii=False, sort_keys=True)

    async def run(self, task: AgentTask) -> AgentResult:
        try:
            from .contracts import ArtifactRole, ModelCallRequest

            role = self._resolve_model_role_for_task(task)
            mm = self.runtime.model_manager
            requested_workflow_id = task.metadata.get("workflow_id")
            workflow_id = str(requested_workflow_id or f"agent-task-{task.task_id}")
            if requested_workflow_id and not self.runtime.repository.workflow_run_exists(
                workflow_id
            ):
                raise ValueError(f"Local delegate workflow_id {workflow_id!r} is not registered")
            prompt_artifact = self.runtime.artifact_store.write_text(
                role=ArtifactRole.PROMPT.value,
                text=task.prompt,
                workflow_id=workflow_id,
                schema_version="agent_task_prompt.v1",
            )
            model_params = dict(task.metadata.get("model_params") or {})
            model_params.setdefault("max_tokens", task.max_tokens)
            req = ModelCallRequest(
                workflow_id=workflow_id,
                model_role=role,
                input_artifact_id=prompt_artifact.artifact_id,
                payload={"prompt": task.prompt},
                params=model_params,
                timeout_seconds=task.timeout_seconds,
            )
            model_result = await asyncio.to_thread(mm.call_model, req)
            output_payload = self.runtime.artifact_store.read_json(
                model_result.output_artifact.artifact_id
            )
            output = self._extract_output_text(output_payload)
            return AgentResult(
                task_id=task.task_id,
                success=True,
                output=output,
                metadata={
                    "adapter": self.name,
                    "model_role": role.value,
                    "model_id": model_result.model_id,
                    "invocation_id": model_result.invocation_id,
                    "output_artifact_id": model_result.output_artifact.artifact_id,
                },
            )
        except Exception as exc:
            return AgentResult(task_id=task.task_id, success=False, output="", error=str(exc))

    async def stream(self, task: AgentTask) -> AsyncIterator[str]:
        result = await self.run(task)
        if result.output:
            yield result.output
        if result.error:
            yield f"[LocalLlama error: {result.error}]"

    async def health_check(self) -> bool:
        try:
            mm = self.runtime.model_manager
            if getattr(mm.settings, "mock_models", False):
                return True
            role = self._resolve_model_role_for_task(
                AgentTask(
                    task_id="health",
                    pow_wow_id="",
                    saga_id="",
                    role="health",
                    prompt="",
                )
            )
            mm.require_loaded(role)
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Hermes adapter (MCP/HTTP)
# ---------------------------------------------------------------------------


class HermesAdapter(AgentAdapter):
    """Adapter for a Hermes agent running an MCP-over-HTTP server."""

    name = "hermes"

    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _build_request_headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def run(self, task: AgentTask) -> AgentResult:
        import httpx

        payload = {
            "prompt": task.prompt,
            "allowed_tools": task.allowed_tools,
            "max_tokens": task.max_tokens,
            "metadata": task.metadata,
        }
        try:
            async with httpx.AsyncClient(timeout=task.timeout_seconds) as client:
                resp = await client.post(
                    f"{self.base_url}/run",
                    json=payload,
                    headers=self._build_request_headers(),
                )
                resp.raise_for_status()
                data = resp.json()
                return AgentResult(
                    task_id=task.task_id,
                    success=data.get("success", True),
                    output=data.get("output", ""),
                    tokens_used=data.get("tokens_used", 0),
                    artifacts=data.get("artifacts", []),
                )
        except Exception as exc:
            return AgentResult(task_id=task.task_id, success=False, output="", error=str(exc))

    async def stream(self, task: AgentTask) -> AsyncIterator[str]:
        import httpx

        payload = {
            "prompt": task.prompt,
            "allowed_tools": task.allowed_tools,
            "max_tokens": task.max_tokens,
            "stream": True,
        }
        try:
            async with (
                httpx.AsyncClient(timeout=task.timeout_seconds) as client,
                client.stream(
                    "POST",
                    f"{self.base_url}/stream",
                    json=payload,
                    headers=self._build_request_headers(),
                ) as resp,
            ):
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        chunk = line[5:].strip()
                        if chunk and chunk != "[DONE]":
                            try:
                                yield json.loads(chunk).get("delta", "")
                            except json.JSONDecodeError:
                                yield chunk
        except Exception as exc:
            yield f"[Hermes error: {exc}]"

    async def health_check(self) -> bool:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    f"{self.base_url}/health", headers=self._build_request_headers()
                )
                return resp.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# OpenCode adapter (MCP/HTTP)
# ---------------------------------------------------------------------------


class OpenCodeAdapter(AgentAdapter):
    """Adapter for an OpenCode agent running over MCP or HTTP."""

    name = "opencode"

    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def _build_request_headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def run(self, task: AgentTask) -> AgentResult:
        import httpx

        payload = {
            "input": task.prompt,
            "tools": task.allowed_tools,
            "options": {"maxTokens": task.max_tokens},
        }
        try:
            async with httpx.AsyncClient(timeout=task.timeout_seconds) as client:
                resp = await client.post(
                    f"{self.base_url}/session/run",
                    json=payload,
                    headers=self._build_request_headers(),
                )
                resp.raise_for_status()
                data = resp.json()
                return AgentResult(
                    task_id=task.task_id,
                    success=True,
                    output=data.get("output", ""),
                    tokens_used=data.get("usage", {}).get("output_tokens", 0),
                )
        except Exception as exc:
            return AgentResult(task_id=task.task_id, success=False, output="", error=str(exc))

    async def stream(self, task: AgentTask) -> AsyncIterator[str]:
        import httpx

        payload = {"input": task.prompt, "tools": task.allowed_tools, "stream": True}
        try:
            async with (
                httpx.AsyncClient(timeout=task.timeout_seconds) as client,
                client.stream(
                    "POST",
                    f"{self.base_url}/session/stream",
                    json=payload,
                    headers=self._build_request_headers(),
                ) as resp,
            ):
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        chunk = line[5:].strip()
                        if chunk and chunk != "[DONE]":
                            try:
                                yield json.loads(chunk).get("content", "")
                            except json.JSONDecodeError:
                                yield chunk
        except Exception as exc:
            yield f"[OpenCode error: {exc}]"

    async def health_check(self) -> bool:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    f"{self.base_url}/health", headers=self._build_request_headers()
                )
                return resp.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class AgentAdapterRegistry:
    """Manages a set of named agent adapters.

    Role-to-adapter routing follows the tier model:
      STRONG roles → ClaudeCode or Hermes (frontier)
      WEAK roles   → LocalLlama or CodexCLI (local / cheap)
      SPECIAL      → ClaudeCode with restricted tools
    """

    def __init__(self) -> None:
        self._adapters: dict[str, AgentAdapter] = {}

    def register(self, adapter: AgentAdapter, name: str | None = None) -> None:
        key = name or adapter.name
        self._adapters[key] = adapter
        logger.debug("Registered agent adapter: %s", key)

    def get(self, name: str) -> AgentAdapter:
        if name not in self._adapters:
            raise KeyError(f"No adapter registered for '{name}'")
        return self._adapters[name]

    def names(self) -> list[str]:
        return list(self._adapters.keys())

    async def route(
        self,
        tier: str,
        task: AgentTask,
    ) -> AgentResult:
        """Route a task to the appropriate adapter based on tier."""
        tier_lower = tier.lower()
        preference: list[str]
        if tier_lower == "strong":
            preference = ["claude_code", "hermes", "opencode", "local_llama"]
        elif tier_lower == "special":
            preference = ["claude_code", "hermes"]
        else:  # weak
            preference = ["local_llama", "codex_cli", "claude_code"]

        for name in preference:
            adapter = self._adapters.get(name)
            if adapter and await adapter.health_check():
                logger.info("Routing %s task to %s adapter", tier, name)
                result = await adapter.run(task)
                result.metadata.setdefault("adapter", name)
                result.metadata.setdefault("tier", tier_lower)
                return result

        # Fallback: first available
        for name, adapter in self._adapters.items():
            if await adapter.health_check():
                result = await adapter.run(task)
                result.metadata.setdefault("adapter", name)
                result.metadata.setdefault("tier", tier_lower)
                return result

        return AgentResult(
            task_id=task.task_id,
            success=False,
            output="",
            error=f"No healthy adapter available for tier={tier}",
        )

    @classmethod
    def from_settings(cls, settings: Any, runtime: Any) -> AgentAdapterRegistry:
        """Build a registry from application settings."""
        registry = cls()

        # Always try to register local llama (it uses the existing runtime)
        registry.register(LocalLlamaAdapter(runtime, model_role="general"))

        # Claude Code if available
        claude_bin = os.environ.get("CLAUDE_BIN", "claude")
        registry.register(ClaudeCodeAdapter(claude_bin=claude_bin))

        # Codex CLI if available
        codex_bin = os.environ.get("CODEX_BIN", "codex")
        registry.register(CodexCLIAdapter(codex_bin=codex_bin))

        # Hermes if configured
        hermes_url = os.environ.get("HERMES_BASE_URL")
        if hermes_url:
            registry.register(
                HermesAdapter(base_url=hermes_url, api_key=os.environ.get("HERMES_API_KEY"))
            )

        # OpenCode if configured
        opencode_url = os.environ.get("OPENCODE_BASE_URL")
        if opencode_url:
            registry.register(
                OpenCodeAdapter(base_url=opencode_url, api_key=os.environ.get("OPENCODE_API_KEY"))
            )

        return registry
