# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from threading import Lock

from .artifacts import ArtifactStore
from .audio_transcriber import AudioTranscriber
from .db import Database
from .model_manager import ModelManager
from .model_registry import ModelRegistry
from .observability import configure_observability
from .pi_prompts import PiPromptRegistry
from .pi_runtime import PiRuntime
from .policies import PolicyStore, seed_workspace_rows
from .repository import Repository
from .retrieval import RetrievalService
from .settings import Settings, get_settings
from .tools import ToolRegistry


@dataclass
class AppRuntime:
    settings: Settings
    database: Database
    repository: Repository
    artifact_store: ArtifactStore
    policy_store: PolicyStore
    model_registry: ModelRegistry
    model_manager: ModelManager
    retrieval: RetrievalService
    tool_registry: ToolRegistry
    pi: PiRuntime
    pi_prompts: PiPromptRegistry
    audio_transcriber: AudioTranscriber

    def close(self) -> None:
        """Release long-lived runtime resources such as supervised tool processes."""

        self.tool_registry.close()


def build_runtime(settings: Settings | None = None) -> AppRuntime:
    """Build a runtime that is ready to use.

    Observability, schema, and workspace seeding happen here rather than in a
    second call the caller has to remember. A returned AppRuntime that still
    needs initializing is a runtime every caller can forget to initialize, and
    the failure mode is a runtime that looks built but has no schema.
    """

    settings = settings or get_settings()
    database = Database(settings)
    repository = Repository(database)
    artifact_store = ArtifactStore(settings.artifact_root, repository, settings)
    policy_store = PolicyStore(settings)
    model_registry = ModelRegistry(settings)
    model_manager = ModelManager(settings, model_registry, artifact_store, repository)
    retrieval = RetrievalService(repository, artifact_store, model_manager)
    tool_registry = ToolRegistry(settings, policy_store, repository)
    pi = PiRuntime(policy_store, repository, artifact_store, retrieval)
    pi_prompts = PiPromptRegistry(settings.pi_prompts_path)
    audio_transcriber = AudioTranscriber(settings, model_registry)
    configure_observability(settings)
    repository.create_database_schema()
    seed_workspace_rows(policy_store, repository)
    return AppRuntime(
        settings=settings,
        database=database,
        repository=repository,
        artifact_store=artifact_store,
        policy_store=policy_store,
        model_registry=model_registry,
        model_manager=model_manager,
        retrieval=retrieval,
        tool_registry=tool_registry,
        pi=pi,
        pi_prompts=pi_prompts,
        audio_transcriber=audio_transcriber,
    )


_runtime_lock = Lock()


@lru_cache(maxsize=1)
def _get_runtime_cached() -> AppRuntime:
    return build_runtime()


def get_runtime() -> AppRuntime:
    with _runtime_lock:
        return _get_runtime_cached()
