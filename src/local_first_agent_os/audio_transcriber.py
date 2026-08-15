# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Audio transcription backed by whisper.cpp's HTTP server.

``AudioTranscriber`` is the single integration point for ASR. The workflow
layer and ``/start /asr`` both go through it. The ASR model is resolved from
the registry when present because whisper.cpp has different runtime semantics
from the llama.cpp GGUF roles.

Two modes:

* **batch** – ``transcribe(audio_path)`` POSTs a WAV file to ``/inference``
  and returns a ``TranscriptionResult``. Used by the
  ``audio_transcription`` workflow.
* **stream** – ``stream()`` delegates to ``asr_client.run_stream`` for a
  foreground mic capture session with client-side VAD, rolling
  transcription, and the inactivity-timeout dump-to-file behaviour. Used
  by the ``/start /asr`` directive.

Both modes assume a long-running whisper-server is reachable at
the ASR registry entry's ``server_url`` or ``settings.whisper_base_url``.
``load_active_model()`` starts the service when needed and swaps the active
model in-place via the server's ``/load`` endpoint. ``stop_server()`` shuts
down the service, including its launchd supervisor, so KeepAlive cannot
immediately recreate it.
"""

from __future__ import annotations

import logging
import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from .contracts import ModelRole, ModelSpec
from .model_registry import ModelRegistry
from .settings import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranscriptionResult:
    status: Literal["completed", "stub"]
    text: str = ""
    language: str | None = None
    confidence: float | None = None
    reason: str = ""


class AudioTranscriber:
    """Centralized loader for audio transcription.

    Backed by whisper.cpp's HTTP server. The server is launched by
    ``scripts/start-agent-runtime.sh`` with the registry-selected ASR model resident;
    on-demand lifecycle changes go through ``load_active_model`` /
    ``stop_server``.
    """

    def __init__(self, settings: Settings, registry: ModelRegistry | None = None):
        self.settings = settings
        self.registry = registry

    def _asr_spec(self) -> ModelSpec | None:
        if self.registry is None:
            return None
        try:
            return self.registry.resolve_model(ModelRole.ASR)
        except KeyError:
            return None

    @property
    def base_url(self) -> str:
        if self.settings.whisper_server_url:
            return self.settings.whisper_base_url
        spec = self._asr_spec()
        if spec and spec.server_url:
            return spec.server_url
        return self.settings.whisper_base_url

    def _active_model_path(self) -> str:
        spec = self._asr_spec()
        if spec and (spec.ggml_path or spec.gguf_path):
            return str(spec.ggml_path or spec.gguf_path)
        return str(self.settings.whisper_models_dir / self.settings.whisper_active_model)

    def _idle_model_path(self) -> str:
        return str(self.settings.whisper_models_dir / self.settings.whisper_idle_model)

    def _inference_options(self) -> dict[str, str]:
        data = {"response_format": "verbose_json", "temperature": "0.0"}
        spec = self._asr_spec()
        if spec and spec.language:
            data["language"] = spec.language
        if spec and spec.translate:
            data["translate"] = "true"
        return data

    # ------------------------------------------------------------------
    # Health & lifecycle
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Whether the whisper-server is reachable.

        Mock mode short-circuits to ``True`` so workflow callers can
        exercise the path deterministically without a live server.
        """
        if self.settings.mock_models:
            return True
        try:
            with httpx.Client(base_url=self.base_url, timeout=2) as client:
                response = client.get("/health")
                if response.status_code == 404:
                    response = client.get("/")
            return response.status_code < 500
        except httpx.HTTPError:
            return False

    def load_active_model(self) -> dict[str, str]:
        """Start whisper-server if needed, then select the active model."""
        if self.settings.mock_models:
            return {"status": "mock_loaded", "model": self._active_model_path()}
        path = self._active_model_path()
        if self.is_available():
            self._post_load(path)
            return {"status": "loaded", "model": path, "server": "already_running"}
        output = self._run_service_action("start")
        return {"status": "loaded", "model": path, "server": "started", "output": output}

    def swap_to_idle(self) -> dict[str, str]:
        """Swap back to the idle model so the server stays lightweight."""
        if self.settings.mock_models:
            return {"status": "mock_swapped_to_idle", "model": self.settings.whisper_idle_model}
        path = self._idle_model_path()
        self._post_load(path)
        return {"status": "swapped_to_idle", "model": path}

    def stop_server(self) -> dict[str, str]:
        """Stop whisper-server and any local supervisor that would respawn it."""
        if self.settings.mock_models:
            return {"status": "mock_stopped"}
        output = self._run_service_action("stop")
        return {"status": "stopped", "output": output}

    def _run_service_action(self, action: Literal["start", "stop"]) -> str:
        repo_root = Path(__file__).resolve().parents[2]
        script = repo_root / "scripts" / "whisper-service.sh"
        if not script.exists():
            raise FileNotFoundError(f"whisper service script not found: {script}")
        env = os.environ.copy()
        env.update(
            {
                "LOCAL_AGENT_WHISPER_BASE_URL": self.base_url,
                "LOCAL_AGENT_WHISPER_HOST": self.settings.whisper_host,
                "LOCAL_AGENT_WHISPER_PORT": str(self.settings.whisper_port),
                "LOCAL_AGENT_WHISPER_BIN_PATH": str(self.settings.whisper_bin_path),
                "LOCAL_AGENT_WHISPER_MODELS_DIR": str(self.settings.whisper_models_dir),
                "LOCAL_AGENT_WHISPER_IDLE_MODEL": self.settings.whisper_idle_model,
                "LOCAL_AGENT_WHISPER_THREADS": str(self.settings.whisper_threads),
            }
        )
        completed = subprocess.run(
            ["bash", str(script), action],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        output = (completed.stdout + completed.stderr).strip()
        if completed.returncode != 0:
            raise RuntimeError(
                f"whisper-service.sh {action} exited {completed.returncode}: {output}"
            )
        return output

    def _post_load(self, model_path: str) -> None:
        with httpx.Client(base_url=self.base_url, timeout=300) as client:
            # whisper-server's /load expects multipart with `model=<path>` as
            # a regular form field — not a file upload. The (None, value)
            # tuple form on httpx encodes a multipart text field.
            response = client.post("/load", files={"model": (None, model_path)})
            response.raise_for_status()

    # ------------------------------------------------------------------
    # Batch transcription (workflow audio_transcription path)
    # ------------------------------------------------------------------

    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        if self.settings.mock_models:
            return TranscriptionResult(
                status="completed",
                text=f"Mock ASR transcript for {audio_path.name}.",
                language="en",
                confidence=0.88,
            )
        if not self.is_available():
            return TranscriptionResult(
                status="stub",
                reason=(
                    "UNSUPPORTED_STUB: whisper-server unreachable at "
                    f"{self.base_url}. Did you run scripts/start-agent-runtime.sh?"
                ),
            )
        try:
            with httpx.Client(base_url=self.base_url, timeout=600) as client:
                response = client.post(
                    "/inference",
                    files={
                        # Current whisper-server reads this multipart field as
                        # a filesystem path unless it was started with --convert.
                        "file": (None, str(audio_path)),
                        **{key: (None, value) for key, value in self._inference_options().items()},
                    },
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            logger.warning("whisper /inference failed for %s: %s", audio_path, exc)
            detail = ""
            if isinstance(exc, httpx.HTTPStatusError):
                detail = f" body={exc.response.text[:500]}"
            return TranscriptionResult(
                status="stub",
                reason=f"UNSUPPORTED_STUB: whisper /inference failed: {exc}{detail}",
            )
        text = (data.get("text") or "").strip()
        language = data.get("language")
        # verbose_json gives per-segment logprobs; average them for a single
        # confidence-ish number that callers can use without parsing tokens.
        confidence: float | None = None
        segments = data.get("segments") or []
        if segments:
            probs: list[float] = []
            for seg in segments:
                lp = seg.get("avg_logprob")
                if lp is None:
                    continue
                try:
                    probs.append(math.exp(float(lp)))
                except (TypeError, ValueError):
                    continue
            if probs:
                confidence = sum(probs) / len(probs)
        return TranscriptionResult(
            status="completed",
            text=text,
            language=language,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Streaming (used by `/start /asr`)
    # ------------------------------------------------------------------

    def stream(self) -> int:
        """Open the mic and stream rolling transcription.

        Returns a process-style exit code: 0 = user-ended (Ctrl-C / clean
        completion); 1 = inactivity timeout fired and the transcript was
        written to disk; 2 = audio dependency was missing.

        Delegates to ``asr_client.run_stream`` so the streaming machinery
        (VAD, framing, trigger router) lives in its own module and this
        class stays narrow.
        """
        # Local import keeps the audio extras (sounddevice/webrtcvad) out of
        # the module-load path for callers that only do batch transcription.
        from . import asr_client

        return asr_client.run_stream(self.settings)
