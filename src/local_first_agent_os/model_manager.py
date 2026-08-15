# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import base64
import contextlib
import json
import logging
import math
import time
from collections.abc import Iterator
from typing import Any

import httpx

from ._dbos_runtime import dbos_step
from .artifacts import ArtifactStore
from .constants import DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS
from .contracts import (
    ArtifactRef,
    ArtifactRole,
    MedicalReport,
    ModelCallRequest,
    ModelCallResult,
    ModelRole,
    ModelSpec,
)
from .db import EMBEDDING_DIM
from .ids import build_invocation_id, sha256_text
from .model_registry import ModelRegistry
from .observability import MODEL_CALL_LATENCY_SECONDS, MODEL_CALLS_TOTAL, profiled_step
from .repository import Repository
from .settings import Settings

logger = logging.getLogger(__name__)

UNLOAD_OK_STATUS_CODES = {200, 202, 204, 400, 404}

# Roles permitted to auto-load on demand. The compactor is the only allowed
# auto-load: it fires implicitly when context exceeds the compaction
# threshold (see pi_channel.compact_context_if_needed). Every other model
# load must be triggered by the user via `pi /start <alias>`.
ALWAYS_AUTOLOAD_ROLES: frozenset[ModelRole] = frozenset({ModelRole.COMPACTOR})
PROMOTABLE_GENERAL_ROLES: frozenset[ModelRole] = frozenset(
    {
        ModelRole.GENERAL,
        ModelRole.GENERAL_FALLBACK,
    }
)
ACTIVE_GENERAL_STATE_NAME = "active_general_role"


class ModelNotLoadedError(RuntimeError):
    """Raised when a workflow needs a model that isn't loaded and autoload is denied."""

    def __init__(self, role: ModelRole):
        self.role = role
        super().__init__(
            f"Model role '{role.value}' is not loaded. Auto-load is disabled to "
            f"prevent surprise OOM. Load it with:\n"
            f"    pi /start /{role.value}\n"
            f"or list available models with:\n"
            f"    local-agent models-help"
        )


VISUAL_MIME_PREFIXES = ("image/", "application/dicom")

THINK_OPEN_TAG = "<think>"
THINK_CLOSE_TAG = "</think>"


def llama_message_text(message: dict[str, Any]) -> str:
    """Read an assistant reply that llama.cpp may have split into two channels.

    Thought parsing moves everything following a `<think>` tag into the
    response's `reasoning_content` field. A model that opens a thought tag and
    then writes its real answer inside it therefore returns an empty `content`,
    which reads as a blank reply rather than the transcription it actually
    produced. Fall back to the reasoning channel in that case. With parsing
    disabled the same body arrives in `content` still carrying the literal tag,
    so strip the tags here and leave callers a single field to read.
    """
    text = message.get("content") or ""
    if not text.strip():
        text = message.get("reasoning_content") or ""
    text = text.strip()
    if text.startswith(THINK_OPEN_TAG):
        text = text[len(THINK_OPEN_TAG) :].lstrip()
    if text.endswith(THINK_CLOSE_TAG):
        text = text[: -len(THINK_CLOSE_TAG)].rstrip()
    return text


class VisualInputRoutingError(RuntimeError):
    """Raised when a visual artifact is routed to a role whose model declares
    no multimodal projector; answering would silently ignore the image."""

    def __init__(self, role: ModelRole, model_id: str, mime_type: str):
        self.role = role
        super().__init__(
            f"Visual input ({mime_type}) was routed to model role '{role.value}' "
            f"({model_id}), which declares no mmproj_path in the model registry "
            f"and would answer without seeing the image. Route visual artifacts "
            f"to a role whose registry entry declares a projector, such as 'ocr'."
        )


class ModelManager:
    def __init__(
        self,
        settings: Settings,
        registry: ModelRegistry,
        artifact_store: ArtifactStore,
        repository: Repository,
    ):
        self.settings = settings
        self.registry = registry
        self.artifact_store = artifact_store
        self.repository = repository
        self.loaded_roles: dict[ModelRole, float] = {}
        self._active_sessions: set[ModelRole] = set()
        self._pending_streams: dict[str, dict[str, Any]] = {}
        self.default_role_fallback: ModelRole | None = None
        self.default_fallback_reason: str | None = None
        self.active_general_role: ModelRole = ModelRole.GENERAL
        self.active_general_reason: str | None = None
        self._restore_default_fallback_state()
        self._restore_active_general_role()

    def _llama_models(self) -> dict[ModelRole, Any]:
        return self.registry.by_runtime("llama.cpp")

    def _require_llama_role(self, role: ModelRole) -> None:
        model = self.registry.resolve_model(role)
        if model.runtime != "llama.cpp":
            raise ValueError(
                f"Model role {role.value!r} is served by {model.runtime}, not llama.cpp"
            )

    def _restore_default_fallback_state(self) -> None:
        try:
            state = self.repository.get_fallback_state("default_role")
        except Exception:
            return
        if state is None:
            return
        role_value = state.get("fallback_role")
        if not role_value:
            return
        try:
            self.default_role_fallback = ModelRole(role_value)
            self.default_fallback_reason = state.get("reason")
        except ValueError:
            self.default_role_fallback = None
            self.default_fallback_reason = None

    def _restore_active_general_role(self) -> None:
        try:
            state = self.repository.get_fallback_state(ACTIVE_GENERAL_STATE_NAME)
        except Exception:
            return
        if state is None:
            return
        role_value = state.get("fallback_role")
        if not role_value:
            return
        try:
            role = ModelRole(role_value)
        except ValueError:
            return
        if role in PROMOTABLE_GENERAL_ROLES:
            self.active_general_role = role
            self.active_general_reason = state.get("reason")

    def effective_general_role(self) -> ModelRole:
        return self.active_general_role

    def set_active_general_role(self, role: ModelRole, reason: str | None = None) -> None:
        if role not in PROMOTABLE_GENERAL_ROLES:
            return
        self.active_general_role = role
        self.active_general_reason = reason
        with contextlib.suppress(Exception):
            self.repository.set_fallback_state(
                name=ACTIVE_GENERAL_STATE_NAME,
                fallback_role=role.value,
                reason=reason,
            )

    def clear_active_general_role(self) -> None:
        self.active_general_role = ModelRole.GENERAL
        self.active_general_reason = None
        with contextlib.suppress(Exception):
            self.repository.set_fallback_state(
                name=ACTIVE_GENERAL_STATE_NAME,
                fallback_role=None,
                reason=None,
            )

    def clear_active_general_role_if(self, role: ModelRole | None) -> None:
        if role is None or role == self.active_general_role:
            self.clear_active_general_role()

    def activate_default_fallback(self, fallback_role: ModelRole, reason: str) -> None:
        self.default_role_fallback = fallback_role
        self.default_fallback_reason = reason
        with contextlib.suppress(Exception):
            self.repository.set_fallback_state(
                name="default_role",
                fallback_role=fallback_role.value,
                reason=reason,
            )

    def clear_default_fallback(self) -> None:
        self.default_role_fallback = None
        self.default_fallback_reason = None
        with contextlib.suppress(Exception):
            self.repository.set_fallback_state(
                name="default_role",
                fallback_role=None,
                reason=None,
            )

    def is_default_fallback_active(self) -> bool:
        return self.default_role_fallback is not None

    def _shares_server_model_with_active_general(self, role: ModelRole) -> bool:
        if role == self.active_general_role:
            return True
        try:
            active_name = self.registry.resolve_model(self.active_general_role).server_model_name
            role_name = self.registry.resolve_model(role).server_model_name
        except Exception:
            return False
        return active_name == role_name

    def _discard_loaded_roles_for_server_model(self, server_model_name: str) -> None:
        for loaded_role in list(self.loaded_roles):
            try:
                loaded_name = self.registry.resolve_model(loaded_role).server_model_name
            except Exception:
                continue
            if loaded_name == server_model_name:
                self.loaded_roles.pop(loaded_role, None)

    def _router_status(self, client: httpx.Client, model_name: str) -> str | None:
        response = client.get("/models")
        if response.status_code != 200:
            return None
        try:
            for item in response.json().get("data", []):
                if item.get("id") == model_name:
                    status = item.get("status")
                    if isinstance(status, dict):
                        return str(status.get("value", "unknown"))
                    return str(status or "unknown")
        except (json.JSONDecodeError, AttributeError, TypeError):
            if model_name in response.text:
                return "loaded"
        return None

    def unload_expired(self) -> None:
        if self.settings.mock_models:
            return
        now = time.time()
        expired: list[tuple[ModelRole, str]] = []
        for role, last_used_at in list(self.loaded_roles.items()):
            if role in self._active_sessions:
                continue
            if self._shares_server_model_with_active_general(role):
                continue
            model = self.registry.resolve_model(role)
            if model.pinned or now - last_used_at <= model.warm_ttl_seconds:
                continue
            expired.append((role, model.server_model_name))
        if not expired:
            return
        with httpx.Client(base_url=self.settings.llama_base_url, timeout=120) as client:
            for _role, model_name in expired:
                try:
                    response = client.post("/models/unload", json={"model": model_name})
                    if response.status_code not in UNLOAD_OK_STATUS_CODES:
                        response.raise_for_status()
                except httpx.HTTPError:
                    continue
                self._discard_loaded_roles_for_server_model(model_name)

    def router_models(self) -> list[dict[str, Any]]:
        if self.settings.mock_models:
            return [
                {
                    "id": spec.server_model_name,
                    "role": role.value,
                    "status": "mock",
                    "model_id": spec.model_id,
                }
                for role, spec in self._llama_models().items()
            ]
        with httpx.Client(base_url=self.settings.llama_base_url, timeout=30) as client:
            response = client.get("/models")
            response.raise_for_status()
            return list(response.json().get("data", []))

    def model_status(self) -> dict[str, Any]:
        """Per-role loaded/unloaded snapshot from the llama-server router.

        Correlates each registry role to the router's reported state so
        `pi /status` can show what is actually resident in VRAM. Never raises:
        an unreachable router yields ``reachable=False`` and ``unreachable``
        per-role states.
        """
        by_id: dict[str, Any] = {}
        reachable = True
        try:
            for item in self.router_models():
                by_id[str(item.get("id"))] = item
        except Exception:
            reachable = False
        roles: list[dict[str, Any]] = []
        for role, spec in sorted(self._llama_models().items(), key=lambda kv: kv[1].priority):
            name = spec.server_model_name
            if not reachable:
                state = "unreachable"
            else:
                item = by_id.get(name)
                raw = item.get("status") if isinstance(item, dict) else None
                if isinstance(raw, dict):
                    state = str(raw.get("value", "unknown"))
                elif raw:
                    state = str(raw)
                else:
                    state = "unknown"
            roles.append(
                {
                    "role": role.value,
                    "model": name,
                    "status": state,
                    "active_general": role == self.active_general_role,
                }
            )
        return {"reachable": reachable, "roles": roles}

    def unload(self, role: ModelRole | None = None) -> dict[str, Any]:
        if self.settings.mock_models:
            roles = [role] if role else list(self.loaded_roles)
            for loaded_role in roles:
                if loaded_role is not None:
                    self.loaded_roles.pop(loaded_role, None)
            return {"status": "mock_unloaded", "role": role.value if role else "all"}

        targets = [role] if role else list(self._llama_models())
        results: list[dict[str, Any]] = []
        with httpx.Client(base_url=self.settings.llama_base_url, timeout=120) as client:
            for target_role in targets:
                if target_role is None:
                    continue
                self._require_llama_role(target_role)
                model = self.registry.resolve_model(target_role)
                if (
                    target_role != self.active_general_role
                    and self._shares_server_model_with_active_general(target_role)
                ):
                    self.loaded_roles.pop(target_role, None)
                    results.append(
                        {
                            "role": target_role.value,
                            "model": model.server_model_name,
                            "status": "shared_with_active_general",
                        }
                    )
                    continue
                response = client.post("/models/unload", json={"model": model.server_model_name})
                if response.status_code not in UNLOAD_OK_STATUS_CODES:
                    response.raise_for_status()
                self._discard_loaded_roles_for_server_model(model.server_model_name)
                results.append(
                    {
                        "role": target_role.value,
                        "model": model.server_model_name,
                        "status_code": response.status_code,
                        "status": (
                            "unload_requested"
                            if response.status_code in {200, 202, 204}
                            else "not_loaded"
                        ),
                    }
                )
        return {"status": "unload_requested", "models": results}

    def reload(self, role: ModelRole, *, timeout_seconds: float = 120) -> None:
        """Fully recycle one model child, waiting across the router's async unload boundary."""
        if self.settings.mock_models:
            self.unload(role)
            self.ensure_loaded(role, allow_autoload=True)
            return
        model = self.registry.resolve_model(role)
        self.unload(role)
        deadline = time.monotonic() + timeout_seconds
        with httpx.Client(base_url=self.settings.llama_base_url, timeout=30) as client:
            while time.monotonic() < deadline:
                status = self._router_status(client, model.server_model_name)
                if status in {"unloaded", None}:
                    break
                time.sleep(0.25)
            else:
                raise TimeoutError(
                    f"Timed out waiting to unload llama.cpp model {model.server_model_name}"
                )
        self.ensure_loaded(role, allow_autoload=True)

    @contextlib.contextmanager
    def loaded_session(self, role: ModelRole):
        self._active_sessions.add(role)
        try:
            self.ensure_loaded(role)
            yield
        finally:
            self._active_sessions.discard(role)
            model = self.registry.resolve_model(role)
            if model.warm_ttl_seconds == 0 and not self._shares_server_model_with_active_general(
                role
            ):
                self.unload(role)

    @contextlib.contextmanager
    def preloaded_session(self, role: ModelRole):
        """Use an already-loaded role without applying scoped-session unload policy."""
        self._active_sessions.add(role)
        try:
            self.require_loaded(role)
            yield
        finally:
            self._active_sessions.discard(role)

    def _mock_text_for(self, req: ModelCallRequest, input_text: str) -> dict[str, Any]:
        if req.model_role in {ModelRole.OCR, ModelRole.HARD_OCR}:
            return {
                "text": f"Mock OCR text extracted from {req.input_artifact_id}.",
                "confidence": 0.91,
                "source_excerpt": input_text[:200],
            }
        if req.model_role == ModelRole.MEDICAL:
            return MedicalReport(
                summary="Non-diagnostic mock review of the provided image artifact.",
                visible_findings=["Image was accepted by the durable medical workflow."],
                limitations=["Mock mode did not inspect clinical content."],
                non_diagnostic_disclaimer=(
                    "This is not a diagnosis or treatment recommendation. A qualified clinician "
                    "must review the source image and report."
                ),
                confidence=0.5,
            ).model_dump()
        if req.model_role == ModelRole.COMPACTOR:
            return {
                "durable_facts": [f"Mock compaction output for artifact {req.input_artifact_id}."],
                "user_preferences": [],
                "current_decisions": [],
                "unresolved_questions": [],
                "commands_configs_paths": [],
                "constraints": [],
                "recent_conversation_state": [],
                "exact_snippets_to_preserve": [],
                "failed_attempts_still_relevant": [],
                "image_state": [],
                "tool_state": [],
                "discarded_as_redundant": [],
            }
        return {
            "text": (
                "Mock local answer generated in deterministic harness mode. "
                f"Prompt/input hash: {sha256_text(input_text)[:12]}."
            )
        }

    def _mock_embedding(self, text: str, dimensions: int = EMBEDDING_DIM) -> list[float]:
        digest = sha256_text(text)
        values = []
        for idx in range(dimensions):
            byte = int(digest[(idx * 2) % len(digest) : ((idx * 2) % len(digest)) + 2], 16)
            values.append((byte / 127.5) - 1.0)
        norm = math.sqrt(sum(value * value for value in values)) or 1.0
        return [value / norm for value in values]

    def _effective_params(
        self,
        req: ModelCallRequest,
        model_params: dict[str, Any],
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        params = {**model_params, **req.params}
        if stream:
            params["stream"] = True
        return params

    def embed_texts(self, texts: list[str], workflow_id: str) -> list[list[float]]:
        model = self.registry.resolve_model(ModelRole.EMBEDDER)
        with profiled_step(
            "embedding_worker",
            workflow_id=workflow_id,
            model_role=ModelRole.EMBEDDER.value,
            batch_size=len(texts),
        ):
            if self.settings.mock_models:
                return [self._mock_embedding(text) for text in texts]
            self.ensure_loaded(ModelRole.EMBEDDER)
            with httpx.Client(
                base_url=self.settings.llama_base_url,
                timeout=DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
            ) as client:
                response = client.post(
                    "/v1/embeddings",
                    json={"model": model.server_model_name, "input": texts},
                )
                response.raise_for_status()
                payload = response.json()
            self.loaded_roles[ModelRole.EMBEDDER] = time.time()
            return [item["embedding"] for item in payload["data"]]

    def ensure_loaded(
        self,
        role: ModelRole,
        *,
        allow_autoload: bool = False,
        wait_seconds: int = DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
    ) -> None:
        """Have the router hold this role live, waiting at most ``wait_seconds``.

        The wait bound belongs to the caller because the tolerable stall does. A
        frontier agent's hour-long budget is fine at its own door; an advisory
        call assembled inline on the way to spawning an agent is not allowed to
        hold its tier slot for an hour polling a load that is not finishing, so
        `call_model` passes each request's own timeout down.
        """

        with profiled_step("llama_cpp_load", model_role=role.value):
            self._require_llama_role(role)
            if self.settings.mock_models:
                self.loaded_roles[role] = time.time()
                return
            self.unload_expired()
            model = self.registry.resolve_model(role)
            with httpx.Client(
                base_url=self.settings.llama_base_url,
                timeout=wait_seconds,
            ) as client:
                status = self._router_status(client, model.server_model_name)
                if status in {"loaded", "sleeping"}:
                    self.loaded_roles[role] = time.time()
                    return
                if not (allow_autoload or role in ALWAYS_AUTOLOAD_ROLES):
                    raise ModelNotLoadedError(role)
                if status != "loading":
                    load_payload = {"model": model.server_model_name}
                    if status is None and model.gguf_path:
                        load_payload["path"] = model.gguf_path
                    if status is None and model.mmproj_path:
                        load_payload["mmproj"] = model.mmproj_path
                    load_response = client.post("/models/load", json=load_payload)
                    load_response.raise_for_status()
                for _ in range(max(1, int(wait_seconds))):
                    status = self._router_status(client, model.server_model_name)
                    if status in {"loaded", "sleeping"}:
                        break
                    if status == "failed":
                        raise RuntimeError(
                            f"llama.cpp failed to load model {model.server_model_name}"
                        )
                    time.sleep(1)
                if status not in {"loaded", "sleeping"}:
                    raise TimeoutError(
                        f"Timed out loading llama.cpp model {model.server_model_name}"
                    )
                self.loaded_roles[role] = time.time()

    def require_loaded(self, role: ModelRole) -> None:
        """Require a role to already be live in the router without autoloading it."""
        self._require_llama_role(role)
        if self.settings.mock_models:
            self.loaded_roles[role] = time.time()
            return
        model = self.registry.resolve_model(role)
        with httpx.Client(base_url=self.settings.llama_base_url, timeout=30) as client:
            status = self._router_status(client, model.server_model_name)
            if status in {"loaded", "sleeping"}:
                self.loaded_roles[role] = time.time()
                return
        raise ModelNotLoadedError(role)

    @dbos_step(
        retries_allowed=True,
        max_attempts=2,
        interval_seconds=2.0,
        backoff_rate=2.0,
    )
    def call_model(self, req: ModelCallRequest) -> ModelCallResult:
        model = self.registry.resolve_model(req.model_role)
        effective_params = self._effective_params(req, model.default_params)
        effective_req = req.model_copy(update={"params": effective_params})
        input_ref = self.repository.get_artifact(req.input_artifact_id)
        if input_ref is None:
            raise KeyError(f"Missing model input artifact: {req.input_artifact_id}")
        input_path = self.artifact_store.local_path(input_ref)
        input_text = (
            input_path.read_text(encoding="utf-8", errors="replace")
            if input_path.exists() and input_ref.mime_type.startswith(("text/", "application/json"))
            else input_ref.sha256
        )
        invocation_id = build_invocation_id(
            req.workflow_id,
            req.model_role.value,
            input_ref.sha256,
            {"payload": req.payload, "params": effective_params},
        )
        started = time.perf_counter()
        status = "failed"
        try:
            self._require_visual_capability(model, input_ref)
            if req.model_role == ModelRole.HARD_OCR:
                step = "chandra_ocr"
            elif req.model_role == ModelRole.OCR:
                step = "surya_ocr"
            else:
                step = "llama_cpp_call"
            with profiled_step(
                step,
                workflow_id=req.workflow_id,
                model_role=req.model_role.value,
                artifact_id=req.input_artifact_id,
            ):
                self.ensure_loaded(req.model_role, wait_seconds=req.timeout_seconds)
                if self.settings.mock_models:
                    output = self._mock_text_for(effective_req, input_text)
                else:
                    output = self._call_llama(effective_req, model, input_ref)
                    self.loaded_roles[req.model_role] = time.time()
            latency_ms = int((time.perf_counter() - started) * 1000)
            artifact = self.artifact_store.write_json(
                role=ArtifactRole.MODEL_OUTPUT.value,
                payload={
                    "schema_version": "model_output.v1",
                    "invocation_id": invocation_id,
                    "model_role": req.model_role.value,
                    "model_id": model.model_id,
                    "output": output,
                },
                workflow_id=req.workflow_id,
                schema_version="model_output.v1",
            )
            self.repository.record_model_invocation(
                invocation_id=invocation_id,
                workflow_id=req.workflow_id,
                model_role=req.model_role.value,
                model_id=model.model_id,
                input_artifact_id=req.input_artifact_id,
                params=effective_params,
                output_artifact_id=artifact.artifact_id,
                latency_ms=latency_ms,
                status="completed",
            )
            status = "completed"
            logger.info(
                "model_call_completed",
                extra={"model_role": req.model_role.value, "artifact_id": req.input_artifact_id},
            )
            return ModelCallResult(
                invocation_id=invocation_id,
                model_role=req.model_role,
                model_id=model.model_id,
                output_artifact=artifact,
                latency_ms=latency_ms,
            )
        except Exception as exc:
            self.repository.record_model_invocation(
                invocation_id=invocation_id,
                workflow_id=req.workflow_id,
                model_role=req.model_role.value,
                model_id=model.model_id,
                input_artifact_id=req.input_artifact_id,
                params=effective_params,
                output_artifact_id=None,
                latency_ms=int((time.perf_counter() - started) * 1000),
                status="failed",
                error=str(exc),
            )
            logger.exception(
                "model_call_failed",
                extra={"model_role": req.model_role.value, "artifact_id": req.input_artifact_id},
            )
            raise
        finally:
            elapsed = time.perf_counter() - started
            MODEL_CALLS_TOTAL.labels(model_role=req.model_role.value, status=status).inc()
            MODEL_CALL_LATENCY_SECONDS.labels(
                model_role=req.model_role.value,
                status=status,
            ).observe(elapsed)

    def stream_deltas(self, req: ModelCallRequest) -> Iterator[str]:
        model = self.registry.resolve_model(req.model_role)
        effective_params = self._effective_params(req, model.default_params)
        stream_params = self._effective_params(req, model.default_params, stream=True)
        effective_req = req.model_copy(update={"params": effective_params})
        input_ref = self.repository.get_artifact(req.input_artifact_id)
        if input_ref is None:
            raise KeyError(f"Missing model input artifact: {req.input_artifact_id}")
        input_path = self.artifact_store.local_path(input_ref)
        input_text = (
            input_path.read_text(encoding="utf-8", errors="replace")
            if input_path.exists() and input_ref.mime_type.startswith(("text/", "application/json"))
            else input_ref.sha256
        )
        invocation_id = build_invocation_id(
            req.workflow_id,
            req.model_role.value,
            input_ref.sha256,
            {"payload": req.payload, "params": stream_params},
        )
        started = time.perf_counter()
        text_parts: list[str] = []
        try:
            self._require_visual_capability(model, input_ref)
            with profiled_step(
                "llama_cpp_call",
                workflow_id=req.workflow_id,
                model_role=req.model_role.value,
                artifact_id=req.input_artifact_id,
            ):
                self.ensure_loaded(req.model_role, wait_seconds=req.timeout_seconds)
                if self.settings.mock_models:
                    output = self._mock_text_for(effective_req, input_text)
                    text = str(output.get("text", output))
                    for char in text:
                        text_parts.append(char)
                        yield char
                else:
                    for delta in self._stream_llama(
                        effective_req,
                        model,
                        input_ref,
                    ):
                        text_parts.append(delta)
                        yield delta
                    self.loaded_roles[req.model_role] = time.time()
        except Exception as exc:
            elapsed = time.perf_counter() - started
            self.repository.record_model_invocation(
                invocation_id=invocation_id,
                workflow_id=req.workflow_id,
                model_role=req.model_role.value,
                model_id=model.model_id,
                input_artifact_id=req.input_artifact_id,
                params=stream_params,
                output_artifact_id=None,
                latency_ms=int(elapsed * 1000),
                status="failed",
                error=str(exc),
            )
            MODEL_CALLS_TOTAL.labels(model_role=req.model_role.value, status="failed").inc()
            MODEL_CALL_LATENCY_SECONDS.labels(
                model_role=req.model_role.value, status="failed"
            ).observe(elapsed)
            logger.exception(
                "model_call_failed",
                extra={"model_role": req.model_role.value, "artifact_id": req.input_artifact_id},
            )
            raise
        self._pending_streams[req.workflow_id] = {
            "output": {"text": "".join(text_parts)},
            "invocation_id": invocation_id,
            "model_id": model.model_id,
            "params": stream_params,
            "started": started,
        }

    def write_completed_stream_result(self, req: ModelCallRequest) -> ModelCallResult:
        pending = self._pending_streams.pop(req.workflow_id)
        latency_ms = int((time.perf_counter() - pending["started"]) * 1000)
        artifact = self.artifact_store.write_json(
            role=ArtifactRole.MODEL_OUTPUT.value,
            payload={
                "schema_version": "model_output.v1",
                "invocation_id": pending["invocation_id"],
                "model_role": req.model_role.value,
                "model_id": pending["model_id"],
                "output": pending["output"],
            },
            workflow_id=req.workflow_id,
            schema_version="model_output.v1",
        )
        self.repository.record_model_invocation(
            invocation_id=pending["invocation_id"],
            workflow_id=req.workflow_id,
            model_role=req.model_role.value,
            model_id=pending["model_id"],
            input_artifact_id=req.input_artifact_id,
            params=pending["params"],
            output_artifact_id=artifact.artifact_id,
            latency_ms=latency_ms,
            status="completed",
        )
        MODEL_CALLS_TOTAL.labels(model_role=req.model_role.value, status="completed").inc()
        MODEL_CALL_LATENCY_SECONDS.labels(
            model_role=req.model_role.value, status="completed"
        ).observe(latency_ms / 1000)
        logger.info(
            "model_call_completed",
            extra={"model_role": req.model_role.value, "artifact_id": req.input_artifact_id},
        )
        return ModelCallResult(
            invocation_id=pending["invocation_id"],
            model_role=req.model_role,
            model_id=pending["model_id"],
            output_artifact=artifact,
            latency_ms=latency_ms,
        )

    def _require_visual_capability(self, model: ModelSpec, input_ref: ArtifactRef) -> None:
        """Die loudly when a visual artifact reaches a text-only model; the
        alternative is a plausible answer produced without seeing the image."""
        if not input_ref.mime_type.startswith(VISUAL_MIME_PREFIXES):
            return
        if model.mmproj_path:
            return
        raise VisualInputRoutingError(model.role, model.model_id, input_ref.mime_type)

    def _messages_for_request(
        self,
        req: ModelCallRequest,
        input_ref: ArtifactRef,
        model: ModelSpec,
    ) -> list[dict[str, Any]]:
        messages = req.payload.get("messages")
        if messages is None:
            messages = [{"role": "user", "content": req.payload.get("prompt", "")}]
        input_is_visual = input_ref.mime_type.startswith(VISUAL_MIME_PREFIXES)
        if input_is_visual:
            ref_path = self.artifact_store.local_path(input_ref)
            encoded = base64.b64encode(ref_path.read_bytes()).decode("ascii")
            text_part = {
                "type": "text",
                "text": req.payload.get("prompt", "Analyze this image."),
            }
            image_part = {
                "type": "image_url",
                "image_url": {"url": f"data:{input_ref.mime_type};base64,{encoded}"},
            }
            messages = [
                {
                    "role": "user",
                    "content": (
                        [image_part, text_part] if model.image_first else [text_part, image_part]
                    ),
                }
            ]
        return messages

    @staticmethod
    def _body_for_request(
        req: ModelCallRequest,
        model: ModelSpec,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """The chat-completions body, with the model's reasoning policy applied.

        `req.params` wins over the registry, because a caller that asked for more
        thinking on this one call is making the decision the policy exists to
        default. `chat_template_kwargs` is merged key-by-key rather than
        replaced, so overriding the reasoning band does not silently drop an
        unrelated template argument the caller also sent.
        """

        body: dict[str, Any] = {
            "model": model.server_model_name,
            "messages": messages,
        }
        overrides = model.reasoning_request_overrides()
        template_kwargs = {
            **overrides.get("chat_template_kwargs", {}),
            **req.params.get("chat_template_kwargs", {}),
        }
        body.update({k: v for k, v in overrides.items() if k != "chat_template_kwargs"})
        body.update(req.params)
        if template_kwargs:
            body["chat_template_kwargs"] = template_kwargs
        return body

    def _call_llama(
        self,
        req: ModelCallRequest,
        model: ModelSpec,
        input_ref: ArtifactRef,
    ) -> dict[str, Any]:
        messages = self._messages_for_request(req, input_ref, model)
        with httpx.Client(
            base_url=self.settings.llama_base_url,
            timeout=req.timeout_seconds,
        ) as client:
            response = client.post(
                "/v1/chat/completions",
                json=self._body_for_request(req, model, messages),
            )
            response.raise_for_status()
            data = response.json()
        content = llama_message_text(data["choices"][0]["message"])
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return {"text": content}

    def _stream_llama(
        self,
        req: ModelCallRequest,
        model: ModelSpec,
        input_ref: ArtifactRef,
    ) -> Iterator[str]:
        messages = self._messages_for_request(req, input_ref, model)
        with (
            httpx.Client(
                base_url=self.settings.llama_base_url,
                timeout=req.timeout_seconds,
            ) as client,
            client.stream(
                "POST",
                "/v1/chat/completions",
                json={**self._body_for_request(req, model, messages), "stream": True},
            ) as response,
        ):
            response.raise_for_status()
            for line in response.iter_lines():
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                if not line or not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    break
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choice = payload.get("choices", [{}])[0]
                delta = choice.get("delta", {})
                content = (
                    delta.get("content")
                    or delta.get("reasoning_content")
                    or choice.get("text")
                    or ""
                )
                if not content:
                    continue
                yield str(content)
