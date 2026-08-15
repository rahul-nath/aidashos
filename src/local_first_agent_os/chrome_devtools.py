# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Supervised Chrome DevTools MCP lifecycle for the /chrome workflow.

Three layers with separate responsibilities:

- ``ChromeDevToolsProcessSupervisor`` owns exactly one MCP child process per
  runtime: spawn, process-group identity, bounded stream draining, and
  process-tree cleanup.
- ``ChromeDevToolsMcpClient`` owns JSON-RPC framing over an already supervised
  process and classifies protocol failures into structured error codes.
- ``ChromeControlService`` classifies actions, gates mutations, implements
  real start/status/stop semantics, and shapes ``chrome_control_result.v2``
  results with a ``chrome_control_result.v1`` compatibility projection.

The supervised process is never created per request. Attachment to a browser
is a separate lifecycle phase from MCP initialization, and read-only actions
never launch a browser profile in the attach modes.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

from .settings import Settings

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2025-06-18"
CHROME_CONTROL_RESULT_V2 = "chrome_control_result.v2"
CHROME_CONTROL_RESULT_V1 = "chrome_control_result.v1"

# Keys of the legacy tool output that v1 consumers may read from the result.
_V1_PAYLOAD_KEYS = (
    "args",
    "invocations",
    "pages",
    "matched_pages",
    "page_snapshots",
    "page_screenshots",
    "match_count",
    "snapshot_count",
    "screenshot_count",
    "category",
    "decision_prompt",
    "confirmed",
    "dry_run",
    "closed_page_ids",
    "warning",
)

OBSERVATIONAL_CHROME_ACTIONS = frozenset(
    {
        "list",
        "gather",
        "read",
        "summarize",
        "decide",
        "console",
        "network",
        "snapshot",
        "screenshot",
    }
)
MUTATING_CHROME_ACTIONS = frozenset(
    {
        "open",
        "navigate",
        "back",
        "forward",
        "reload",
        "evaluate",
        "select",
        "close",
        "close_category",
    }
)
LIFECYCLE_CHROME_ACTIONS = frozenset({"start", "status", "stop"})


def is_mutating_chrome_action(action: str) -> bool:
    """Classify an action; unknown actions are programmer errors and crash."""

    if action in MUTATING_CHROME_ACTIONS:
        return True
    if action in OBSERVATIONAL_CHROME_ACTIONS or action in LIFECYCLE_CHROME_ACTIONS:
        return False
    raise ValueError(f"Unknown chrome action: {action}")


class ChromeDevToolsErrorCode(StrEnum):
    COMMAND_NOT_RESOLVED = "command_not_resolved"
    PROCESS_START_FAILED = "process_start_failed"
    INITIALIZE_TIMEOUT = "initialize_timeout"
    INITIALIZE_PROTOCOL_ERROR = "initialize_protocol_error"
    BROWSER_ATTACH_FAILED = "browser_attach_failed"
    CHILD_EXITED = "child_exited"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_PROTOCOL_ERROR = "tool_protocol_error"
    MUTATION_NOT_ALLOWED = "mutation_not_allowed"
    CLEANUP_FAILED = "cleanup_failed"
    CANCELLED = "cancelled"


class ChromeLifecyclePhase(StrEnum):
    READY = "ready"
    INITIALIZE = "initialize"
    ATTACH = "attach"
    CALL = "call"
    CLEANUP = "cleanup"


class ChromeSupervisorState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    STOPPING = "stopping"


@dataclass(frozen=True)
class ProcessCleanupReport:
    direct_child_reaped: bool
    process_group_reaped: bool
    surviving_pids: tuple[int, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "direct_child_reaped": self.direct_child_reaped,
            "process_group_reaped": self.process_group_reaped,
        }
        if self.surviving_pids:
            payload["surviving_pids"] = list(self.surviving_pids)
        return payload


_NO_PROCESS_CLEANUP = ProcessCleanupReport(direct_child_reaped=True, process_group_reaped=True)


class ChromeDevToolsError(RuntimeError):
    """Structured lifecycle failure; ``status`` separates blocked from failed."""

    def __init__(
        self,
        code: ChromeDevToolsErrorCode,
        phase: ChromeLifecyclePhase,
        message: str,
        *,
        status: str = "failed",
        cleanup: ProcessCleanupReport | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.status = status
        self.cleanup = cleanup

    def with_cleanup(self, cleanup: ProcessCleanupReport) -> ChromeDevToolsError:
        return ChromeDevToolsError(
            self.code, self.phase, str(self), status=self.status, cleanup=cleanup
        )


class ChromeControlFailure(RuntimeError):
    """Carries a complete ``chrome_control_result.v2`` failure payload."""

    def __init__(self, result: dict[str, Any]):
        error = result.get("error") or {}
        super().__init__(str(error.get("message") or "Chrome control failed."))
        self.result = result


_SECRET_PATTERN = re.compile(
    r"(?i)\b(authorization|cookie|set-cookie|password|passwd|secret|token|api[_-]?key)\b"
    r"\s*[:=]\s*(?:(?:bearer|basic|digest|negotiate)\s+)?\S+"
)


def redact_chrome_text(text: str, limit: int) -> str:
    """Bound and redact operator-facing evidence text."""

    redacted = _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    redacted = redacted.replace(str(Path.home()), "~")
    return redacted[-limit:]


@dataclass(frozen=True)
class ResolvedChromeDevToolsCommand:
    executable: str
    argv: tuple[str, ...]


def resolve_chrome_devtools_command(settings: Settings) -> ResolvedChromeDevToolsCommand:
    """Deterministically resolve the pinned MCP command without starting it."""

    for arg in (settings.chrome_devtools_command, *settings.chrome_devtools_command_args):
        if "@latest" in arg:
            raise ChromeDevToolsError(
                ChromeDevToolsErrorCode.COMMAND_NOT_RESOLVED,
                ChromeLifecyclePhase.INITIALIZE,
                "The live Chrome DevTools MCP path must pin an exact version; "
                f"refusing floating spec {arg!r}. Pin chrome_devtools_command_args "
                "(for example chrome-devtools-mcp@1.6.0) in settings/.env.",
                status="blocked",
            )
    executable = shutil.which(settings.chrome_devtools_command)
    if executable is None:
        raise ChromeDevToolsError(
            ChromeDevToolsErrorCode.COMMAND_NOT_RESOLVED,
            ChromeLifecyclePhase.INITIALIZE,
            f"Chrome DevTools MCP command {settings.chrome_devtools_command!r} "
            "was not found on PATH.",
            status="blocked",
        )
    return ResolvedChromeDevToolsCommand(
        executable=executable,
        argv=(executable, *settings.chrome_devtools_command_args),
    )


def preflight_chrome_devtools(
    settings: Settings, *, timeout_seconds: float = 60.0
) -> dict[str, Any]:
    """Resolve the pinned executable and version without starting Chrome."""

    resolved = resolve_chrome_devtools_command(settings)
    proc = subprocess.run(
        [*resolved.argv, "--version"],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if proc.returncode != 0:
        raise ChromeDevToolsError(
            ChromeDevToolsErrorCode.PROCESS_START_FAILED,
            ChromeLifecyclePhase.INITIALIZE,
            "Chrome DevTools MCP preflight failed: "
            + redact_chrome_text(
                proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}",
                settings.chrome_devtools_log_tail_chars,
            ),
        )
    return {
        "executable": resolved.executable,
        "argv": list(resolved.argv),
        "version": proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "",
    }


# Operator-facing attach targets for /chrome start. "tabs" inspects the real
# signed-in browser (requires the one-time chrome://inspect remote-debugging
# toggle), "isolated" launches a dedicated disposable instance that needs no
# browser-side setup, and "endpoint" attaches to an explicit debugging URL.
CHROME_ATTACH_TARGET_ALIASES: dict[str, str] = {
    "tabs": "auto_connect",
    "browser": "auto_connect",
    "auto": "auto_connect",
    "isolated": "launch",
    "headless": "launch",
    "launch": "launch",
    "endpoint": "browser_url",
    "url": "browser_url",
}


def attach_args_for(settings: Settings, mode: str) -> list[str]:
    """Arguments selecting how the MCP server reaches a browser."""

    if mode == "browser_url":
        if not settings.chrome_devtools_browser_url:
            raise ChromeDevToolsError(
                ChromeDevToolsErrorCode.BROWSER_ATTACH_FAILED,
                ChromeLifecyclePhase.ATTACH,
                "chrome_devtools_attach_mode=browser_url requires "
                "chrome_devtools_browser_url to be configured.",
                status="blocked",
            )
        return ["--browserUrl", settings.chrome_devtools_browser_url]
    if mode == "auto_connect":
        return ["--autoConnect"]
    return list(settings.chrome_devtools_launch_args)


# ---------------------------------------------------------------------------
# CLI-shaped command translation shared by service callers
# ---------------------------------------------------------------------------


def translate_chrome_cli_command(command_args: list[str]) -> tuple[str, dict[str, Any]]:
    if not command_args:
        raise ValueError("Chrome DevTools MCP tool command is empty.")
    name = command_args[0]
    args = command_args[1:]
    if name in {"list_pages", "list_console_messages", "list_network_requests"}:
        return name, _flags_to_kwargs(args)
    if name == "new_page":
        url, rest = _required_positional(args, name)
        return name, {"url": url, **_flags_to_kwargs(rest)}
    if name in {"select_page", "close_page"}:
        page_id, rest = _required_positional(args, name)
        return name, {"pageId": int(page_id), **_flags_to_kwargs(rest)}
    if name == "navigate_page":
        return name, _flags_to_kwargs(args)
    if name == "evaluate_script":
        function, rest = _required_positional(args, name)
        return name, {"function": function, **_flags_to_kwargs(rest)}
    if name in {"take_snapshot", "take_screenshot"}:
        return name, _flags_to_kwargs(args)
    raise ValueError(f"Unsupported Chrome DevTools MCP tool: {name}")


def _flags_to_kwargs(args: list[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("--"):
            raise ValueError(f"Unexpected Chrome DevTools argument: {token}")
        key = token[2:]
        if index + 1 >= len(args) or args[index + 1].startswith("--"):
            payload[key] = True
        else:
            payload[key] = _coerce_value(args[index + 1])
            index += 1
        index += 1
    return payload


def _coerce_value(value: str) -> Any:
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value


def _required_positional(args: list[str], name: str) -> tuple[str, list[str]]:
    if not args:
        raise ValueError(f"{name} requires a positional argument.")
    return args[0], args[1:]


def mcp_content_text(result: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            chunks.append(str(item.get("text") or ""))
    if chunks:
        return "\n".join(chunks)
    structured = result.get("structuredContent")
    return json.dumps(structured, sort_keys=True) if structured is not None else ""


# ---------------------------------------------------------------------------
# JSON-RPC client over a supervised process
# ---------------------------------------------------------------------------


class ChromeDevToolsMcpClient:
    """One JSON-RPC session per process generation with bounded buffers."""

    _TAIL_LINES = 80
    _TAIL_LINE_CHARS = 500

    def __init__(self, process: subprocess.Popen[str], settings: Settings):
        self._process = process
        self._settings = settings
        self._condition = threading.Condition()
        self._write_lock = threading.Lock()
        self._responses: dict[int, dict[str, Any]] = {}
        self._pending: set[int] = set()
        self._notifications: deque[str] = deque(maxlen=32)
        self._stdout_lines: deque[str] = deque(maxlen=self._TAIL_LINES)
        self._stderr_lines: deque[str] = deque(maxlen=self._TAIL_LINES)
        self._next_id = 1
        self._cancelled = False
        self._exited = False
        self._initialized = False
        self._server_info: dict[str, Any] = {}
        threading.Thread(target=self._drain_stdout, name="chrome-mcp-stdout", daemon=True).start()
        threading.Thread(target=self._drain_stderr, name="chrome-mcp-stderr", daemon=True).start()

    @property
    def child_exited(self) -> bool:
        return self._exited or self._process.poll() is not None

    @property
    def server_info(self) -> dict[str, Any]:
        return dict(self._server_info)

    def initialize(self) -> dict[str, Any]:
        if self._initialized:
            raise ChromeDevToolsError(
                ChromeDevToolsErrorCode.INITIALIZE_PROTOCOL_ERROR,
                ChromeLifecyclePhase.INITIALIZE,
                "Chrome DevTools MCP client is already initialized for this generation.",
            )
        result = self._request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "local-first-agent-os", "version": "0.1.0"},
            },
            timeout_seconds=self._settings.chrome_devtools_startup_timeout_seconds,
            phase=ChromeLifecyclePhase.INITIALIZE,
        )
        self._notify("notifications/initialized", {})
        self._initialized = True
        server_info = result.get("serverInfo")
        if isinstance(server_info, dict):
            self._server_info = server_info
        return result

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
        phase: ChromeLifecyclePhase = ChromeLifecyclePhase.CALL,
    ) -> dict[str, Any]:
        if not self._initialized:
            raise ChromeDevToolsError(
                ChromeDevToolsErrorCode.INITIALIZE_PROTOCOL_ERROR,
                phase,
                "Chrome DevTools MCP client is not initialized.",
            )
        return self._request(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout_seconds=(
                timeout_seconds or self._settings.chrome_devtools_call_timeout_seconds
            ),
            phase=phase,
        )

    def cancel(self) -> None:
        with self._condition:
            self._cancelled = True
            self._condition.notify_all()

    def read_redacted_stdout_tail(self) -> str:
        return redact_chrome_text(
            "\n".join(self._stdout_lines), self._settings.chrome_devtools_log_tail_chars
        )

    def read_redacted_stderr_tail(self) -> str:
        return redact_chrome_text(
            "\n".join(self._stderr_lines), self._settings.chrome_devtools_log_tail_chars
        )

    # -- internals ----------------------------------------------------------

    def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout_seconds: float,
        phase: ChromeLifecyclePhase,
    ) -> dict[str, Any]:
        with self._condition:
            if self._cancelled:
                raise self._build_cancelled_request_error(phase)
            if self._exited:
                raise self._build_child_process_exit_error(phase)
            request_id = self._next_id
            self._next_id += 1
            self._pending.add(request_id)
        try:
            self._send_mcp_message(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            response = self._wait_for_response(request_id, timeout_seconds, phase)
        finally:
            with self._condition:
                self._pending.discard(request_id)
                self._responses.pop(request_id, None)
        if "error" in response:
            code = (
                ChromeDevToolsErrorCode.INITIALIZE_PROTOCOL_ERROR
                if phase is ChromeLifecyclePhase.INITIALIZE
                else ChromeDevToolsErrorCode.TOOL_PROTOCOL_ERROR
            )
            raise ChromeDevToolsError(
                code,
                phase,
                redact_chrome_text(
                    str(response["error"]), self._settings.chrome_devtools_log_tail_chars
                ),
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise ChromeDevToolsError(
                ChromeDevToolsErrorCode.TOOL_PROTOCOL_ERROR
                if phase is not ChromeLifecyclePhase.INITIALIZE
                else ChromeDevToolsErrorCode.INITIALIZE_PROTOCOL_ERROR,
                phase,
                f"Chrome DevTools MCP returned a non-object result for {method}.",
            )
        return result

    def _wait_for_response(
        self, request_id: int, timeout_seconds: float, phase: ChromeLifecyclePhase
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        with self._condition:
            while True:
                if request_id in self._responses:
                    return self._responses[request_id]
                if self._cancelled:
                    raise self._build_cancelled_request_error(phase)
                if self._exited:
                    raise self._build_child_process_exit_error(phase)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise self._build_request_timeout_error(phase, timeout_seconds)
                try:
                    self._condition.wait(min(remaining, 0.25))
                except KeyboardInterrupt:
                    self._cancelled = True
                    raise self._build_cancelled_request_error(phase) from None

    def _build_request_timeout_error(
        self, phase: ChromeLifecyclePhase, timeout_seconds: float
    ) -> ChromeDevToolsError:
        if phase is ChromeLifecyclePhase.INITIALIZE:
            return ChromeDevToolsError(
                ChromeDevToolsErrorCode.INITIALIZE_TIMEOUT,
                phase,
                f"Chrome DevTools MCP did not answer initialize within {timeout_seconds:.0f}s.",
            )
        if phase is ChromeLifecyclePhase.ATTACH:
            return ChromeDevToolsError(
                ChromeDevToolsErrorCode.BROWSER_ATTACH_FAILED,
                phase,
                f"Chrome DevTools MCP could not attach to a browser within {timeout_seconds:.0f}s.",
                status="blocked",
            )
        return ChromeDevToolsError(
            ChromeDevToolsErrorCode.TOOL_TIMEOUT,
            phase,
            f"Chrome DevTools MCP tool call timed out after {timeout_seconds:.0f}s.",
        )

    def _build_cancelled_request_error(self, phase: ChromeLifecyclePhase) -> ChromeDevToolsError:
        return ChromeDevToolsError(
            ChromeDevToolsErrorCode.CANCELLED,
            phase,
            "Chrome DevTools MCP request was cancelled.",
        )

    def _build_child_process_exit_error(self, phase: ChromeLifecyclePhase) -> ChromeDevToolsError:
        returncode = self._process.poll()
        detail = self.read_redacted_stderr_tail()
        message = f"Chrome DevTools MCP process exited (returncode={returncode})."
        if detail:
            message += f" stderr tail: {detail[-500:]}"
        return ChromeDevToolsError(ChromeDevToolsErrorCode.CHILD_EXITED, phase, message)

    def _send_mcp_message(self, payload: dict[str, Any]) -> None:
        stdin = self._process.stdin
        if stdin is None:
            raise self._build_child_process_exit_error(ChromeLifecyclePhase.CALL)
        try:
            with self._write_lock:
                stdin.write(json.dumps(payload) + "\n")
                stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise self._build_child_process_exit_error(ChromeLifecyclePhase.CALL) from exc

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send_mcp_message({"jsonrpc": "2.0", "method": method, "params": params})

    def _drain_stdout(self) -> None:
        stdout = self._process.stdout
        if stdout is None:
            return
        try:
            for raw_line in stdout:
                line = raw_line.rstrip("\n")
                self._stdout_lines.append(line[: self._TAIL_LINE_CHARS])
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                with self._condition:
                    if isinstance(message_id, int) and message_id in self._pending:
                        self._responses[message_id] = message
                        self._condition.notify_all()
                    elif message_id is None and "method" in message:
                        self._notifications.append(str(message.get("method"))[:200])
        except (OSError, ValueError):
            pass
        finally:
            with self._condition:
                self._exited = True
                self._condition.notify_all()

    def _drain_stderr(self) -> None:
        stderr = self._process.stderr
        if stderr is None:
            return
        try:
            for raw_line in stderr:
                self._stderr_lines.append(raw_line.rstrip("\n")[: self._TAIL_LINE_CHARS])
        except (OSError, ValueError):
            pass


# ---------------------------------------------------------------------------
# Process supervisor
# ---------------------------------------------------------------------------


def read_pid_command_map() -> dict[int, tuple[int, str]]:
    """pid -> (ppid, command) from one bounded ps scan."""

    proc = subprocess.run(
        ["ps", "-axo", "pid,ppid,command"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    mapping: dict[int, tuple[int, str]] = {}
    for line in proc.stdout.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        mapping[pid] = (ppid, parts[2] if len(parts) > 2 else "")
    return mapping


def capture_descendant_process_snapshot(root_pid: int) -> dict[int, str]:
    """All live transitive descendants of ``root_pid`` as pid -> command."""

    mapping = read_pid_command_map()
    children: dict[int, list[int]] = {}
    for pid, (ppid, _command) in mapping.items():
        children.setdefault(ppid, []).append(pid)
    descendants: dict[int, str] = {}
    frontier = [root_pid]
    while frontier:
        current = frontier.pop()
        for child in children.get(current, []):
            if child not in descendants:
                descendants[child] = mapping[child][1]
                frontier.append(child)
    return descendants


def _signal_process_group(pgid: int | None, fallback_pid: int, sig: signal.Signals) -> None:
    if pgid is not None:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, sig)
            return
    with suppress(ProcessLookupError, PermissionError):
        os.kill(fallback_pid, sig)


def _process_group_alive(pgid: int | None) -> bool:
    if pgid is None:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class ChromeDevToolsProcessSupervisor:
    """Owns exactly one supervised Chrome DevTools MCP child process."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._lock = threading.RLock()
        self._state = ChromeSupervisorState.STOPPED
        self._generation = 0
        self._spawn_count = 0
        self._process: subprocess.Popen[str] | None = None
        self._pgid: int | None = None
        self._client: ChromeDevToolsMcpClient | None = None
        self._server_version: str | None = None
        self._started_at: float | None = None
        self._last_activity = time.monotonic()
        self._last_error: ChromeDevToolsError | None = None
        self._last_cleanup: ProcessCleanupReport | None = None
        self._last_stdout_tail = ""
        self._last_stderr_tail = ""
        self._atexit_registered = False
        self._idle_watchdog_started = False
        self._idle_watchdog_stop = threading.Event()

    @property
    def state(self) -> ChromeSupervisorState:
        return self._state

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def spawn_count(self) -> int:
        return self._spawn_count

    @property
    def last_error(self) -> ChromeDevToolsError | None:
        return self._last_error

    @property
    def last_cleanup(self) -> ProcessCleanupReport | None:
        return self._last_cleanup

    def record_client_activity(self) -> None:
        self._last_activity = time.monotonic()

    def get_healthy_mcp_client(self) -> ChromeDevToolsMcpClient | None:
        """The current generation's client when READY and alive, else None."""

        with self._lock:
            client = self._client
            if self._state is not ChromeSupervisorState.READY or client is None:
                return None
            if client.child_exited:
                error = ChromeDevToolsError(
                    ChromeDevToolsErrorCode.CHILD_EXITED,
                    ChromeLifecyclePhase.READY,
                    "Chrome DevTools MCP process exited outside of a request.",
                )
                self.fail_generation(error)
                return None
            self.record_client_activity()
            return client

    def start(self, *, attach_args: list[str]) -> ChromeDevToolsMcpClient:
        """Idempotent when healthy; a failed start cleans up before returning."""

        with self._lock:
            existing = self.get_healthy_mcp_client()
            if existing is not None:
                return existing
            self._state = ChromeSupervisorState.STARTING
            self._last_error = None
            try:
                client = self._launch_and_initialize_mcp_process_locked(attach_args)
            except ChromeDevToolsError as exc:
                report = self._cleanup_locked()
                self._state = ChromeSupervisorState.FAILED
                failure = exc if exc.cleanup is not None else exc.with_cleanup(report)
                self._last_error = failure
                raise failure from None
            self._state = ChromeSupervisorState.READY
            self._started_at = time.time()
            self.record_client_activity()
            self._register_atexit_locked()
            self._start_idle_watchdog_locked()
            logger.info(
                "chrome_devtools_mcp_started generation=%s pid=%s pgid=%s version=%s",
                self._generation,
                client and self._process and self._process.pid,
                self._pgid,
                self._server_version,
            )
            return client

    def _launch_and_initialize_mcp_process_locked(
        self, attach_args: list[str]
    ) -> ChromeDevToolsMcpClient:
        resolved = resolve_chrome_devtools_command(self._settings)
        argv = [
            *resolved.argv,
            *self._settings.chrome_devtools_start_args,
            *attach_args,
        ]
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            raise ChromeDevToolsError(
                ChromeDevToolsErrorCode.PROCESS_START_FAILED,
                ChromeLifecyclePhase.INITIALIZE,
                f"Failed to start Chrome DevTools MCP process: {exc}",
            ) from exc
        self._process = process
        self._spawn_count += 1
        try:
            self._pgid = os.getpgid(process.pid)
        except ProcessLookupError:
            self._pgid = process.pid
        client = ChromeDevToolsMcpClient(process, self._settings)
        self._client = client
        client.initialize()
        server_info = client.server_info
        self._server_version = str(server_info.get("version") or "") or None
        self._generation += 1
        return client

    def stop(self, *, reason: str = "stop") -> ProcessCleanupReport:
        """Idempotent full process-group termination with reap evidence."""

        with self._lock:
            if self._process is None:
                if self._state is not ChromeSupervisorState.FAILED:
                    self._state = ChromeSupervisorState.STOPPED
                return self._last_cleanup or _NO_PROCESS_CLEANUP
            self._state = ChromeSupervisorState.STOPPING
            report = self._cleanup_locked()
            if report.process_group_reaped:
                self._state = ChromeSupervisorState.STOPPED
                self._last_error = None
            else:
                self._state = ChromeSupervisorState.FAILED
                self._last_error = ChromeDevToolsError(
                    ChromeDevToolsErrorCode.CLEANUP_FAILED,
                    ChromeLifecyclePhase.CLEANUP,
                    f"Process descendants survived cleanup: {report.surviving_pids}.",
                    cleanup=report,
                )
            logger.info(
                "chrome_devtools_mcp_stopped reason=%s generation=%s reaped=%s",
                reason,
                self._generation,
                report.process_group_reaped,
            )
            return report

    def fail_generation(self, error: ChromeDevToolsError) -> ProcessCleanupReport:
        """Terminate the current generation after an unrecoverable failure."""

        with self._lock:
            report = self._cleanup_locked()
            self._state = ChromeSupervisorState.FAILED
            self._last_error = error if error.cleanup is not None else error.with_cleanup(report)
            return report

    def shutdown(self) -> ProcessCleanupReport:
        """Stop the watchdog and reap the supervised process family."""

        self._idle_watchdog_stop.set()
        return self.stop(reason="runtime_shutdown")

    def cancel_active(self) -> None:
        """Wake any waiter with a cancellation; safe from any thread."""

        client = self._client
        if client is not None:
            client.cancel()

    def status(self) -> dict[str, Any]:
        process = self._process
        last_error = self._last_error
        payload: dict[str, Any] = {
            "state": self._state.value,
            "generation": self._generation,
            "spawn_count": self._spawn_count,
            "pid": process.pid if process is not None else None,
            "process_group_id": self._pgid,
            "server_version": self._server_version,
            "uptime_seconds": (
                round(time.time() - self._started_at, 3) if self._started_at else None
            ),
            "idle_seconds": round(time.monotonic() - self._last_activity, 3),
        }
        if last_error is not None:
            payload["last_error"] = {
                "code": last_error.code.value,
                "phase": last_error.phase.value,
                "message": redact_chrome_text(
                    str(last_error), self._settings.chrome_devtools_log_tail_chars
                ),
            }
        if self._last_cleanup is not None:
            payload["last_cleanup"] = self._last_cleanup.to_payload()
        return payload

    def build_process_evidence(self) -> dict[str, Any]:
        client = self._client
        stdout_tail = (
            client.read_redacted_stdout_tail() if client is not None else self._last_stdout_tail
        )
        stderr_tail = (
            client.read_redacted_stderr_tail() if client is not None else self._last_stderr_tail
        )
        return {
            "command_version": self._server_version,
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
        }

    # -- internals ----------------------------------------------------------

    def _cleanup_locked(self) -> ProcessCleanupReport:
        process = self._process
        if process is None:
            return self._last_cleanup or _NO_PROCESS_CLEANUP
        client = self._client
        if client is not None:
            self._last_stdout_tail = client.read_redacted_stdout_tail()
            self._last_stderr_tail = client.read_redacted_stderr_tail()
            client.cancel()
        descendants = capture_descendant_process_snapshot(process.pid)
        if process.stdin is not None:
            with suppress(OSError, ValueError):
                process.stdin.close()
        _signal_process_group(self._pgid, process.pid, signal.SIGTERM)
        direct_child_reaped = self._wait_for_exit(
            process, self._settings.chrome_devtools_stop_timeout_seconds
        )
        if not direct_child_reaped:
            _signal_process_group(self._pgid, process.pid, signal.SIGKILL)
            direct_child_reaped = self._wait_for_exit(process, 2.0)
        survivors = self._list_surviving_descendants(descendants)
        deadline = time.monotonic() + self._settings.chrome_devtools_stop_timeout_seconds
        while survivors and time.monotonic() < deadline:
            time.sleep(0.2)
            survivors = self._list_surviving_descendants(descendants)
        for pid in survivors:
            with suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        if survivors:
            time.sleep(0.2)
            survivors = self._list_surviving_descendants(descendants)
        group_alive = _process_group_alive(self._pgid)
        report = ProcessCleanupReport(
            direct_child_reaped=direct_child_reaped,
            process_group_reaped=not survivors and not group_alive,
            surviving_pids=tuple(sorted(survivors)),
        )
        if survivors or group_alive:
            logger.warning(
                "chrome_devtools_mcp_cleanup_survivors pids=%s group_alive=%s",
                sorted(survivors),
                group_alive,
            )
        self._process = None
        self._pgid = None
        self._client = None
        self._started_at = None
        self._last_cleanup = report
        return report

    def _list_surviving_descendants(self, snapshot: dict[int, str]) -> list[int]:
        """Snapshot pids still alive with an unchanged command (guards pid reuse)."""

        current = read_pid_command_map()
        survivors: list[int] = []
        for pid, command in snapshot.items():
            entry = current.get(pid)
            if entry is not None and entry[1] == command:
                survivors.append(pid)
        return survivors

    def _wait_for_exit(self, process: subprocess.Popen[str], timeout_seconds: float) -> bool:
        try:
            process.wait(timeout=timeout_seconds)
            return True
        except subprocess.TimeoutExpired:
            return False

    def _register_atexit_locked(self) -> None:
        if self._atexit_registered:
            return
        atexit.register(self._terminate_supervisor_at_exit)
        self._atexit_registered = True

    def _terminate_supervisor_at_exit(self) -> None:
        try:
            self.stop(reason="interpreter_exit")
        except Exception:  # noqa: BLE001 - never propagate from atexit
            logger.exception("chrome_devtools_mcp_atexit_stop_failed")

    def _start_idle_watchdog_locked(self) -> None:
        idle_limit = self._settings.chrome_devtools_idle_shutdown_seconds
        if idle_limit <= 0 or self._idle_watchdog_started:
            return
        self._idle_watchdog_started = True
        threading.Thread(
            target=self._idle_watchdog_loop,
            name="chrome-mcp-idle-watchdog",
            daemon=True,
        ).start()

    def _idle_watchdog_loop(self) -> None:
        idle_limit = self._settings.chrome_devtools_idle_shutdown_seconds
        interval = max(0.05, min(idle_limit / 4, 30.0))
        while not self._idle_watchdog_stop.wait(interval):
            idle_limit = self._settings.chrome_devtools_idle_shutdown_seconds
            if idle_limit <= 0:
                continue
            if (
                self._state is ChromeSupervisorState.READY
                and time.monotonic() - self._last_activity > idle_limit
            ):
                logger.info("chrome_devtools_mcp_idle_shutdown")
                self.stop(reason="idle")


# ---------------------------------------------------------------------------
# Control service
# ---------------------------------------------------------------------------


class ChromeControlService:
    """Routes /chrome actions through one supervised MCP process generation."""

    def __init__(self, settings: Settings, *, mutation_allowed: Callable[[], bool]):
        self._settings = settings
        self._mutation_allowed = mutation_allowed
        self._supervisor = ChromeDevToolsProcessSupervisor(settings)
        self._action_lock = threading.RLock()
        # Sticky attach mode: seeded from settings, changed only by an explicit
        # /chrome start <target>, and reused by lazy restarts so the operator's
        # last explicit choice survives idle shutdowns.
        self._attach_mode: str = settings.chrome_devtools_attach_mode

    @property
    def attach_mode(self) -> str:
        return self._attach_mode

    @property
    def supervisor(self) -> ChromeDevToolsProcessSupervisor:
        return self._supervisor

    @property
    def action_lock(self) -> threading.RLock:
        return self._action_lock

    def close(self) -> None:
        self._supervisor.cancel_active()
        self._supervisor.shutdown()

    def ensure_mutation_allowed(self, action: str) -> None:
        if is_mutating_chrome_action(action) and not self._mutation_allowed():
            raise PermissionError(
                f"Chrome action {action!r} mutates browser state and writes are "
                "disabled for the chrome workspace."
            )

    def ensure_ready(self, *, explicit: bool) -> ChromeDevToolsMcpClient:
        with self._action_lock:
            client = self._supervisor.get_healthy_mcp_client()
            if client is not None:
                return client
            if not explicit:
                if self._supervisor.state is ChromeSupervisorState.FAILED:
                    last_error = self._supervisor.last_error
                    detail = (
                        f" Last failure: {last_error.code.value}: {last_error}"
                        if last_error is not None
                        else ""
                    )
                    raise ChromeDevToolsError(
                        last_error.code if last_error else ChromeDevToolsErrorCode.CHILD_EXITED,
                        ChromeLifecyclePhase.READY,
                        "The previous Chrome DevTools MCP generation failed and "
                        "automatic restarts are disabled. Run /chrome start after "
                        "addressing the failure." + detail,
                        status="blocked",
                    )
                if not self._settings.chrome_devtools_lazy_start:
                    raise ChromeDevToolsError(
                        ChromeDevToolsErrorCode.BROWSER_ATTACH_FAILED,
                        ChromeLifecyclePhase.READY,
                        "Chrome DevTools MCP is stopped and lazy start is disabled. "
                        "Run /chrome start first.",
                        status="blocked",
                    )
            return self._start_generation()

    def call_tool_command(
        self, command_args: list[str], *, allow_failure: bool = False
    ) -> dict[str, Any]:
        """Run one translated CLI-shaped command; output matches the legacy shape."""

        with self._action_lock:
            name, arguments = translate_chrome_cli_command(command_args)
            client = self._supervisor.get_healthy_mcp_client()
            if client is None:
                raise ChromeDevToolsError(
                    ChromeDevToolsErrorCode.CHILD_EXITED,
                    ChromeLifecyclePhase.CALL,
                    "No healthy Chrome DevTools MCP generation is available.",
                )
            try:
                result = client.call_tool(name, arguments)
            except ChromeDevToolsError as exc:
                if exc.code in {
                    ChromeDevToolsErrorCode.TOOL_TIMEOUT,
                    ChromeDevToolsErrorCode.CHILD_EXITED,
                }:
                    report = self._supervisor.fail_generation(exc)
                    raise exc.with_cleanup(report) from None
                raise
            stdout = mcp_content_text(result)
            output: dict[str, Any] = {
                "command": [
                    self._settings.chrome_devtools_command,
                    *self._settings.chrome_devtools_command_args,
                    "::",
                    name,
                    json.dumps(arguments, sort_keys=True),
                ],
                "returncode": 1 if result.get("isError") else 0,
                "stdout": stdout,
                "stderr": "",
            }
            structured = result.get("structuredContent")
            if structured is not None:
                output["json"] = structured
            if output["returncode"] != 0 and not allow_failure:
                raise RuntimeError(stdout or f"Chrome DevTools MCP tool {name} failed")
            return output

    # -- lifecycle actions ---------------------------------------------------

    def start_action(self, workflow_id: str, args: list[str]) -> dict[str, Any]:
        started = time.monotonic()
        requested_mode = self._parse_start_target(args)
        extra: dict[str, Any] = {
            "args": args,
            "attach_mode": requested_mode or self._attach_mode,
            "supervisor": None,
        }
        try:
            with self._action_lock:
                if requested_mode is not None and requested_mode != self._attach_mode:
                    # Switching targets replaces the running generation.
                    self._supervisor.stop(reason="attach_mode_change")
                    self._attach_mode = requested_mode
                self.ensure_ready(explicit=True)
        except ChromeDevToolsError as exc:
            raise ChromeControlFailure(
                self.build_failure(workflow_id, "start", started, exc, extra=extra)
            ) from exc
        extra["supervisor"] = self._supervisor.status()
        return self.build_success(workflow_id, "start", started, extra=extra)

    def _parse_start_target(self, args: list[str]) -> str | None:
        if not args:
            return None
        if len(args) > 1:
            raise ValueError(
                "/chrome start accepts at most one attach target: "
                "tabs (your running browser), isolated (dedicated headless "
                "instance), or endpoint (configured debugging URL)."
            )
        target = args[0].lower()
        mode = CHROME_ATTACH_TARGET_ALIASES.get(target)
        if mode is None:
            allowed = ", ".join(sorted(CHROME_ATTACH_TARGET_ALIASES))
            raise ValueError(f"/chrome start target must be one of: {allowed}; got {args[0]}")
        return mode

    def read_chrome_status(self, workflow_id: str, args: list[str]) -> dict[str, Any]:
        started = time.monotonic()
        extra = {
            "args": args,
            "attach_mode": self._attach_mode,
            "supervisor": self._supervisor.status(),
        }
        return self.build_success(workflow_id, "status", started, extra=extra, touch=False)

    def stop_action(self, workflow_id: str, args: list[str]) -> dict[str, Any]:
        started = time.monotonic()
        # Cancel before taking the action lock so a hung in-flight call wakes
        # with CANCELLED immediately instead of holding stop to its timeout.
        self._supervisor.cancel_active()
        with self._action_lock:
            report = self._supervisor.stop(reason="operator_stop")
        extra = {
            "args": args,
            "supervisor": self._supervisor.status(),
            "process_cleanup": report.to_payload(),
        }
        if not report.process_group_reaped:
            error = ChromeDevToolsError(
                ChromeDevToolsErrorCode.CLEANUP_FAILED,
                ChromeLifecyclePhase.CLEANUP,
                f"Process descendants survived stop: {report.surviving_pids}.",
                cleanup=report,
            )
            raise ChromeControlFailure(
                self.build_failure(workflow_id, "stop", started, error, extra=extra)
            )
        return self.build_success(workflow_id, "stop", started, extra=extra, touch=False)

    # -- result shaping ------------------------------------------------------

    def build_success(
        self,
        workflow_id: str,
        action: str,
        started_monotonic: float,
        *,
        extra: dict[str, Any] | None = None,
        touch: bool = True,
    ) -> dict[str, Any]:
        if touch:
            self._supervisor.record_client_activity()
        result = self._build_base_tool_result(workflow_id, action, started_monotonic)
        result["status"] = "completed"
        result["lifecycle_phase"] = ChromeLifecyclePhase.READY.value
        self._merge_extra(result, extra)
        result["v1"] = project_chrome_control_result_v1(result)
        return result

    def build_failure(
        self,
        workflow_id: str,
        action: str,
        started_monotonic: float,
        error: ChromeDevToolsError,
        *,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = self._build_base_tool_result(workflow_id, action, started_monotonic)
        result["status"] = error.status
        result["lifecycle_phase"] = error.phase.value
        result["error"] = {
            "code": error.code.value,
            "message": redact_chrome_text(
                str(error), self._settings.chrome_devtools_log_tail_chars
            ),
        }
        cleanup = error.cleanup or self._supervisor.last_cleanup
        if cleanup is not None:
            result["process_cleanup"] = cleanup.to_payload()
        self._merge_extra(result, extra)
        result["v1"] = project_chrome_control_result_v1(result)
        return result

    def _build_base_tool_result(
        self, workflow_id: str, action: str, started_monotonic: float
    ) -> dict[str, Any]:
        return {
            "schema_version": CHROME_CONTROL_RESULT_V2,
            "workflow_id": workflow_id,
            "action": action,
            "transport": "mcp",
            "process_generation": self._supervisor.generation,
            "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
            "evidence": self._supervisor.build_process_evidence(),
        }

    @staticmethod
    def _merge_extra(result: dict[str, Any], extra: dict[str, Any] | None) -> None:
        """Add legacy fields without allowing them to corrupt v2 invariants."""

        if not extra:
            return
        reserved = {
            "schema_version",
            "workflow_id",
            "action",
            "status",
            "transport",
            "process_generation",
            "duration_ms",
            "evidence",
            "lifecycle_phase",
            "error",
            "v1",
        }
        result.update({key: value for key, value in extra.items() if key not in reserved})

    # -- internals -----------------------------------------------------------

    def _start_generation(self) -> ChromeDevToolsMcpClient:
        self._validate_browser_url_preflight()
        client = self._supervisor.start(
            attach_args=attach_args_for(self._settings, self._attach_mode)
        )
        try:
            probe = client.call_tool(
                "list_pages",
                {},
                timeout_seconds=self._settings.chrome_devtools_attach_timeout_seconds,
                phase=ChromeLifecyclePhase.ATTACH,
            )
            if probe.get("isError"):
                # The server reports attach failures as tool-level errors
                # (for example a missing DevToolsActivePort handshake).
                raise ChromeDevToolsError(
                    ChromeDevToolsErrorCode.BROWSER_ATTACH_FAILED,
                    ChromeLifecyclePhase.ATTACH,
                    redact_chrome_text(
                        mcp_content_text(probe),
                        self._settings.chrome_devtools_log_tail_chars,
                    ),
                    status="blocked",
                )
        except ChromeDevToolsError as exc:
            annotated = self._add_attach_failure_guidance(exc)
            report = self._supervisor.fail_generation(annotated)
            raise annotated.with_cleanup(report) from None
        return client

    def _validate_browser_url_preflight(self) -> None:
        if self._attach_mode != "browser_url":
            return
        browser_url = self._settings.chrome_devtools_browser_url
        if not browser_url:
            raise ChromeDevToolsError(
                ChromeDevToolsErrorCode.BROWSER_ATTACH_FAILED,
                ChromeLifecyclePhase.ATTACH,
                "chrome_devtools_attach_mode=browser_url requires "
                "chrome_devtools_browser_url to be configured.",
                status="blocked",
            )
        probe_url = browser_url.rstrip("/") + "/json/version"
        try:
            response = httpx.get(probe_url, timeout=3.0)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ChromeDevToolsError(
                ChromeDevToolsErrorCode.BROWSER_ATTACH_FAILED,
                ChromeLifecyclePhase.ATTACH,
                f"Chrome debugging endpoint {browser_url} is not reachable "
                f"({exc.__class__.__name__}). Start Chrome with "
                "--remote-debugging-port or fix chrome_devtools_browser_url.",
                status="blocked",
            ) from exc

    def _add_attach_failure_guidance(self, error: ChromeDevToolsError) -> ChromeDevToolsError:
        if error.code is not ChromeDevToolsErrorCode.BROWSER_ATTACH_FAILED:
            return error
        mode = self._attach_mode
        if mode == "auto_connect":
            hint = (
                " No eligible running Chrome accepted the auto-connect handshake. "
                "Flip the one-time remote-debugging toggle for your Chrome profile "
                "(chrome://inspect/#remote-debugging, Chrome 144+; it persists "
                "across restarts), then retry /chrome start tabs. For a browser "
                "that needs no setup, use /chrome start isolated."
            )
        elif mode == "browser_url":
            hint = (
                " The configured debugging endpoint did not accept the attach. "
                "Verify chrome_devtools_browser_url."
            )
        else:
            hint = " The dedicated browser launch did not become ready."
        return ChromeDevToolsError(
            error.code,
            error.phase,
            f"{error}{hint}",
            status=error.status,
            cleanup=error.cleanup,
        )


def project_chrome_control_result_v1(result: dict[str, Any]) -> dict[str, Any]:
    """Compatibility projection for consumers expecting v1 results."""

    projection: dict[str, Any] = {
        "schema_version": CHROME_CONTROL_RESULT_V1,
        "workflow_id": result.get("workflow_id"),
        "action": result.get("action"),
    }
    for key in _V1_PAYLOAD_KEYS:
        if key in result:
            projection[key] = result[key]
    projection.setdefault("args", [])
    projection.setdefault("invocations", [])
    error = result.get("error")
    if error:
        projection["status"] = "failed"
        projection["error"] = str(error.get("message") or "")
    else:
        projection["status"] = "completed"
    return projection
