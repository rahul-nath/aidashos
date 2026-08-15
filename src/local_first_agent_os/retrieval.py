# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import httpx

from .artifacts import ArtifactStore
from .contracts import (
    CHUNKER_VERSION,
    ArtifactRef,
    ArtifactRole,
    GraphNeighborhood,
    GraphRetrievalBounds,
    ModelRole,
    SearchHit,
    StableTextChunk,
    WorkflowType,
    WorkspaceId,
)
from .db import EMBEDDING_DIM
from .ids import build_chunk_id, sha256_text
from .model_manager import ModelManager, ModelNotLoadedError
from .observability import (
    EMBEDDING_BATCH_SIZE,
    GRAPH_AUGMENTED_QUERY_LATENCY_SECONDS,
    current_workflow_type,
    profiled_step,
)
from .repository import Repository

EMBEDDABLE_ROLES = {
    ArtifactRole.OCR_TEXT.value,
    ArtifactRole.NORMALIZED_TEXT.value,
    ArtifactRole.NOTES_SNAPSHOT.value,
    ArtifactRole.WORKFLOWY_NODE_SNAPSHOT.value,
    ArtifactRole.TRANSCRIPT.value,
    ArtifactRole.ANSWER.value,
}


def _tokens(text: str) -> list[str]:
    return re.findall(r"\S+", text)


def chunk_text(
    *,
    artifact: ArtifactRef,
    workspace_id: str,
    text: str,
    embedding_model_id: str,
    max_tokens: int = 1000,
    overlap_tokens: int = 100,
) -> list[StableTextChunk]:
    tokens = _tokens(text)
    if not tokens:
        return []
    chunks: list[StableTextChunk] = []
    start = 0
    index = 0
    while start < len(tokens):
        end = min(len(tokens), start + max_tokens)
        body = " ".join(tokens[start:end])
        chunks.append(
            StableTextChunk(
                chunk_id=build_chunk_id(artifact.sha256, index, embedding_model_id),
                artifact_id=artifact.artifact_id,
                workspace_id=workspace_id,
                chunk_index=index,
                text=body,
                text_sha256=sha256_text(body),
                embedding_model_id=embedding_model_id,
                metadata={
                    "schema_version": "embedding_chunk.v1",
                    "chunker_version": CHUNKER_VERSION,
                    "source_offsets": {"token_start": start, "token_end": end},
                    "artifact_role": str(artifact.role),
                },
            )
        )
        index += 1
        if end == len(tokens):
            break
        start = max(end - overlap_tokens, start + 1)
    return chunks


def truncate_embedding(values: list[float], dim: int = EMBEDDING_DIM) -> list[float]:
    """Matryoshka-truncate an embedding to `dim` dimensions and L2-renormalize.

    The embedder emits wider vectors than pgvector can HNSW-index; slicing the
    leading `dim` components preserves Qwen3's Matryoshka structure.
    """
    sliced = list(values[:dim])
    norm = math.sqrt(sum(value * value for value in sliced)) or 1.0
    return [value / norm for value in sliced]


class RetrievalService:
    def __init__(
        self,
        repository: Repository,
        artifact_store: ArtifactStore,
        model_manager: ModelManager,
    ):
        self.repository = repository
        self.artifact_store = artifact_store
        self.model_manager = model_manager

    def embed_artifact(self, artifact: ArtifactRef, workspace_id: str, workflow_id: str) -> int:
        if str(artifact.role) not in EMBEDDABLE_ROLES:
            return 0
        text = self.artifact_store.local_path(artifact).read_text(
            encoding="utf-8",
            errors="replace",
        )
        model_id = self.model_manager.registry.resolve_model("embedder").model_id
        with profiled_step("embedding_worker", workflow_id=workflow_id):
            chunks = chunk_text(
                artifact=artifact,
                workspace_id=workspace_id,
                text=text,
                embedding_model_id=model_id,
            )
            EMBEDDING_BATCH_SIZE.labels(workflow_type=current_workflow_type()).observe(len(chunks))
            embeddings = self.model_manager.embed_texts(
                [chunk.text for chunk in chunks],
                workflow_id,
            )
        added = 0
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            if self.repository.upsert_embedding_chunk(
                chunk_id=chunk.chunk_id,
                artifact_id=chunk.artifact_id,
                workspace_id=workspace_id,
                chunk_index=chunk.chunk_index,
                text_sha256=chunk.text_sha256,
                text=chunk.text,
                embedding_model_id=chunk.embedding_model_id,
                embedding=truncate_embedding(embedding),
                metadata=chunk.metadata,
            ):
                added += 1
        return added

    def search(
        self,
        query: str,
        workspace_id: str | None = None,
        top_k: int = 50,
    ) -> list[SearchHit]:
        query_embedding = truncate_embedding(
            self.model_manager.embed_texts([query], "retrieval-query")[0]
        )
        results = self.repository.search_embedding_chunks(query_embedding, workspace_id, top_k)
        return [
            SearchHit(
                chunk_id=row.chunk_id,
                artifact_id=row.artifact_id,
                workspace_id=row.workspace_id,
                text=row.text,
                score=score,
                metadata=row.metadata_json or {},
            )
            for row, score in results
        ]

    def graph_augmented_context(
        self,
        query: str,
        *,
        retrieval_bounds: GraphRetrievalBounds,
        workspace_id: str | None = None,
        top_k: int = 8,
    ) -> tuple[list[SearchHit], GraphNeighborhood]:
        """Find entry points by vector search, then expand them through the graph.

        The two halves are returned separately rather than blended: the caller
        feeds both to the model, and `pi /get` keeps working off the hits alone.
        Vector search is unchanged by the presence of a graph, which is what
        makes GraphRAG an opt-in path rather than a replacement.

        A hit whose chunk touches no node contributes no seed, so an unpopulated
        graph degrades to exactly the vector hits.
        """
        with (
            profiled_step("graph_augmented_query"),
            GRAPH_AUGMENTED_QUERY_LATENCY_SECONDS.labels(
                workflow_type=WorkflowType.MODEL_DIRECTIVE.value
            ).time(),
        ):
            hits = self.search(query, workspace_id=workspace_id, top_k=top_k)
            seed_node_ids = self.repository.graph_seed_nodes(
                chunk_ids=[hit.chunk_id for hit in hits],
                artifact_ids=[hit.artifact_id for hit in hits],
            )
            neighborhood = self.repository.graph_neighborhood(
                seed_node_ids=seed_node_ids,
                max_hops=retrieval_bounds.max_hops,
                max_neighbors=retrieval_bounds.max_neighbors,
            )
        return hits, neighborhood

    def fetch_workflowy(self, query: str, top_k: int = 8) -> list[SearchHit]:
        """Fetch Workflowy-only evidence from the indexed retrieval store.

        Ordinal path questions such as "the first bullet under /ideas" are
        answered from the semantic chunk metadata so ordering is deterministic.
        Other questions use vector search scoped to the Workflowy workspace.
        """
        top_level_match = re.search(r"(?<![\w/])(/[A-Za-z0-9][\w-]*)", query)
        wants_first = bool(re.search(r"\b(?:first|1st)\b", query, re.IGNORECASE))
        if top_level_match is not None and wants_first:
            top_level = top_level_match.group(1)
            candidates = [
                row
                for row in self.repository.list_embedding_chunks(WorkspaceId.WORKFLOWY.value)
                if (row.metadata_json or {}).get("top_level") == top_level
                and isinstance(
                    (row.metadata_json or {}).get("workflowy_chunk_idx"),
                    int,
                )
            ]
            if not candidates:
                return []
            first_chunk_idx = min(
                int(row.metadata_json["workflowy_chunk_idx"]) for row in candidates
            )
            selected = [
                row
                for row in candidates
                if row.metadata_json.get("workflowy_chunk_idx") == first_chunk_idx
            ]
            selected.sort(key=lambda row: int((row.metadata_json or {}).get("sub_chunk", 0)))
            return [
                SearchHit(
                    chunk_id=row.chunk_id,
                    artifact_id=row.artifact_id,
                    workspace_id=row.workspace_id,
                    text=row.text,
                    score=1.0,
                    metadata={
                        **(row.metadata_json or {}),
                        "retrieval_mode": "structured_rag_metadata",
                    },
                )
                for row in selected[:top_k]
            ]
        return self.search(
            query,
            workspace_id=WorkspaceId.WORKFLOWY.value,
            top_k=top_k,
        )

    def import_workflowy_chunks_jsonl(
        self,
        path: Path,
        workspace_id: str,
        workflow_id: str,
        limit: int | None = None,
        top_level: str | None = None,
        batch_size: int = 1,
    ) -> int:
        """Import a metadata-aware Workflowy chunk export.

        Each JSONL line is already a semantic retrieval unit produced by
        ai_stack_local's tree-aware chunker. The importer preserves that unit
        atomically: one source record becomes exactly one pgvector row, including
        long idea subtrees. A complete refresh prunes stale rows from the same
        source/scope only after all replacement embeddings succeed. Transport
        requests default to one record because llama.cpp's pooled embedding
        endpoint is more reliable that way; this does not change chunk boundaries.
        """
        model_id = self.model_manager.registry.resolve_model("embedder").model_id
        # Fail fast before writing any artifacts if the embedder is not loaded.
        self.model_manager.require_loaded(ModelRole.EMBEDDER)
        source_path = str(path.expanduser().resolve())
        existing_by_id = {
            row.chunk_id: row for row in self.repository.list_embedding_chunks(workspace_id)
        }
        pending: list[tuple[ArtifactRef, str, str, dict[str, object]]] = []
        imported_chunk_ids: set[str] = set()
        matched_records = 0
        for idx, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            payload = json.loads(line)
            if top_level is not None and payload.get("top_level") != top_level:
                continue
            if limit is not None and matched_records >= limit:
                break
            text = str(payload.get("context_text") or payload.get("text") or "").strip()
            if not text:
                continue
            matched_records += 1
            artifact = self.artifact_store.write_json(
                role=ArtifactRole.WORKFLOWY_NODE_SNAPSHOT.value,
                payload={"schema_version": "workflowy_node_snapshot.v1", **payload},
                workflow_id=workflow_id,
                schema_version="workflowy_node_snapshot.v1",
            )
            base_metadata = {
                "schema_version": "embedding_chunk.v1",
                "chunker_version": CHUNKER_VERSION,
                "artifact_role": ArtifactRole.WORKFLOWY_NODE_SNAPSHOT.value,
                "source": "workflowy_chunks_jsonl",
                "source_path": source_path,
                "workflowy_chunk_idx": int(payload.get("chunk_idx", idx)),
                "headings": payload.get("headings", []),
                "path_titles": payload.get("path_titles", []),
                "top_level": payload.get("top_level"),
                "node_ids": payload.get("node_ids", []),
                "parent_ids": payload.get("parent_ids", []),
                "root_node_id": payload.get("root_node_id"),
                "created_at_min": payload.get("created_at_min"),
                "created_at_max": payload.get("created_at_max"),
                "modified_at_max": payload.get("modified_at_max"),
                "priority_min": payload.get("priority_min"),
                "priority_max": payload.get("priority_max"),
                "has_notes": payload.get("has_notes"),
                "node_count": payload.get("node_count"),
            }
            chunk_id = build_chunk_id(artifact.sha256, 0, model_id)
            imported_chunk_ids.add(chunk_id)
            existing = existing_by_id.get(chunk_id)
            if (
                existing is not None
                and existing.text_sha256 == sha256_text(text)
                and existing.embedding_model_id == model_id
                and "sub_chunk" not in (existing.metadata_json or {})
            ):
                continue
            pending.append((artifact, chunk_id, text, base_metadata))

        added = 0
        batch_size = max(1, batch_size)
        for start in range(0, len(pending), batch_size):
            batch = pending[start : start + batch_size]
            texts = [text for _, _, text, _ in batch]
            try:
                embeddings = self.model_manager.embed_texts(texts, workflow_id)
            except (httpx.HTTPError, ModelNotLoadedError):
                # llama.cpp can recycle an embedding child after a long pooled
                # request. Reload once and retry the same atomic source records.
                self.model_manager.ensure_loaded(
                    ModelRole.EMBEDDER,
                    allow_autoload=True,
                )
                embeddings = self.model_manager.embed_texts(texts, workflow_id)
            for (artifact, chunk_id, text, metadata), embedding in zip(
                batch,
                embeddings,
                strict=True,
            ):
                if self.repository.upsert_embedding_chunk(
                    chunk_id=chunk_id,
                    artifact_id=artifact.artifact_id,
                    workspace_id=workspace_id,
                    chunk_index=0,
                    text_sha256=sha256_text(text),
                    text=text,
                    embedding_model_id=model_id,
                    embedding=truncate_embedding(embedding),
                    metadata=metadata,
                ):
                    added += 1

        if limit is None:
            stale_chunk_ids = [
                row.chunk_id
                for row in self.repository.list_embedding_chunks(workspace_id)
                if row.chunk_id not in imported_chunk_ids
                and (row.metadata_json or {}).get("source") == "workflowy_chunks_jsonl"
                and (row.metadata_json or {}).get("source_path") == source_path
                and (top_level is None or (row.metadata_json or {}).get("top_level") == top_level)
            ]
            self.repository.delete_embedding_chunks(stale_chunk_ids)
        return added
