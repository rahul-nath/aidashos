# SPDX-License-Identifier: AGPL-3.0-or-later
"""Codex's native filesystem worker behind the shared inspection capability.

The relay is trusted and host-side; the native worker receives only stdio.
Every operation, including Codex's own metadata reads, crosses the same closed
authorization port. No RPC method grants process execution to a reviewer.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import stat
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .agent_process_cleanup import await_cleanup, close_process, wait_bounded
from .capabilities import Capability
from .process_containment import ProcessContainmentUnavailable
from .sandbox_runtime import ReadOnlyToolWorker
from .spawn_authority import SpawnAuthority

_MAX_FRAME_BYTES = 2 * 1024 * 1024
_MAX_FILE_BYTES = 1024 * 1024
_RPC_TIMEOUT = 10.0
_RESOURCE_CLOSE_TIMEOUT = 3.0
_FS_METHODS = frozenset({"fs/getMetadata", "fs/readFile", "fs/readDirectory", "fs/canonicalize"})
_ENV_METHODS = frozenset({"environment/info", "environment/status"})


class WorkerOperationDenied(PermissionError):
    """The requested effect is outside the inspection authority."""


class NativeWorkerError(RuntimeError):
    """The native worker completed a request with a typed RPC error."""

    def __init__(self, error: Mapping[str, Any]) -> None:
        self.error = dict(error)
        super().__init__(str(error.get("message", "native worker request failed")))


@dataclass(frozen=True)
class InspectionRpcPolicy:
    repository: Path
    authority: SpawnAuthority
    _root_identity: tuple[int, int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        repository = self.repository.resolve(strict=True)
        metadata = repository.stat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise WorkerOperationDenied("inspection authority requires a repository directory")
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "_root_identity", (metadata.st_dev, metadata.st_ino))

    def assert_repository(self) -> None:
        try:
            resolved = self.repository.resolve(strict=True)
            metadata = resolved.stat()
        except (OSError, RuntimeError) as exc:
            raise WorkerOperationDenied("assigned repository changed after admission") from exc
        if resolved != self.repository or (metadata.st_dev, metadata.st_ino) != self._root_identity:
            raise WorkerOperationDenied("assigned repository changed after admission")

    def repository_path(self, raw: object) -> Path:
        self.assert_repository()
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise WorkerOperationDenied("a repository path is required")
        if raw.startswith("file:"):
            uri = urlsplit(raw)
            if uri.scheme != "file" or uri.netloc or uri.query or uri.fragment:
                raise WorkerOperationDenied("only local file URIs are permitted")
            path = Path(unquote(uri.path))
        elif "://" in raw:
            raise WorkerOperationDenied("remote paths are not repository reads")
        else:
            path = Path(raw)
            if not path.is_absolute():
                path = self.repository / path
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError) as exc:
            raise WorkerOperationDenied("repository path cannot be resolved") from exc
        if not resolved.is_relative_to(self.repository):
            raise WorkerOperationDenied("path is outside the assigned repository")
        return resolved

    def authorize(self, method: str, params: object) -> object:
        if method in _ENV_METHODS:
            if params not in (None, {}):
                raise WorkerOperationDenied("environment probes do not accept overrides")
            return None
        if method not in _FS_METHODS:
            raise WorkerOperationDenied(f"inspection authority does not grant {method}")
        if Capability.READ_REPOSITORY not in self.authority.capabilities:
            raise WorkerOperationDenied("READ_REPOSITORY was not granted")
        if not isinstance(params, dict) or set(params) - {"path", "sandbox"}:
            raise WorkerOperationDenied("invalid filesystem request shape")
        path = self.repository_path(params.get("path"))
        # The verified SRT worker is the policy owner. Codex's helper must not
        # install a nested sandbox or choose a different filesystem authority.
        return {"path": path.as_uri(), "sandbox": None}


READ_REPOSITORY_TOOL = {
    "name": "read_repository",
    "description": "Read UTF-8 repository files or list repository directories without a shell.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["read_file", "list_directory"]},
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "max_lines": {"type": "integer", "minimum": 1, "maximum": 1000},
        },
        "required": ["operation", "path"],
        "additionalProperties": False,
    },
}


class CodexToolWorker:
    """One process lifetime, one native session, one fail-closed effect port."""

    def __init__(
        self,
        boundary: ReadOnlyToolWorker,
        codex_bin: str,
        authority: SpawnAuthority,
    ) -> None:
        self.boundary = boundary
        self.codex_bin = codex_bin
        self.policy = InspectionRpcPolicy(boundary.repository, authority)
        self.disconnected = asyncio.Event()
        self._stack = ExitStack()
        self._process: asyncio.subprocess.Process | None = None
        self._server: Server | None = None
        self._reader: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self._session_id = ""
        self._url = ""
        self._relay_key = secrets.token_urlsafe(32)
        self._connected = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._sequence = 0
        self.failure_detail: str | None = None

    @property
    def url(self) -> str:
        if not self._url:
            raise ProcessContainmentUnavailable("worker relay is not ready")
        return self._url

    @property
    def is_alive(self) -> bool:
        return (
            not self._closed
            and not self.disconnected.is_set()
            and self._process is not None
            and self._process.returncode is None
        )

    def assert_alive(self) -> None:
        if not self.is_alive:
            raise ProcessContainmentUnavailable("native inspection worker is disconnected")

    async def __aenter__(self) -> CodexToolWorker:
        if self._process is not None or self._closed:
            raise ValueError("worker contexts cannot be reused")
        if Capability.READ_REPOSITORY not in self.policy.authority.capabilities:
            raise WorkerOperationDenied("READ_REPOSITORY was not granted")
        try:
            command = (
                self.codex_bin,
                "-c",
                "analytics.enabled=false",
                "exec-server",
                "--listen",
                "stdio",
                "--concurrent-requests",
                "4",
            )
            contained = self._stack.enter_context(self.boundary.contain_service(command))
            # No new process group: the existing execution supervisor owns all
            # descendants of the model driver, including this native worker.
            self._process = await asyncio.create_subprocess_exec(
                *contained.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=dict(contained.environment),
                cwd=str(self.boundary.repository),
                limit=_MAX_FRAME_BYTES,
            )
            self._reader = asyncio.create_task(self._read_responses())
            result = await self._request(
                "initialize", {"clientName": "aidashos-inspection", "resumeSessionId": None}
            )
            if not isinstance(result, dict) or not isinstance(result.get("sessionId"), str):
                raise ProcessContainmentUnavailable("native worker returned no session identity")
            self._session_id = result["sessionId"]
            await self._write({"method": "initialized", "params": {}})
            await self.require_ready()
            self._server = await serve(
                self._relay,
                "127.0.0.1",
                0,
                max_size=_MAX_FRAME_BYTES,
                max_queue=4,
                origins=[None],
                compression=None,
                close_timeout=1,
            )
            port = self._server.sockets[0].getsockname()[1]
            self._url = f"ws://127.0.0.1:{port}/{self._relay_key}"
            return self
        except BaseException:
            await self.aclose()
            raise

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def _write(self, message: Mapping[str, Any]) -> None:
        self.assert_alive()
        assert self._process is not None and self._process.stdin is not None
        encoded = (json.dumps(message) + "\n").encode()
        if len(encoded) > _MAX_FRAME_BYTES:
            raise WorkerOperationDenied("native request exceeds the frame bound")
        async with self._write_lock:
            try:
                self._process.stdin.write(encoded)
                await self._process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                self._fail_pending()
                raise ProcessContainmentUnavailable("native worker transport closed") from exc

    async def _request(self, method: str, params: object) -> Any:
        self._sequence += 1
        request_id = f"aidashos-{self._sequence}"
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, _RPC_TIMEOUT)
        except TimeoutError as exc:
            self._fail_pending()
            raise ProcessContainmentUnavailable("native worker RPC deadline exceeded") from exc
        finally:
            self._pending.pop(request_id, None)
            if future.done() and not future.cancelled():
                # A failed write may invalidate this future before await starts.
                # Retrieve its exception so the initiating call owns the failure.
                future.exception()

    def _fail_pending(self) -> None:
        self.disconnected.set()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(
                    ProcessContainmentUnavailable("native inspection worker disconnected")
                )

    async def _read_responses(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while raw := await self._process.stdout.readline():
                message = json.loads(raw)
                if not isinstance(message, dict) or "id" not in message:
                    raise ValueError("unexpected native worker message")
                future = self._pending.get(str(message["id"]))
                if future is None or future.done():
                    raise ValueError("native response has no outstanding request")
                if "error" in message:
                    future.set_exception(NativeWorkerError(message["error"]))
                elif "result" in message:
                    future.set_result(message["result"])
                else:
                    raise ValueError("native response has no result")
        except asyncio.CancelledError:
            raise
        except (ValueError, TypeError, OSError) as exc:
            self.failure_detail = f"invalid native worker transport: {exc}"
        finally:
            self._fail_pending()
            if self._server is not None:
                self._server.close()

    async def require_ready(self) -> None:
        self.assert_alive()
        self.policy.assert_repository()
        info = await self._request("environment/info", None)
        if not isinstance(info, dict) or not isinstance(info.get("shell"), dict):
            raise ProcessContainmentUnavailable("native environment info is malformed")
        status = await self._request("environment/status", None)
        if status != {"status": "ready"}:
            raise ProcessContainmentUnavailable("native environment did not attest ready")
        self.assert_alive()

    async def _authorized_request(self, method: str, params: object) -> Any:
        """One admission gateway for both native and dynamic repository reads."""
        authorized = self.policy.authorize(method, params)
        if method == "fs/readFile":
            metadata = await self._request("fs/getMetadata", authorized)
            if (
                not isinstance(metadata, dict)
                or metadata.get("isFile") is not True
                or type(metadata.get("size")) is not int
                or not 0 <= metadata["size"] <= _MAX_FILE_BYTES
            ):
                raise WorkerOperationDenied("path is not a bounded regular file")
            # An awaited metadata RPC cannot authorize a subsequently retargeted path.
            authorized = self.policy.authorize(method, authorized)
        result = await self._request(method, authorized)
        if method == "fs/readFile":
            data = base64.b64decode(result["dataBase64"], validate=True)
            if len(data) > _MAX_FILE_BYTES:
                raise WorkerOperationDenied("file exceeds the inspection output bound")
        return result

    async def _relay(self, socket: ServerConnection) -> None:
        if (
            self._connected
            or socket.request is None
            or socket.request.path != f"/{self._relay_key}"
        ):
            await socket.close(code=1008, reason="unrecognized worker connection")
            return
        self._connected = True
        initialized = False
        try:
            async for raw in socket:
                message = json.loads(raw)
                if not isinstance(message, dict) or not isinstance(message.get("method"), str):
                    await socket.close(code=1008, reason="invalid worker request")
                    return
                method, params = message["method"], message.get("params")
                request_id = message.get("id")
                try:
                    self.assert_alive()
                    if method != "initialized" and type(request_id) not in (str, int):
                        await socket.close(code=1008, reason="effect requests need an RPC identity")
                        return
                    if method == "initialize":
                        if initialized or not isinstance(params, dict):
                            raise WorkerOperationDenied("invalid worker initialization")
                        if params.get("resumeSessionId") not in (None, self._session_id):
                            raise WorkerOperationDenied("worker session identity changed")
                        initialized = True
                        result = {"sessionId": self._session_id}
                    elif not initialized:
                        raise WorkerOperationDenied("worker connection is not initialized")
                    elif method == "initialized":
                        continue
                    else:
                        result = await self._authorized_request(method, params)
                    await socket.send(json.dumps({"id": request_id, "result": result}))
                except WorkerOperationDenied as exc:
                    if request_id is None:
                        await socket.close(code=1008, reason="unauthorized notification")
                        return
                    await socket.send(
                        json.dumps(
                            {"id": request_id, "error": {"code": -32003, "message": str(exc)}}
                        )
                    )
                except NativeWorkerError as exc:
                    await socket.send(json.dumps({"id": request_id, "error": exc.error}))
        except (ConnectionClosed, ValueError, ProcessContainmentUnavailable):
            await socket.close(code=1011, reason="worker unavailable")
        finally:
            # A lost driver connection invalidates this worker. It must never
            # be silently reattached to a fresh conversation or recovered host.
            self._fail_pending()

    async def read_repository(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise WorkerOperationDenied("read_repository requires an object")
        operation = arguments.get("operation")
        allowed_keys = {"operation", "path"}
        if operation == "read_file":
            allowed_keys |= {"start_line", "max_lines"}
        elif operation != "list_directory":
            raise WorkerOperationDenied("unknown repository read operation")
        if set(arguments) - allowed_keys:
            raise WorkerOperationDenied("unexpected repository read argument")
        params = {"path": arguments.get("path"), "sandbox": None}
        if operation == "list_directory":
            metadata = await self._authorized_request("fs/getMetadata", params)
            if metadata.get("isDirectory") is not True:
                raise WorkerOperationDenied("path is not a directory")
            result = await self._authorized_request("fs/readDirectory", params)
            entries = result["entries"]
            return {"entries": entries[:1000], "truncated": len(entries) > 1000}
        start, limit = arguments.get("start_line", 1), arguments.get("max_lines", 200)
        if type(start) is not int or start < 1 or type(limit) is not int or not 1 <= limit <= 1000:
            raise WorkerOperationDenied("invalid file line bounds")
        result = await self._authorized_request("fs/readFile", params)
        data = base64.b64decode(result["dataBase64"], validate=True)
        try:
            lines = data.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise WorkerOperationDenied("file is not UTF-8 text") from exc
        selected = lines[start - 1 : start - 1 + limit]
        return {
            "path": arguments["path"],
            "start_line": start,
            "text": "\n".join(selected),
            "total_lines": len(lines),
            "truncated": start - 1 + len(selected) < len(lines),
        }

    async def aclose(self) -> None:
        if self._close_task is None:
            self._fail_pending()
            self._close_task = asyncio.create_task(self._close())
        await await_cleanup(self._close_task)

    async def _close(self) -> None:
        failures: list[Exception] = []
        try:
            if self._server is not None:
                try:
                    self._server.close()
                    await wait_bounded(self._server.wait_closed(), _RESOURCE_CLOSE_TIMEOUT)
                except Exception as exc:
                    failures.append(exc)
            if self._process is not None:
                try:
                    await close_process(self._process)
                except Exception as exc:
                    failures.append(exc)
            if self._reader is not None:
                self._reader.cancel()
                try:
                    await wait_bounded(
                        asyncio.gather(self._reader, return_exceptions=True),
                        _RESOURCE_CLOSE_TIMEOUT,
                    )
                except Exception as exc:
                    failures.append(exc)
        finally:
            self._stack.close()
        if failures:
            raise ProcessContainmentUnavailable(
                "inspection worker cleanup failed"
            ) from ExceptionGroup("worker cleanup failures", failures)
        self._closed = True
