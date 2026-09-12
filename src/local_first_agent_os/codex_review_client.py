# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Codex model connection over an AiDashOS-owned inspection worker.

The client owns model I/O, never the execution of a repository effect.
Every repository read crosses the worker port; commands are not implied by
READ_REPOSITORY. No client, environment or model fallback is attempted.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .agent_process_cleanup import await_cleanup, close_process, wait_bounded
from .capabilities import Capability
from .codex_code_mode import CodexCodeModeHost
from .codex_local_stdio import prepare_local_stdio_client
from .codex_tool_worker import (
    READ_REPOSITORY_TOOL,
    CodexToolWorker,
    NativeWorkerError,
    WorkerOperationDenied,
)
from .process_containment import ProcessContainmentUnavailable

_MAX_MESSAGE = 2 * 1024 * 1024
_ENVIRONMENT = "aidashos-inspection"
_DISABLED_FEATURES = (
    "apps",
    "plugins",
    "hooks",
    "multi_agent",
    "shell_tool",
    "unified_exec",
    "shell_snapshot",
    "image_generation",
    "view_image",
    "remote_plugin",
    "memories",
    "skill_mcp_dependency_install",
)


@dataclass(frozen=True)
class CodexSubscription:
    model: str
    auth_file: Path


class _ExternalSubscription:
    """Read-only consumer of the user's credential owner, never an OAuth refresher.

    Sharing a managed auth.json lets an ephemeral client rotate its owner's
    refresh token. External-token login gives this client only access authority;
    the normal Codex login retains refresh-token ownership and persistence.
    """

    def __init__(self, auth_file: Path):
        self.auth_file = auth_file
        self.current = self._read()

    def _read(self) -> dict[str, str]:
        try:
            with self.auth_file.open("rb") as stream:
                encoded = stream.read(_MAX_MESSAGE + 1)
            if len(encoded) > _MAX_MESSAGE:
                raise ValueError("oversized credential source")
            tokens = json.loads(encoded)["tokens"]
            access, account = tokens["access_token"], tokens["account_id"]
            if (
                not isinstance(access, str)
                or not access
                or not isinstance(account, str)
                or not account
            ):
                raise ValueError("missing subscription access identity")
            return {"accessToken": access, "chatgptAccountId": account}
        except (OSError, ValueError, TypeError, KeyError):
            raise ProcessContainmentUnavailable(
                "Codex subscription access is unavailable; authenticate in the owning Codex session"
            ) from None

    def login(self) -> dict[str, str]:
        return {"type": "chatgptAuthTokens", **self.current}

    def refresh(self, params: Mapping[str, Any]) -> dict[str, str]:
        fresh = self._read()
        if (
            params.get("reason") != "unauthorized"
            or params.get("previousAccountId") not in {None, self.current["chatgptAccountId"]}
            or fresh["chatgptAccountId"] != self.current["chatgptAccountId"]
            or fresh["accessToken"] == self.current["accessToken"]
        ):
            raise ProcessContainmentUnavailable(
                "Codex subscription needs refresh by its owning session; review cannot continue"
            )
        self.current = fresh
        return dict(fresh)


@dataclass(frozen=True)
class LocalFixtureModel:
    """A credential-free local test provider, not a production fallback."""

    model: str
    url: str

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username
            or parsed.password
            or not parsed.port
        ):
            raise ValueError("fixture provider must be an explicit loopback HTTP endpoint")


type ReviewModel = CodexSubscription | LocalFixtureModel


def _client_command(codex_bin: str, model: ReviewModel) -> list[str]:
    command = [codex_bin, "--strict-config"]
    for feature in _DISABLED_FEATURES:
        command.extend(("--disable", feature))
    for setting in (
        "analytics.enabled=false",
        'web_search="disabled"',
        "allow_login_shell=false",
        'approval_policy="never"',
        'sandbox_mode="read-only"',
    ):
        command.extend(("-c", setting))
    if isinstance(model, LocalFixtureModel):
        for setting in (
            'model_provider="aidashos_fixture"',
            'model_providers.aidashos_fixture.name="AiDashOS local fixture"',
            f"model_providers.aidashos_fixture.base_url={json.dumps(model.url)}",
            'model_providers.aidashos_fixture.wire_api="responses"',
            "model_providers.aidashos_fixture.requires_openai_auth=false",
        ):
            command.extend(("-c", setting))
    return [
        *command,
        "--enable",
        "code_mode_host",
        "--enable",
        "code_mode",
        "app-server",
    ]


class _Client:
    def __init__(
        self,
        process: asyncio.subprocess.Process,
        worker: CodexToolWorker,
        code_host: CodexCodeModeHost,
        subscription: _ExternalSubscription | None = None,
    ):
        self.process = process
        self.worker = worker
        self.code_host = code_host
        self.subscription = subscription
        self.sequence = 0
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
        self.reader: asyncio.Task[None] | None = None

    async def send(self, value: Mapping[str, Any]) -> None:
        if self.process.stdin is None:
            raise ProcessContainmentUnavailable("Codex client input is unavailable")
        self.process.stdin.write((json.dumps(value) + "\n").encode())
        await self.process.stdin.drain()

    async def rpc(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        self.sequence += 1
        key = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        await self.send({"id": key, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, 20)
        finally:
            self.pending.pop(key, None)

    async def pump(self) -> None:
        assert self.process.stdout is not None
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("Codex client emitted a non-object message")
                if (
                    message.get("method") == "account/chatgptAuthTokens/refresh"
                    and "id" in message
                    and self.subscription is not None
                ):
                    result = self.subscription.refresh(message["params"])
                    await self.send({"id": message["id"], "result": result})
                elif "method" not in message and message.get("id") in self.pending:
                    future = self.pending[message["id"]]
                    if future.done():
                        raise ValueError("duplicate Codex RPC response")
                    if "error" in message:
                        future.set_exception(ProcessContainmentUnavailable(str(message["error"])))
                    else:
                        future.set_result(message["result"])
                else:
                    self.events.put_nowait(message)
            raise ProcessContainmentUnavailable("Codex client transport closed")
        except Exception as exc:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(exc)
            self.events.put_nowait({"method": "aidashos/clientFailure", "error": str(exc)})

    async def next_event(self) -> dict[str, Any]:
        # Couple completion to worker liveness even when the model is silent.
        while True:
            self.worker.assert_alive()
            self.code_host.require_alive()
            if self.reader is not None and self.reader.done() and self.events.empty():
                raise ProcessContainmentUnavailable("Codex client event reader stopped")
            try:
                return await asyncio.wait_for(self.events.get(), 0.5)
            except TimeoutError:
                continue


async def _close_client(client: _Client) -> None:
    try:
        await close_process(client.process, grace_seconds=5)
    finally:
        if client.reader is not None:
            client.reader.cancel()
            await wait_bounded(asyncio.gather(client.reader, return_exceptions=True), 2)


async def run_read_only_review(
    *,
    worker: CodexToolWorker,
    codex_bin: str,
    repository: Path,
    model: ReviewModel,
    prompt: str,
    emit: Callable[[Mapping[str, Any]], None],
    effort: str | None = None,
) -> str:
    """Use an already-admitted worker; transport failures cannot become approval."""
    if Capability.INVOKE_MODEL not in worker.policy.authority.capabilities:
        raise WorkerOperationDenied("INVOKE_MODEL was not granted")
    if repository.resolve(strict=True) != worker.boundary.repository.resolve(strict=True):
        raise WorkerOperationDenied("model connection does not match the admitted repository")
    await worker.require_ready()
    async with AsyncExitStack() as resources:
        raw = resources.enter_context(tempfile.TemporaryDirectory(prefix="aidashos-model-client-"))
        home = Path(raw).resolve()
        codex_home = home / "codex"
        codex_home.mkdir(mode=0o700)
        subscription = (
            _ExternalSubscription(model.auth_file) if isinstance(model, CodexSubscription) else None
        )
        environment = {
            "HOME": str(home),
            "CODEX_HOME": str(codex_home),
            "TMPDIR": str(home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "SHELL": "/bin/sh",
        }
        code_host = await resources.enter_async_context(
            CodexCodeModeHost(worker.boundary, codex_bin)
        )
        prepared = prepare_local_stdio_client(codex_bin, endpoint=code_host.endpoint, home=home)
        emit({"type": "codex.local_stdio.prepared", **prepared.provenance()})
        process = await asyncio.create_subprocess_exec(
            *_client_command(str(prepared.executable), model),
            cwd=home,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # Inherit stderr so the existing supervisor owns bounded capture.
            stderr=None,
            limit=_MAX_MESSAGE,
        )
        client = _Client(process, worker, code_host, subscription)
        client.reader = asyncio.create_task(client.pump())
        try:
            await client.rpc(
                "initialize",
                {
                    "clientInfo": {"name": "aidashos_review", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            await client.send({"method": "initialized", "params": {}})
            if subscription is not None:
                try:
                    await client.rpc("account/login/start", subscription.login())
                except ProcessContainmentUnavailable:
                    raise ProcessContainmentUnavailable(
                        "Codex external-token authentication failed"
                    ) from None
            await client.rpc(
                "environment/add",
                {
                    "environmentId": _ENVIRONMENT,
                    "execServerUrl": worker.url,
                    "connectTimeoutMs": 5000,
                },
            )
            await client.rpc("environment/info", {"environmentId": _ENVIRONMENT})
            await worker.require_ready()
            thread = await client.rpc(
                "thread/start",
                {
                    "model": model.model,
                    "ephemeral": True,
                    "cwd": str(repository),
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "environments": [{"environmentId": _ENVIRONMENT, "cwd": str(repository)}],
                    "dynamicTools": [READ_REPOSITORY_TOOL],
                    "developerInstructions": (
                        "Inspect repository text using read_repository. "
                        "Shell commands and mutation are not authorized. "
                        "Use supplied verification evidence rather than running checks yourself. "
                        "Report CANNOT_REVIEW if required evidence is unavailable."
                    ),
                },
            )
            thread_id = thread["thread"]["id"]
            emit({"type": "thread.started", "thread_id": thread_id})
            params: dict[str, Any] = {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "environments": [{"environmentId": _ENVIRONMENT, "cwd": str(repository)}],
                "sandboxPolicy": {"type": "externalSandbox", "networkAccess": "restricted"},
            }
            if effort:
                params["effort"] = effort
            await worker.require_ready()
            started = await client.rpc("turn/start", params)
            turn_id = started["turn"]["id"]
            if not isinstance(turn_id, str) or not turn_id:
                raise ProcessContainmentUnavailable("Codex review turn identity is unavailable")
            usage_notification: Mapping[str, Any] | None = None
            text = ""
            while True:
                code_host.require_alive()
                message = await client.next_event()
                method = message.get("method")
                payload = message.get("params", {})
                if method == "item/tool/call":
                    if payload.get("tool") != "read_repository":
                        raise ProcessContainmentUnavailable("unexpected client-side tool request")
                    try:
                        result = await worker.read_repository(payload["arguments"])
                        response = {
                            "success": True,
                            "contentItems": [
                                {"type": "inputText", "text": json.dumps(result)},
                            ],
                        }
                    except (PermissionError, NativeWorkerError) as exc:
                        # A completed RPC can reject an individual read (for
                        # example ENOENT) without losing the worker boundary.
                        # Transport/liveness failures are not caught here.
                        response = {
                            "success": False,
                            "contentItems": [
                                {"type": "inputText", "text": str(exc)},
                            ],
                        }
                    await client.send({"id": message["id"], "result": response})
                    emit(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "repository_read",
                                "success": response["success"],
                            },
                        }
                    )
                elif "id" in message:
                    # No implicit approval, auth refresh, MCP elicitation or
                    # other host effect can be requested through this adapter.
                    raise ProcessContainmentUnavailable(f"unsupported client request: {method}")
                elif method == "item/completed":
                    item = payload.get("item", {})
                    if item.get("type") == "agentMessage":
                        text = item.get("text", "")
                        emit(
                            {
                                "type": "item.completed",
                                "item": {
                                    "type": "agent_message",
                                    "text": text,
                                },
                            }
                        )
                elif method == "thread/tokenUsage/updated":
                    if payload.get("threadId") != thread_id or payload.get("turnId") != turn_id:
                        raise ProcessContainmentUnavailable("Codex review usage identity differs")
                    # This is one fresh ephemeral thread and one turn. Its total
                    # is cumulative across model calls; summing updates or using
                    # `last` would miscount the invocation. Retain the final
                    # notification unchanged, even if its counters are malformed.
                    usage_notification = payload
                elif method == "turn/completed":
                    if (
                        payload.get("threadId") != thread_id
                        or payload.get("turn", {}).get("id") != turn_id
                    ):
                        raise ProcessContainmentUnavailable(
                            "Codex review completion identity differs"
                        )
                    if payload.get("turn", {}).get("status") != "completed":
                        raise ProcessContainmentUnavailable("Codex review turn did not complete")
                    await worker.require_ready()
                    code_host.require_alive()
                    if not text:
                        raise ProcessContainmentUnavailable("Codex review returned no final report")
                    break
                elif method in {"error", "aidashos/clientFailure"}:
                    raise ProcessContainmentUnavailable(str(message))
        finally:
            await await_cleanup(asyncio.create_task(_close_client(client)))
        # A final report is provisional until the native client is reaped.
        # The last worker proof precedes shutdown because closing the client
        # intentionally invalidates its one-use worker connection.
        if client.process.returncode != 0:
            raise ProcessContainmentUnavailable("Codex review client did not exit successfully")
        prepared.verify()
    # Completion is emitted only after the contained interpreter scope closes.
    # App-server lifecycle completion does not promise CLI turn.completed usage.
    # Do not retain the terminal's item collection, which can contain reasoning.
    emit(
        {
            "type": "codex.app_server.turn.completed",
            "threadId": thread_id,
            "turnId": turn_id,
            "usage_notification": usage_notification,
        }
    )
    return text
