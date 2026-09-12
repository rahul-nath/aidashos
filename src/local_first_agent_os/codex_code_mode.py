# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native Code Mode inside the same AiDashOS-owned read-only policy.

The host-side relay owns the socket. The code interpreter gets only pipes,
never a loopback exception, model credentials, or an unsandboxed fallback.
Tool callbacks return to Codex and its existing capability-filtered worker.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import tempfile
from contextlib import ExitStack, suppress
from pathlib import Path

from .agent_process_cleanup import await_cleanup, close_process, wait_bounded
from .codex_stdio_relay import FRAME_LIMIT, CodeModeEndpoint, connect_endpoint, read_frame
from .process_containment import ProcessContainmentUnavailable
from .sandbox_runtime import ReadOnlyToolWorker


def code_mode_executable(codex_bin: str | Path) -> Path:
    return Path(codex_bin).resolve(strict=True).with_name("codex-code-mode-host")


async def preflight_code_mode_runtime(boundary: ReadOnlyToolWorker, codex_bin: str) -> None:
    """Exercise the pinned IPC interpreter without credentials or model I/O."""
    try:
        # Keep resource cleanup outside the execution timeout.
        async with CodexCodeModeHost(boundary, codex_bin) as host:  # noqa: SIM117
            async with asyncio.timeout(10):
                reader, writer = await connect_endpoint(host.endpoint)

                try:

                    async def exchange(message: dict) -> dict:
                        encoded = json.dumps(message).encode()
                        writer.write(len(encoded).to_bytes(4, "little") + encoded)
                        await writer.drain()
                        raw = await read_frame(reader)
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
                    if ready != {
                        "type": "connection/ready",
                        "selectedVersion": 1,
                        "capabilities": [],
                    }:
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
                    if (
                        started.get("result", {}).get("value", {}).get("type")
                        != "execution/started"
                    ):
                        raise ValueError("Code Mode execution did not start")
                    raw = await read_frame(reader)
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
                finally:
                    writer.close()
                    with suppress(OSError):
                        await wait_bounded(writer.wait_closed(), 2)

    except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError) as exc:
        raise ProcessContainmentUnavailable(
            "contained Code Mode execution preflight failed"
        ) from exc


class CodexCodeModeHost:
    """A single-use, supervised interpreter with a private authenticated relay.

    The listener, every connection and the contained process share one owner.
    The interpreter receives pipes only; it never receives socket authority.
    """

    def __init__(self, boundary: ReadOnlyToolWorker, codex_bin: str):
        self.boundary = boundary
        self.executable = code_mode_executable(codex_bin)
        self._stack = ExitStack()
        self._process: asyncio.subprocess.Process | None = None
        self._server: asyncio.Server | None = None
        self._endpoint: CodeModeEndpoint | None = None
        self._connections: set[asyncio.StreamWriter] = set()
        self._handlers: set[asyncio.Task[None]] = set()
        self._connected = False
        self._disconnected = False
        self._failure: str | None = None
        self._close_task: asyncio.Task[None] | None = None

    @property
    def endpoint(self) -> CodeModeEndpoint:
        if self._endpoint is None:
            raise ValueError("Code Mode endpoint is not prepared")
        return self._endpoint

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
                limit=FRAME_LIMIT,
            )
            # macOS sockaddr_un is short even when TMPDIR or the repository is long.
            directory = Path(
                self._stack.enter_context(
                    tempfile.TemporaryDirectory(prefix="acm-", dir=Path("/tmp").resolve())
                )
            )
            directory.chmod(0o700)
            self._endpoint = CodeModeEndpoint(directory / "ipc", secrets.token_urlsafe(32))
            self._server = await asyncio.start_unix_server(
                self._accept, path=self.endpoint.path, limit=FRAME_LIMIT, backlog=4
            )
            self.endpoint.path.chmod(0o600)
            return self
        except BaseException:
            await self.aclose()
            raise

    async def __aexit__(self, exc_type: object, *_: object) -> None:
        await self.aclose()
        if exc_type is None and (
            self._process is None or self._process.returncode != 0 or self._failure is not None
        ):
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

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._close_task is not None or len(self._handlers) >= 4:
            writer.close()
            return
        self._connections.add(writer)
        task = asyncio.create_task(self._relay(reader, writer))
        self._handlers.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._handlers.discard(task)
        if not task.cancelled() and (failure := task.exception()) is not None:
            self._failure = type(failure).__name__

    async def _relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        admitted = False
        tasks: list[asyncio.Task[None]] = []
        try:
            async with asyncio.timeout(5):
                supplied = await reader.readexactly(44)
                if not hmac.compare_digest(supplied, self.endpoint.token.encode("ascii") + b"\n"):
                    return
            # The check and assignment contain no await: concurrent handshakes cannot
            # attach a second reader to the interpreter's stdout.
            if self._connected or self._close_task is not None:
                return
            self.require_alive()
            self._connected = admitted = True
            assert self._process is not None
            assert self._process.stdin is not None and self._process.stdout is not None
            stdin, stdout = self._process.stdin, self._process.stdout
            writer.write(b"ok\n")
            await writer.drain()

            async def transfer(
                source: asyncio.StreamReader, destination: asyncio.StreamWriter
            ) -> None:
                while (frame := await read_frame(source)) is not None:
                    self.require_alive()
                    destination.write(frame)
                    await destination.drain()

            tasks = [
                asyncio.create_task(transfer(reader, stdin)),
                asyncio.create_task(transfer(stdout, writer)),
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (
            OSError,
            ValueError,
            TimeoutError,
            asyncio.IncompleteReadError,
            ProcessContainmentUnavailable,
        ) as failure:
            if admitted:
                self._failure = type(failure).__name__
        finally:
            if admitted:
                self._disconnected = True
                assert self._process is not None and self._process.stdin is not None
                self._process.stdin.close()
            for task in tasks:
                task.cancel()
            if tasks:
                await wait_bounded(asyncio.gather(*tasks, return_exceptions=True), 2)
            writer.close()
            self._connections.discard(writer)
            with suppress(OSError, TimeoutError):
                await wait_bounded(writer.wait_closed(), 2)

    async def _close(self) -> None:
        try:
            if self._server is not None:
                self._server.close()
            for writer in tuple(self._connections):
                writer.close()
            for task in tuple(self._handlers):
                task.cancel()
            if self._process is not None:
                await close_process(self._process)
            if self._handlers:
                await wait_bounded(asyncio.gather(*self._handlers, return_exceptions=True), 3)
            if self._server is not None:
                await wait_bounded(self._server.wait_closed(), 3)
        finally:
            self._stack.close()

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await await_cleanup(self._close_task)
