# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Durable Workflowy refresh: sync + import as two checkpointed DBOS steps.

The sync step (fetch the account, semantic-chunk it) and the import step (embed
every chunk and store it) are separate `@dbos_step`s, so DBOS checkpoints the
sync result. The import step retries with backoff — if the embedder is not
loaded yet, the chunks synced in step one are never lost between attempts. When
DBOS is disabled the workflow still runs, just without durability.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NewType, TypedDict
from uuid import UUID, uuid4

from ._dbos_runtime import DBOS, SetWorkflowID, dbos_step, dbos_workflow
from .settings import get_settings

logger = logging.getLogger(__name__)

DEFAULT_CHUNKS_PATH = "data/seed/workflowy_chunks_with_meta.jsonl"

WorkflowyCorpusGenerationId = NewType("WorkflowyCorpusGenerationId", UUID)


class WorkflowyRefreshResult(TypedDict):
    generation_id: str
    workflow_id: str
    chunked: int
    imported: int


def resolve_generation_id(
    value: WorkflowyCorpusGenerationId | UUID | str | None = None,
) -> WorkflowyCorpusGenerationId:
    """Create one request identity or validate an identity supplied for retry."""
    resolved = uuid4() if value is None else UUID(str(value))
    return WorkflowyCorpusGenerationId(resolved)


def build_workflowy_refresh_id(
    generation_id: WorkflowyCorpusGenerationId | UUID | str,
) -> str:
    """Return the durable workflow identity for exactly one corpus generation."""
    return f"workflowy_refresh:{resolve_generation_id(generation_id)}"


@dbos_step()
def _sync_step(input_path: str | None, chunks_path: str, max_chars: int) -> int:
    """Fetch the Workflowy account (or read a saved export) and write the
    semantic-chunk JSONL. Checkpointed, so import-step retries never re-run it."""
    from .workflowy_sync import sync_workflowy

    return sync_workflowy(
        input_path=Path(input_path) if input_path else None,
        chunks_output=Path(chunks_path),
        max_chars=max_chars,
    )


@dbos_step(
    retries_allowed=True,
    max_attempts=4,
    interval_seconds=120.0,
    backoff_rate=2.0,
)
def _import_step(chunks_path: str, generation_id: str) -> int:
    """Embed every chunk and store it in the vector store. Retried with backoff
    so the embedder can be loaded after the sync without losing synced chunks."""
    from .contracts import WorkspaceId
    from .runtime import get_runtime

    runtime = get_runtime()
    return runtime.retrieval.import_workflowy_chunks_jsonl(
        Path(chunks_path),
        workspace_id=WorkspaceId.WORKFLOWY.value,
        workflow_id=f"workflowy_refresh_import:{generation_id}",
    )


def _execute_workflowy_refresh(
    input_path: str | None,
    chunks_path: str,
    max_chars: int,
    generation_id: str,
) -> WorkflowyRefreshResult:
    chunked = _sync_step(input_path, chunks_path, max_chars)
    imported = _import_step(chunks_path, generation_id)
    return {
        "generation_id": generation_id,
        "workflow_id": build_workflowy_refresh_id(generation_id),
        "chunked": chunked,
        "imported": imported,
    }


@dbos_workflow()
def workflowy_refresh_workflow(
    input_path: str | None,
    chunks_path: str,
    max_chars: int,
    generation_id: str,
) -> WorkflowyRefreshResult:
    return _execute_workflowy_refresh(
        input_path,
        chunks_path,
        max_chars,
        generation_id,
    )


def run_workflowy_refresh(
    *,
    input_path: str | None = None,
    chunks_path: str = DEFAULT_CHUNKS_PATH,
    max_chars: int = 1200,
    generation_id: WorkflowyCorpusGenerationId | UUID | str | None = None,
) -> WorkflowyRefreshResult:
    """Run the refresh as a durable DBOS workflow when DBOS is enabled.

    A normal call creates a fresh generation identity, so a later refresh runs
    the export again even when it writes to the same chunks path. Supplying the
    same generation identity re-enters that one DBOS workflow and replays its
    checkpointed sync result instead of fetching the account twice.
    """
    resolved_generation_id = resolve_generation_id(generation_id)
    generation_id_text = str(resolved_generation_id)
    workflow_id = build_workflowy_refresh_id(resolved_generation_id)
    settings = get_settings()
    if settings.use_dbos and DBOS is not None and SetWorkflowID is not None:
        from .dbos_app import launch_dbos

        launch_dbos()
        with SetWorkflowID(workflow_id):
            return workflowy_refresh_workflow(
                input_path,
                chunks_path,
                max_chars,
                generation_id_text,
            )
    return workflowy_refresh_workflow(
        input_path,
        chunks_path,
        max_chars,
        generation_id_text,
    )
