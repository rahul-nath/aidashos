# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native Code Mode inside the same AiDashOS-owned read-only policy.

The host-side relay owns the socket. The code interpreter gets only pipes,
never a loopback exception, model credentials, or an unsandboxed fallback.
Tool callbacks return to Codex and its existing capability-filtered worker.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from contextlib import ExitStack
from pathlib import Path

from websockets.asyncio.client import connect
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .agent_process_cleanup import await_cleanup, close_process, wait_bounded
from .process_containment import ProcessContainmentUnavailable
from .sandbox_runtime import ReadOnlyToolWorker

_FRAME_LIMIT = 2 * 1024 * 1024


def code_mode_executable(codex_bin: str | Path) -> Path:
    return Path(codex_bin).resolve(strict=True).with_name("codex-code-mode-host")


async def preflight_code_mode_runtime(boundary: ReadOnlyToolWorker, codex_bin: str) -> None:
    """Exercise the pinned IPC interpreter without credentials or model I/O."""
    try:
        # Keep resource cleanup outside the execution timeout.
        async with CodexCodeModeHost(boundary, codex_bin) as host:  # noqa: SIM117
            async with asyncio.timeout(10), connect(host.url, max_size=_FRAME_LIMIT) as socket:

                async def exchange(message: dict) -> dict:
                    encoded = json.dumps(message).encode()
                    await socket.send(len(encoded).to_bytes(4, "little") + encoded)
                    raw = await socket.recv()
                    if (
                        not isinstance(raw, bytes)
                        or len(raw) < 4
                        or int.from_bytes(raw[:4], "little") != len(raw) - 4
                    ):
                        raise ValueError("invalid Code Mode preflight frame")
                    return json.loads(raw[4:])

                ready = await exchange(
                    {
                        "type": "connection/hello",
                        "supportedVersions": [1],
                        "requiredCapabilities": [],
                        "optionalCapabilities": [],
                    }
                )
                if ready != {"type": "connection/ready", "selectedVersion": 1, "capabilities": []}:
                    raise ValueError("Code Mode protocol differs from the verified version")
                opened = await exchange(
                    {
                        "type": "operation/request",
                        "id": 1,
                        "request": {"method": "session/open", "sessionId": "preflight"},
                    }
                )
                if opened.get("result") != {
                    "status": "ok",
                    "value": {"type": "session/ready", "sessionId": "preflight"},
                }:
                    raise ValueError("Code Mode session could not start")
                started = await exchange(
                    {
                        "type": "operation/request",
                        "id": 2,
                        "request": {
                            "method": "session/execute",
                            "sessionId": "preflight",
                            "request": {
                                "tool_call_id": "preflight",
                                "enabled_tools": [],
                                "source": 'text("AIDASHOS_CODE_MODE_READY");',
                                "yield_time_ms": None,
                                "max_output_tokens": None,
                            },
                        },
                    }
                )
                if started.get("result", {}).get("value", {}).get("type") != "execution/started":
                    raise ValueError("Code Mode execution did not start")
                raw = await socket.recv()
                if (
                    not isinstance(raw, bytes)
                    or len(raw) < 4
                    or int.from_bytes(raw[:4], "little") != len(raw) - 4
                ):
                    raise ValueError("invalid Code Mode execution frame")
                completed = json.loads(raw[4:])
                result = completed.get("result", {}).get("value", {}).get("Result", {})
                if (
                    completed.get("type") != "execute/initialResponse"
                    or completed.get("id") != 2
                    or result.get("error_text") is not None
                    or result.get("content_items")
                    != [{"type": "input_text", "text": "AIDASHOS_CODE_MODE_READY"}]
                ):
                    raise ValueError("Code Mode did not return its computed readiness proof")
                host.require_alive()
    except (OSError, ValueError, TimeoutError, ConnectionClosed) as exc:
        raise ProcessContainmentUnavailable(
            "contained Code Mode execution preflight failed"
        ) from exc


class CodexCodeModeHost:
    """A single-use, supervised, contained native interpreter connection."""

    def __init__(self, boundary: ReadOnlyToolWorker, codex_bin: str):
        self.boundary = boundary
        self.executable = code_mode_executable(codex_bin)
        self._stack = ExitStack()
        self._process: asyncio.subprocess.Process | None = None
        self._server: Server | None = None
        self._key = secrets.token_urlsafe(32)
        self._connected = False
        self._disconnected = False
        self._close_task: asyncio.Task[None] | None = None
        self.url = ""

    async def __aenter__(self) -> CodexCodeModeHost:
        if self._process is not None or self._close_task is not None:
            raise ValueError("Code Mode host cannot be reused")
        try:
            launch = self._stack.enter_context(
                self.boundary.contain_service((str(self.executable), "--listen", "stdio"))
            )
            self._process = await asyncio.create_subprocess_exec(
                *launch.command,
                env=dict(launch.environment),
                cwd=str(self.boundary.repository),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=None,
                limit=_FRAME_LIMIT,
            )
            self._server = await serve(
                self._relay,
                "127.0.0.1",
                0,
                max_size=_FRAME_LIMIT,
                max_queue=4,
                origins=[None],
                compression=None,
                close_timeout=1,
            )
            port = self._server.sockets[0].getsockname()[1]
            self.url = f"ws://127.0.0.1:{port}/{self._key}"
            return self
        except BaseException:
            await self.aclose()
            raise

    async def __aexit__(self, exc_type: object, *_: object) -> None:
        await self.aclose()
        if exc_type is None and (self._process is None or self._process.returncode != 0):
            raise ProcessContainmentUnavailable(
                "contained Code Mode host did not exit successfully"
            )

    def require_alive(self) -> None:
        if self._process is None or self._process.returncode is not None or self._disconnected:
            code = None if self._process is None else self._process.returncode
            raise ProcessContainmentUnavailable(
                "contained Code Mode host is unavailable "
                f"(exit={code}, disconnected={self._disconnected})"
            )

    async def _relay(self, socket: ServerConnection) -> None:
        if self._connected or socket.request is None or socket.request.path != f"/{self._key}":
            await socket.close(code=1008, reason="unrecognized Code Mode connection")
            return
        self._connected = True
        assert self._process is not None
        assert self._process.stdin is not None and self._process.stdout is not None
        stdin, stdout = self._process.stdin, self._process.stdout

        async def to_worker() -> None:
            async for raw in socket:
                self.require_alive()
                if not isinstance(raw, bytes) or len(raw) > _FRAME_LIMIT:
                    raise ValueError("Code Mode frame exceeds the bound")
                # Native WebSocket and stdio both carry length-prefixed JSON.
                # Preserve the IPC bytes rather than introducing another envelope.
                if len(raw) < 4 or int.from_bytes(raw[:4], "little") != len(raw) - 4:
                    raise ValueError("invalid Code Mode IPC frame")
                stdin.write(raw)
                await stdin.drain()

        async def from_worker() -> None:
            while True:
                try:
                    header = await stdout.readexactly(4)
                except asyncio.IncompleteReadError as exc:
                    if exc.partial:
                        raise ValueError("truncated Code Mode frame") from exc
                    return
                length = int.from_bytes(header, "little")
                if length > _FRAME_LIMIT:
                    raise ValueError("Code Mode response exceeds the bound")
                await socket.send(header + await stdout.readexactly(length))

        tasks = [asyncio.create_task(to_worker()), asyncio.create_task(from_worker())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except ConnectionClosed:
            pass
        finally:
            self._disconnected = True
            stdin.close()
            for task in tasks:
                task.cancel()
            await wait_bounded(asyncio.gather(*tasks, return_exceptions=True), 2)
            await socket.close(code=1011, reason="Code Mode connection ended")

    async def _close(self) -> None:
        try:
            if self._server is not None:
                self._server.close()
            if self._process is not None:
                await close_process(self._process)
            if self._server is not None:
                await wait_bounded(self._server.wait_closed(), 3)
        finally:
            self._stack.close()

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await await_cleanup(self._close_task)
