# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

from .contracts import ModelRole, ModelSpec, ReasoningPolicy, SpeculativeDecoding
from .settings import Settings


def _home_path(*parts: str) -> str:
    return str(Path.home().joinpath(*parts))


DEFAULT_MODELS: list[ModelSpec] = [
    ModelSpec(
        alias="general_gemma4",
        role=ModelRole.GENERAL,
        model_id="gemma-4-e4b-q4-k-m",
        server_model_name="gemma4",
        runtime="llama.cpp",
        backend="metal",
        gguf_path=_home_path("models", "gemma4", "model.gguf"),
        warm_ttl_seconds=900,
        priority=10,
        context_window=65536,
        reasoning=ReasoningPolicy(mode="off"),
        default_params={"temperature": 1.0, "top_k": 64, "top_p": 0.95},
    ),
    ModelSpec(
        alias="qwen38_fallback",
        role=ModelRole.GENERAL_FALLBACK,
        model_id="qwen3.8-27b-mtp-ud-q5-k-xl",
        server_model_name="qwen3.8-27b-mtp",
        runtime="llama.cpp",
        backend="metal",
        gguf_path=_home_path("models", "qwen3.8-27b-mtp", "model.gguf"),
        warm_ttl_seconds=900,
        priority=15,
        context_window=8192,
        parallel=1,
        reasoning=ReasoningPolicy(mode="off"),
        speculative=SpeculativeDecoding(
            type="draft-mtp",
            draft_n_max=2,
            draft_gguf_path=_home_path("models", "qwen3.8-27b-mtp", "draft.gguf"),
        ),
        default_params={"temperature": 0.6, "top_k": 20, "top_p": 0.95},
    ),
    ModelSpec(
        alias="glimmer_deliberator",
        role=ModelRole.DELIBERATOR,
        model_id="muse-glimmer-30b-kquant-dynamic",
        server_model_name="glimmer",
        runtime="llama.cpp",
        backend="metal",
        gguf_path=_home_path("models", "glimmer", "model.gguf"),
        warm_ttl_seconds=900,
        priority=14,
        context_window=32768,
        parallel=1,
        reasoning=ReasoningPolicy(mode="off"),
        reasoning_dialect="reasoning_strength",
        speculative=SpeculativeDecoding(
            type="draft-dflash",
            draft_n_max=2,
            draft_gguf_path=_home_path("models", "glimmer", "draft.gguf"),
        ),
        default_params={"temperature": 0.6, "top_k": 20, "top_p": 0.95},
    ),
    ModelSpec(
        alias="context_compactor",
        role=ModelRole.COMPACTOR,
        model_id="gemma-4-e4b-q4-k-m",
        server_model_name="gemma4",
        runtime="llama.cpp",
        backend="metal",
        gguf_path=_home_path("models", "gemma4", "model.gguf"),
        warm_ttl_seconds=0,
        priority=16,
        context_window=65536,
        default_params={"temperature": 1.0, "top_k": 64, "top_p": 0.95},
    ),
    ModelSpec(
        alias="surya_ocr_2",
        role=ModelRole.OCR,
        model_id="surya-ocr-2",
        server_model_name="surya-ocr-2",
        runtime="llama.cpp",
        backend="metal",
        gguf_path=_home_path("models", "surya-ocr-2", "model.gguf"),
        mmproj_path=_home_path("models", "surya-ocr-2", "mmproj.gguf"),
        warm_ttl_seconds=300,
        priority=20,
        context_window=32768,
        parallel=1,
        image_first=True,
        # Surya's vision tower stops adding detail at roughly 4.1 megapixels;
        # a 3:4 frame at 2048 px stays just under that ceiling.
        ocr_max_dimension=2048,
    ),
    ModelSpec(
        alias="chandra_ocr_2_q8",
        role=ModelRole.HARD_OCR,
        model_id="chandra-ocr-2",
        server_model_name="chandra-ocr-2-q8",
        runtime="llama.cpp",
        backend="metal",
        gguf_path=_home_path("models", "chandra-ocr-2-q8", "model.gguf"),
        mmproj_path=_home_path("models", "chandra-ocr-2-q8", "mmproj.gguf"),
        warm_ttl_seconds=300,
        priority=21,
        context_window=32768,
        parallel=1,
        # chandra-ocr-2 opens a `<think>` tag and then writes its transcription
        # inside it. Under llama.cpp's default deepseek parsing the whole body
        # lands in `reasoning_content` and `content` comes back empty.
        reasoning_format="none",
        ocr_max_dimension=2048,
    ),
    ModelSpec(
        alias="whisper_large_v3_turbo",
        role=ModelRole.ASR,
        model_id="whisper-large-v3-turbo",
        server_model_name="whisper-large-v3-turbo",
        runtime="whisper.cpp",
        backend="coreml+metal",
        ggml_path=_home_path("ai_projects", "whisper.cpp", "models", "ggml-large-v3-turbo.bin"),
        coreml_path=_home_path(
            "ai_projects",
            "whisper.cpp",
            "models",
            "ggml-large-v3-turbo-encoder.mlmodelc",
        ),
        warm_ttl_seconds=300,
        priority=30,
        server_url="http://127.0.0.1:8090",
        port=8090,
        language="auto",
        translate=False,
        threads=8,
    ),
    ModelSpec(
        alias="qwen_embed_8b",
        role=ModelRole.EMBEDDER,
        model_id="qwen-embed-8b",
        server_model_name="qwen-embed-8b",
        runtime="llama.cpp",
        backend="metal",
        gguf_path=_home_path("models", "qwen-embed-8b", "model.gguf"),
        warm_ttl_seconds=600,
        priority=50,
        context_window=32768,
    ),
    ModelSpec(
        alias="medgemma_4b",
        role=ModelRole.MEDICAL,
        model_id="medgemma-4b",
        server_model_name="medgemma-4b",
        runtime="llama.cpp",
        backend="metal",
        gguf_path=_home_path("models", "medgemma-4b", "model.gguf"),
        mmproj_path=_home_path("models", "medgemma-4b", "mmproj.gguf"),
        warm_ttl_seconds=120,
        priority=40,
        context_window=8192,
    ),
]


class ModelRegistry:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.models = self._load_model_specs()

    def _load_model_specs(self) -> dict[ModelRole, ModelSpec]:
        payload = self.settings.load_toml(self.settings.model_registry_path)
        models = DEFAULT_MODELS
        if payload.get("models"):
            models = [
                ModelSpec.model_validate({"alias": alias, **body})
                for alias, body in payload["models"].items()
            ]
        return {model.role: model for model in models}

    def resolve_model(self, role: ModelRole | str) -> ModelSpec:
        model_role = ModelRole(role)
        try:
            return self.models[model_role]
        except KeyError as exc:
            raise KeyError(f"No model configured for role {model_role}") from exc

    def by_runtime(self, runtime: str) -> dict[ModelRole, ModelSpec]:
        return {role: spec for role, spec in self.models.items() if spec.runtime == runtime}

    def role_for_server_name(self, server_model_name: str) -> ModelRole | None:
        """Reverse-resolve a served model id (e.g. 'gemma4') to the role that
        maps to it, so a bench slot can pick a model by name, not just by role."""
        for role, spec in self.models.items():
            if spec.server_model_name == server_model_name:
                return role
        return None

    def as_dict(self) -> dict[str, dict[str, object]]:
        return {role.value: spec.model_dump() for role, spec in self.models.items()}
