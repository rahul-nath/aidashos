# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.exc import OperationalError

from .constants import DAEMON_STATE_DIR_ENV_VAR, DEFAULT_DAEMON_STATE_DIR
from .contracts import ArtifactRole
from .daemon_stdio import detach_inherited_stdin
from .ids import build_session_item_id
from .runtime import AppRuntime, get_runtime
from .session_durability import persist_session_item
from .settings import Settings, get_settings
from .utils import TURN_ASSISTANT, TURN_USER, estimate_tokens

logger = logging.getLogger(__name__)

SCHEMA_VERSION_SESSION_CONTEXT = "session_context.v1"


class SessionDaemonUnavailable(RuntimeError):
    """Raised when session memory is requested but the local daemon is unavailable."""


@dataclass
class SessionContextState:
    session_id: str
    model_id: str
    context: str = ""
    active_context_artifact_id: str | None = None
    compacted_summary_artifact_id: str | None = None
    snapshot_item_id: str | None = None
    max_window_tokens: int | None = None
    export_path: str | None = None
    dirty: bool = False

    @property
    def token_count(self) -> int:
        return estimate_tokens(self.context)


class SessionMemoryStore:
    def __init__(self, runtime: AppRuntime):
        self.runtime = runtime
        self._contexts: dict[tuple[str, str], SessionContextState] = {}

    def get_context(self, session_id: str, model_id: str) -> SessionContextState:
        key = (session_id, model_id)
        if key in self._contexts:
            return self._contexts[key]
        row = self.runtime.repository.get_session_context(session_id, model_id)
        state = SessionContextState(session_id=session_id, model_id=model_id)
        if row is not None:
            artifact_id = row.get("active_context_artifact_id")
            if isinstance(artifact_id, str) and artifact_id:
                try:
                    state.context = self.runtime.artifact_store.read_text(artifact_id)
                    state.active_context_artifact_id = artifact_id
                except Exception:
                    logger.exception("session_context_load_failed")
            compacted_id = row.get("compacted_summary_artifact_id")
            state.compacted_summary_artifact_id = (
                compacted_id if isinstance(compacted_id, str) else None
            )
            snapshot_item_id = row.get("snapshot_item_id")
            state.snapshot_item_id = snapshot_item_id if isinstance(snapshot_item_id, str) else None
            max_window = row.get("max_window_tokens")
            state.max_window_tokens = int(max_window) if max_window else None
            export_path = row.get("export_path")
            state.export_path = export_path if isinstance(export_path, str) else None
        items = self.runtime.repository.list_session_items(
            session_id,
            model_id,
            after_item_id=state.snapshot_item_id,
        )
        state.context = _join_context(state.context, _format_session_items(items))
        for item in reversed(items):
            metadata = item.get("metadata")
            if isinstance(metadata, dict) and metadata.get("max_window_tokens"):
                state.max_window_tokens = int(metadata["max_window_tokens"])
                break
        self._contexts[key] = state
        return state

    def begin_turn(
        self,
        *,
        session_id: str,
        model_id: str,
        user_text: str,
        turn_id: str | None = None,
        created_at: datetime | None = None,
        model_selector: str | None = None,
        max_window_tokens: int | None = None,
        source_workspace_id: str | None = None,
        retrieved_artifact_ids: list[str] | None = None,
    ) -> dict[str, str]:
        state = self.get_context(session_id, model_id)
        turn_id = turn_id or f"turn:{uuid.uuid4().hex}"
        created_at = created_at or datetime.now(UTC)
        metadata: dict[str, Any] = {
            "retrieved_artifact_ids": retrieved_artifact_ids or [],
        }
        if model_selector:
            metadata["model_selector"] = model_selector
        if max_window_tokens is not None:
            metadata["max_window_tokens"] = max_window_tokens
        if source_workspace_id:
            metadata["source_workspace_id"] = source_workspace_id
        item = {
            "item_id": build_session_item_id(turn_id, 0),
            "turn_id": turn_id,
            "session_id": session_id,
            "model_id": model_id,
            "ordinal": 0,
            "item_type": "message",
            "role": "user",
            "content": user_text.strip(),
            "metadata": metadata,
            "created_at": created_at.isoformat(),
        }
        result = persist_session_item(self.runtime, item)
        if result["inserted"]:
            state.context = _join_context(state.context, _format_session_items([item]))
            state.max_window_tokens = max_window_tokens or state.max_window_tokens
            self._refresh_export(state)
        return {"turn_id": turn_id, "created_at": created_at.isoformat()}

    def complete_turn(
        self,
        *,
        session_id: str,
        model_id: str,
        turn_id: str,
        created_at: datetime,
        answer: str,
    ) -> SessionContextState:
        state = self.get_context(session_id, model_id)
        item = {
            "item_id": build_session_item_id(turn_id, 1),
            "turn_id": turn_id,
            "session_id": session_id,
            "model_id": model_id,
            "ordinal": 1,
            "item_type": "message",
            "role": "assistant",
            "content": answer.strip(),
            "metadata": {},
            "created_at": created_at.isoformat(),
        }
        result = persist_session_item(self.runtime, item)
        if result["inserted"]:
            state.context = _join_context(state.context, _format_session_items([item]))
            self._refresh_export(state)
        return state

    def append_turn(
        self,
        *,
        session_id: str,
        model_id: str,
        user_text: str,
        answer: str,
        model_selector: str | None = None,
        max_window_tokens: int | None = None,
        source_workspace_id: str | None = None,
        retrieved_artifact_ids: list[str] | None = None,
        turn_id: str | None = None,
    ) -> SessionContextState:
        handle = self.begin_turn(
            session_id=session_id,
            model_id=model_id,
            user_text=user_text,
            turn_id=turn_id,
            model_selector=model_selector,
            max_window_tokens=max_window_tokens,
            source_workspace_id=source_workspace_id,
            retrieved_artifact_ids=retrieved_artifact_ids,
        )
        return self.complete_turn(
            session_id=session_id,
            model_id=model_id,
            turn_id=handle["turn_id"],
            created_at=datetime.fromisoformat(handle["created_at"]),
            answer=answer,
        )

    def set_context(
        self,
        *,
        session_id: str,
        model_id: str,
        context: str,
        compacted_summary_artifact_id: str | None = None,
        max_window_tokens: int | None = None,
    ) -> SessionContextState:
        state = self.get_context(session_id, model_id)
        state.context = context
        if compacted_summary_artifact_id is not None:
            state.compacted_summary_artifact_id = compacted_summary_artifact_id
        state.max_window_tokens = max_window_tokens or state.max_window_tokens
        state.snapshot_item_id = self.runtime.repository.latest_session_item_id(
            session_id,
            model_id,
        )
        state.dirty = True
        self.flush(session_id=session_id)
        return state

    def flush(self, session_id: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for key, state in list(self._contexts.items()):
            if session_id is not None and key[0] != session_id:
                continue
            if not state.dirty or not state.context.strip():
                continue
            artifact = self.runtime.artifact_store.write_text(
                role=ArtifactRole.SESSION_CONTEXT.value,
                text=state.context,
                workflow_id=None,
                schema_version=SCHEMA_VERSION_SESSION_CONTEXT,
                mime_type="text/markdown",
            )
            state.active_context_artifact_id = artifact.artifact_id
            export_path = self._write_export_file(state)
            state.export_path = str(export_path)
            self.runtime.repository.upsert_session_context(
                session_id=state.session_id,
                model_id=state.model_id,
                active_context_artifact_id=state.active_context_artifact_id,
                compacted_summary_artifact_id=state.compacted_summary_artifact_id,
                snapshot_item_id=state.snapshot_item_id,
                token_count=state.token_count,
                max_window_tokens=state.max_window_tokens,
                export_path=state.export_path,
            )
            state.dirty = False
            rows.append(
                {
                    "session_id": state.session_id,
                    "model_id": state.model_id,
                    "active_context_artifact_id": state.active_context_artifact_id,
                    "snapshot_item_id": state.snapshot_item_id,
                    "token_count": state.token_count,
                    "max_window_tokens": state.max_window_tokens,
                    "export_path": state.export_path,
                }
            )
        return rows

    def _write_export_file(self, state: SessionContextState) -> Path:
        root = self.runtime.settings.session_context_export_dir.expanduser()
        session_dir = root / _safe_path_part(state.session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        path = session_dir / f"{_safe_path_part(state.model_id)}.md"
        header = (
            f"session_id: {state.session_id}\n"
            f"model_id: {state.model_id}\n"
            f"token_count: {state.token_count}\n"
            f"max_window_tokens: {state.max_window_tokens or ''}\n\n"
        )
        path.write_text(header + state.context, encoding="utf-8")
        return path

    def _refresh_export(self, state: SessionContextState) -> None:
        try:
            state.export_path = str(self._write_export_file(state))
        except OSError:
            logger.exception(
                "session_context_export_failed",
                extra={"session_id": state.session_id, "model_id": state.model_id},
            )


class SessionDaemonClient:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.base_url = (
            f"http://{self.settings.session_daemon_host}:{self.settings.session_daemon_port}"
        )

    def health(self) -> bool:
        try:
            response = httpx.get(f"{self.base_url}/health", timeout=1)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    def get_context(self, *, session_id: str, model_id: str) -> str:
        try:
            response = httpx.post(
                f"{self.base_url}/context/get",
                json={"session_id": session_id, "model_id": model_id},
                timeout=2,
            )
            response.raise_for_status()
            payload = response.json()
            value = payload.get("context")
            return value if isinstance(value, str) else ""
        except httpx.HTTPError as exc:
            raise SessionDaemonUnavailable(
                f"Failed to read session context for {session_id}/{model_id} from {self.base_url}."
            ) from exc

    def append_turn(
        self,
        *,
        session_id: str,
        model_id: str,
        user_text: str,
        answer: str,
        model_selector: str | None,
        max_window_tokens: int | None,
        source_workspace_id: str | None,
        retrieved_artifact_ids: list[str] | None = None,
    ) -> None:
        handle = self.begin_turn(
            session_id=session_id,
            model_id=model_id,
            user_text=user_text,
            model_selector=model_selector,
            max_window_tokens=max_window_tokens,
            source_workspace_id=source_workspace_id,
            retrieved_artifact_ids=retrieved_artifact_ids,
        )
        self.complete_turn(
            session_id=session_id,
            model_id=model_id,
            turn_id=handle["turn_id"],
            created_at=handle["created_at"],
            answer=answer,
        )

    def begin_turn(
        self,
        *,
        session_id: str,
        model_id: str,
        user_text: str,
        model_selector: str | None,
        max_window_tokens: int | None,
        source_workspace_id: str | None,
        retrieved_artifact_ids: list[str] | None = None,
    ) -> dict[str, str]:
        try:
            response = httpx.post(
                f"{self.base_url}/context/begin",
                json={
                    "session_id": session_id,
                    "model_id": model_id,
                    "user_text": user_text,
                    "model_selector": model_selector,
                    "max_window_tokens": max_window_tokens,
                    "source_workspace_id": source_workspace_id,
                    "retrieved_artifact_ids": retrieved_artifact_ids or [],
                },
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            return {
                "turn_id": str(payload["turn_id"]),
                "created_at": str(payload["created_at"]),
            }
        except httpx.HTTPError as exc:
            raise SessionDaemonUnavailable(
                f"Failed to begin session turn for {session_id}/{model_id} to {self.base_url}."
            ) from exc

    def complete_turn(
        self,
        *,
        session_id: str,
        model_id: str,
        turn_id: str,
        created_at: str,
        answer: str,
    ) -> None:
        try:
            response = httpx.post(
                f"{self.base_url}/context/complete",
                json={
                    "session_id": session_id,
                    "model_id": model_id,
                    "turn_id": turn_id,
                    "created_at": created_at,
                    "answer": answer,
                },
                timeout=10,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SessionDaemonUnavailable(
                f"Failed to complete session turn for {session_id}/{model_id} on {self.base_url}."
            ) from exc

    def set_context(
        self,
        *,
        session_id: str,
        model_id: str,
        context: str,
        compacted_summary_artifact_id: str | None = None,
        max_window_tokens: int | None = None,
    ) -> None:
        try:
            response = httpx.post(
                f"{self.base_url}/context/set",
                json={
                    "session_id": session_id,
                    "model_id": model_id,
                    "context": context,
                    "compacted_summary_artifact_id": compacted_summary_artifact_id,
                    "max_window_tokens": max_window_tokens,
                },
                timeout=2,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SessionDaemonUnavailable(
                f"Failed to set session context for {session_id}/{model_id} on {self.base_url}."
            ) from exc

    def flush(self, session_id: str | None = None) -> list[dict[str, Any]]:
        try:
            response = httpx.post(
                f"{self.base_url}/context/flush",
                json={"session_id": session_id},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("flushed")
            return rows if isinstance(rows, list) else []
        except httpx.HTTPError as exc:
            raise SessionDaemonUnavailable(
                f"Failed to flush session context from {self.base_url}."
            ) from exc


def ensure_session_daemon(settings: Settings | None = None, *, wait_seconds: float = 10) -> None:
    settings = settings or get_settings()
    client = SessionDaemonClient(settings)
    if client.health():
        return

    raw_state_dir = os.environ.get(DAEMON_STATE_DIR_ENV_VAR, DEFAULT_DAEMON_STATE_DIR)
    state_dir = Path(raw_state_dir).expanduser()
    state_dir.mkdir(parents=True, exist_ok=True)
    pid_path = state_dir / "session-daemon.pid"
    log_path = state_dir / "session-daemon.log"
    if pid_path.exists():
        try:
            os.kill(int(pid_path.read_text(encoding="utf-8").strip()), 0)
        except (OSError, ValueError):
            pid_path.unlink(missing_ok=True)

    if not pid_path.exists():
        repo_root = Path(__file__).resolve().parents[2]
        log = log_path.open("ab")
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from local_first_agent_os.session_memory import run_session_daemon; "
                "run_session_daemon()",
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
    raise SessionDaemonUnavailable(
        f"Session memory daemon is not available at {client.base_url}. Check {log_path}."
    )


def _wait_for_session_runtime(
    runtime_factory: Callable[[], AppRuntime],
    sleep: Callable[[float], None],
    *,
    initial_retry_seconds: float = 2.0,
    max_retry_seconds: float = 60.0,
) -> AppRuntime:
    """Wait for Postgres without emitting one traceback per launchd restart."""

    retry_seconds = initial_retry_seconds
    outage_logged = False
    while True:
        try:
            runtime = runtime_factory()
        except OperationalError:
            if not outage_logged:
                logger.error(
                    "session_daemon_database_unavailable; waiting for database; "
                    "further retries suppressed until recovery"
                )
                outage_logged = True
            sleep(retry_seconds)
            retry_seconds = min(max_retry_seconds, retry_seconds * 2)
            continue
        if outage_logged:
            logger.warning("session_daemon_database_recovered")
        return runtime


def run_session_daemon() -> None:
    detach_inherited_stdin()
    # get_runtime initializes the schema.  Keep this process resident while
    # Postgres is down so launchd does not create an unbounded traceback loop.
    runtime = _wait_for_session_runtime(get_runtime, time.sleep)
    store = SessionMemoryStore(runtime)
    server = ThreadingHTTPServer(
        (runtime.settings.session_daemon_host, runtime.settings.session_daemon_port),
        build_session_memory_http_handler(store),
    )
    logger.warning(
        "session_daemon_started",
        extra={
            "host": runtime.settings.session_daemon_host,
            "port": runtime.settings.session_daemon_port,
        },
    )
    server.serve_forever()


def build_session_memory_http_handler(store: SessionMemoryStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                self._write_json({"status": "ok"})
                return
            self.send_error(404)

        def do_POST(self) -> None:
            payload = self._read_json()
            try:
                if self.path == "/context/get":
                    state = store.get_context(
                        session_id=str(payload["session_id"]),
                        model_id=str(payload["model_id"]),
                    )
                    self._write_json(
                        {
                            "context": state.context,
                            "token_count": state.token_count,
                            "active_context_artifact_id": state.active_context_artifact_id,
                        }
                    )
                    return
                if self.path == "/context/append":
                    state = store.append_turn(
                        session_id=str(payload["session_id"]),
                        model_id=str(payload["model_id"]),
                        user_text=str(payload.get("user_text") or ""),
                        answer=str(payload.get("answer") or ""),
                        model_selector=_optional_str(payload.get("model_selector")),
                        max_window_tokens=_optional_int(payload.get("max_window_tokens")),
                        source_workspace_id=_optional_str(payload.get("source_workspace_id")),
                        retrieved_artifact_ids=_optional_str_list(
                            payload.get("retrieved_artifact_ids")
                        ),
                    )
                    self._write_json({"status": "appended", "token_count": state.token_count})
                    return
                if self.path == "/context/begin":
                    handle = store.begin_turn(
                        session_id=str(payload["session_id"]),
                        model_id=str(payload["model_id"]),
                        user_text=str(payload.get("user_text") or ""),
                        model_selector=_optional_str(payload.get("model_selector")),
                        max_window_tokens=_optional_int(payload.get("max_window_tokens")),
                        source_workspace_id=_optional_str(payload.get("source_workspace_id")),
                        retrieved_artifact_ids=_optional_str_list(
                            payload.get("retrieved_artifact_ids")
                        ),
                    )
                    self._write_json(handle)
                    return
                if self.path == "/context/complete":
                    state = store.complete_turn(
                        session_id=str(payload["session_id"]),
                        model_id=str(payload["model_id"]),
                        turn_id=str(payload["turn_id"]),
                        created_at=datetime.fromisoformat(str(payload["created_at"])),
                        answer=str(payload.get("answer") or ""),
                    )
                    self._write_json({"status": "completed", "token_count": state.token_count})
                    return
                if self.path == "/context/set":
                    state = store.set_context(
                        session_id=str(payload["session_id"]),
                        model_id=str(payload["model_id"]),
                        context=str(payload.get("context") or ""),
                        compacted_summary_artifact_id=_optional_str(
                            payload.get("compacted_summary_artifact_id")
                        ),
                        max_window_tokens=_optional_int(payload.get("max_window_tokens")),
                    )
                    self._write_json({"status": "set", "token_count": state.token_count})
                    return
                if self.path == "/context/flush":
                    rows = store.flush(session_id=_optional_str(payload.get("session_id")))
                    self._write_json({"flushed": rows})
                    return
            except Exception as exc:
                logger.exception("session_daemon_request_failed")
                self.send_error(500, str(exc))
                return
            self.send_error(404)

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

    return Handler


def _join_context(base: str, tail: str) -> str:
    parts = [part.strip() for part in (base, tail) if part and part.strip()]
    return "\n".join(parts) + ("\n" if parts else "")


def _format_session_items(items: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in items:
        role = str(item.get("role") or "")
        content = str(item.get("content") or "").strip()
        if role == "user":
            metadata = item.get("metadata")
            fields: list[str] = []
            if isinstance(metadata, dict):
                for key in (
                    "model_selector",
                    "source_workspace_id",
                    "retrieved_artifact_ids",
                ):
                    value = metadata.get(key)
                    if value:
                        rendered = ",".join(value) if isinstance(value, list) else str(value)
                        fields.append(f"{key}={rendered}")
            prefix = f"<!-- {'; '.join(fields)} -->\n" if fields else ""
            parts.append(f"{prefix}{TURN_USER}\n{content}")
        elif role == "assistant":
            parts.append(f"{TURN_ASSISTANT}\n{content}")
        else:
            parts.append(f"<{role or 'item'}>\n{content}")
    return "\n".join(parts)


def _safe_path_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned[:120] or "default"


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    return int(value)


def _optional_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]
