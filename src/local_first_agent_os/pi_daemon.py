# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import fcntl
import json
import logging
import os
import shlex
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from threading import RLock, Thread
from typing import Any
from urllib.parse import urlparse

import httpx
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .constants import (
    DAEMON_STATE_DIR_ENV_VAR,
    DEFAULT_DAEMON_STATE_DIR,
    DEFAULT_PI_STREAM_HEARTBEAT_SECONDS,
    DEFAULT_PI_STREAM_IDLE_TIMEOUT_SECONDS,
)
from .daemon_stdio import detach_inherited_stdin
from .directives import DISPATCH_ALIAS
from .progress_events import progress_event_sink
from .runtime_source import runtime_revision
from .settings import Settings, get_settings

logger = logging.getLogger(__name__)

PiQueryItem = str | dict[str, Any]
PiQueryRunner = Callable[..., Iterable[PiQueryItem]]
PiResultRenderer = Callable[[dict[str, Any]], str]
PiResultPayloadResolver = Callable[[dict[str, Any]], dict[str, Any] | None]


def _query_may_bypass_run_lock(payload: dict[str, Any]) -> bool:
    """Keep control-plane inspection available during long agent dispatches."""

    text = str(payload.get("text") or "").strip()
    try:
        tokens = shlex.split(text)
    except ValueError:
        return False
    if not tokens:
        return False
    if "&&" in tokens:
        return False
    if tokens[0] == "/ledger":
        inspection_tail = tokens[1:]
    elif tokens[:2] == ["/read", "/ledger"]:
        inspection_tail = tokens[2:]
    elif tokens[0] == "/review-merge" and len(tokens) <= 2:
        inspection_tail = tokens[1:]
    else:
        return False
    return not any(token.startswith("/") for token in inspection_tail)


class PiDaemonUnavailable(RuntimeError):
    """Raised when the resident Pi orchestrator is not reachable."""


class PiDaemonClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.base_url = self.settings.pi_daemon_base_url.rstrip("/")

    def health(self) -> bool:
        try:
            response = httpx.get(f"{self.base_url}/health", timeout=0.5)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    def stream_query(
        self,
        *,
        text: str,
        workspace_id: str,
        session_id: str,
        context: str | None,
        max_window_tokens: int | None,
        streaming: bool,
    ) -> Iterator[dict[str, Any]]:
        payload = {
            "text": text,
            "workspace_id": workspace_id,
            "session_id": session_id,
            "context": context,
            "max_window_tokens": max_window_tokens,
            "streaming": streaming,
        }
        idle_timeout = float(
            getattr(
                self.settings,
                "pi_stream_idle_timeout_seconds",
                DEFAULT_PI_STREAM_IDLE_TIMEOUT_SECONDS,
            )
        )
        timeout = httpx.Timeout(connect=5.0, read=idle_timeout, write=5.0, pool=5.0)
        try:
            with (
                httpx.Client(timeout=timeout) as client,
                client.stream("POST", f"{self.base_url}/query", json=payload) as response,
            ):
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    if isinstance(event, dict):
                        yield event
        except httpx.ReadTimeout as exc:
            raise PiDaemonUnavailable(
                f"Pi daemon at {self.base_url} produced no event for "
                f"{idle_timeout:g} seconds. The command may still be recoverable "
                "from the ledger; inspect it before retrying."
            ) from exc
        except httpx.HTTPError as exc:
            raise PiDaemonUnavailable(f"Pi daemon is not reachable at {self.base_url}.") from exc


def ensure_pi_daemon(settings: Settings | None = None, *, wait_seconds: float = 20) -> None:
    settings = settings or get_settings()
    client = PiDaemonClient(settings)
    if client.health():
        return
    if not settings.pi_daemon_autostart:
        raise PiDaemonUnavailable(f"Pi daemon is not reachable at {client.base_url}.")

    raw_state_dir = os.environ.get(DAEMON_STATE_DIR_ENV_VAR, DEFAULT_DAEMON_STATE_DIR)
    state_dir = Path(raw_state_dir).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)
    pid_path = state_dir / "pi-daemon.pid"
    log_path = state_dir / "pi-daemon.log"

    # Serialize the start decision across processes: two concurrent `pi`
    # invocations must not both spawn a daemon, or the loser crashes on the port
    # bind and clobbers the pid file with a dead PID. The lock is released before
    # the (potentially long) readiness wait below.
    lock_path = state_dir / "pi-daemon.spawn.lock"
    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        if not client.health():
            if pid_path.exists():
                try:
                    os.kill(int(pid_path.read_text(encoding="utf-8").strip()), 0)
                except (OSError, ValueError):
                    pid_path.unlink(missing_ok=True)
            if not pid_path.exists():
                repo_root = Path(__file__).resolve().parents[2]
                with log_path.open("ab") as log:
                    process = subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            (
                                "from local_first_agent_os.pi_daemon import run_pi_daemon; "
                                "run_pi_daemon()"
                            ),
                        ],
                        cwd=repo_root,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                pid_path.write_text(str(process.pid), encoding="utf-8")

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if client.health():
            return
        time.sleep(0.25)
    raise PiDaemonUnavailable(f"Pi daemon is not available at {client.base_url}. Check {log_path}.")


class _PiHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def run_pi_daemon() -> None:
    from .pi_channel import (
        directive_result_payload,
        render_terminal_result,
        run_terminal_query,
    )
    from .runtime import get_runtime

    detach_inherited_stdin()
    runtime = get_runtime()
    # Bind the address the client actually targets. pi_daemon_base_url honors
    # the LOCAL_AGENT_PI_DAEMON_URL override, so deriving host/port from it keeps
    # an autostarted daemon and its client in agreement even when the URL pins a
    # non-default port.
    parsed = urlparse(runtime.settings.pi_daemon_base_url)
    host = parsed.hostname or runtime.settings.pi_daemon_host
    port = parsed.port or runtime.settings.pi_daemon_port
    handler = build_pi_daemon_http_handler(
        run_terminal_query,
        render_terminal_result,
        result_payload_resolver=directive_result_payload,
        health_payload={
            "coordination_backend": runtime.settings.coordination_backend,
            "runtime_revision": runtime_revision(),
        },
        stream_heartbeat_seconds=runtime.settings.pi_stream_heartbeat_seconds,
    )
    server = _PiHTTPServer((host, port), handler)

    # The runtime scripts stop this daemon with SIGTERM, whose default handler
    # skips atexit entirely. Convert it to SystemExit so the finally block
    # reaps supervised tool processes (Chrome DevTools MCP) before exit.
    def _request_pi_daemon_shutdown(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _request_pi_daemon_shutdown)
    logger.warning("pi_daemon_started host=%s port=%s", host, port)
    try:
        server.serve_forever()
    finally:
        logger.warning("pi_daemon_shutdown_cleanup")
        runtime.close()


def build_pi_daemon_http_handler(
    query_runner: PiQueryRunner,
    result_renderer: PiResultRenderer,
    *,
    result_payload_resolver: PiResultPayloadResolver | None = None,
    health_payload: dict[str, Any] | None = None,
    stream_heartbeat_seconds: float = DEFAULT_PI_STREAM_HEARTBEAT_SECONDS,
) -> type[BaseHTTPRequestHandler]:
    run_lock = RLock()
    payload = {"status": "ok", **(health_payload or {})}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                self._write_json(payload)
                return
            if self.path == "/metrics":
                data = generate_latest()
                self.send_response(200)
                self.send_header("content-type", CONTENT_TYPE_LATEST)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            self.send_error(404)

        def do_POST(self) -> None:
            if self.path != "/query":
                self.send_error(404)
                return
            try:
                payload = self._read_json()
            except ValueError:
                # Bad content-length or non-JSON body. Reply with a clean 400
                # before any streaming headers go out, rather than raising out
                # of the handler and leaving the client with a broken socket.
                self.send_error(400, "invalid request body")
                return
            self.send_response(200)
            self.send_header("content-type", "application/x-ndjson")
            self.send_header("cache-control", "no-cache")
            self.end_headers()
            try:
                text = str(payload.get("text") or "").strip()
                if text == DISPATCH_ALIAS:
                    self._write_event(
                        {
                            "type": "status",
                            "message": (
                                "dispatch accepted by the Pi daemon; waiting to claim "
                                "one PENDING intent and run its durable dispatch lifecycle"
                            ),
                        }
                    )
                events: Queue[dict[str, Any] | None] = Queue()

                def produce_events() -> None:
                    request_lock = (
                        nullcontext() if _query_may_bypass_run_lock(payload) else run_lock
                    )
                    try:
                        with request_lock, progress_event_sink(events.put):
                            for event in _query_events(
                                payload,
                                query_runner,
                                result_renderer,
                                result_payload_resolver,
                            ):
                                events.put(event)
                    finally:
                        events.put(None)

                Thread(target=produce_events, daemon=True).start()
                started = time.monotonic()
                while True:
                    try:
                        event = events.get(timeout=stream_heartbeat_seconds)
                    except Empty:
                        self._write_event(
                            {
                                "type": "status",
                                "message": (
                                    f"Pi daemon command still running "
                                    f"({int(time.monotonic() - started)}s elapsed)"
                                ),
                            }
                        )
                        continue
                    if event is None:
                        break
                    self._write_event(event)
                self._write_event({"type": "done"})
            except BrokenPipeError:
                return
            except Exception as exc:
                logger.exception("pi_daemon_query_failed")
                try:
                    self._write_event({"type": "error", "error": str(exc)})
                except BrokenPipeError:
                    return

        def log_message(self, _format: str, *args: Any) -> None:
            return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("content-length") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length).decode("utf-8")
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else {}

        def _write_json(self, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, sort_keys=True).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _write_event(self, event: dict[str, Any]) -> None:
            data = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
            self.wfile.write(data)
            self.wfile.flush()

    return Handler


def _query_events(
    payload: dict[str, Any],
    query_runner: PiQueryRunner,
    result_renderer: PiResultRenderer,
    result_payload_resolver: PiResultPayloadResolver | None = None,
) -> Iterator[dict[str, Any]]:
    text = str(payload.get("text") or "").strip()
    if not text:
        yield {"type": "error", "error": "No Pi command text provided."}
        return
    try:
        items = query_runner(
            text,
            workspace_id=str(payload.get("workspace_id") or "general"),
            context=_optional_str(payload.get("context")),
            max_window_tokens=_optional_int(payload.get("max_window_tokens")),
            shell_session_id=str(payload.get("session_id") or "pi-daemon"),
            streaming=bool(payload.get("streaming")),
        )
        for item in items:
            if isinstance(item, str):
                yield {"type": "delta", "text": item}
                continue
            rendered = _render_result(item, result_renderer)
            event = {"type": "result", "result": item, "rendered": rendered}
            if result_payload_resolver is not None:
                directive_payload = result_payload_resolver(item)
                if directive_payload is not None:
                    event["directive_payload"] = directive_payload
            yield event
    except Exception as exc:  # noqa: BLE001 - surfaced to the client as an error event
        logger.exception("pi_daemon_query_failed")
        yield {"type": "error", "error": str(exc), "exit_code": _exit_code_for(exc)}


def _exit_code_for(exc: BaseException) -> int:
    """Map a query exception to the exit code the direct CLI path would use."""
    from .model_manager import ModelNotLoadedError
    from .session_memory import SessionDaemonUnavailable

    if isinstance(exc, ModelNotLoadedError):
        return 2
    if isinstance(exc, SessionDaemonUnavailable):
        return 3
    return 1


def _render_result(result: dict[str, Any], result_renderer: PiResultRenderer) -> str:
    try:
        return result_renderer(result)
    except Exception:
        status = result.get("status", "completed")
        return status if isinstance(status, str) else str(status)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None


def _optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    return int(value)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        run_pi_daemon()
    except KeyboardInterrupt:
        return
