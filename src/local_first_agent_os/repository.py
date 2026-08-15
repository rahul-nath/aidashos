# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from sqlalchemy import delete, or_, select

from ._dbos_runtime import dbos_step
from .contracts import (
    ArtifactRef,
    EgressStatus,
    GraphEdge,
    GraphMention,
    GraphNeighborhood,
    GraphNode,
    GraphWriteOutcome,
    IngressEvent,
    NodeResolutionPath,
    Stage,
    WorkflowRunState,
    WorkflowStatus,
    WorkflowType,
    WorkspacePolicy,
)
from .db import (
    ArtifactRow,
    Database,
    EgressWriteRow,
    EmbeddingChunkRow,
    FallbackStateRow,
    GraphEdgeRow,
    GraphMentionRow,
    GraphNodeRow,
    IngressEventRow,
    ModelInvocationRow,
    PiTurnRow,
    SessionContextRow,
    SessionItemRow,
    ToolCallRow,
    WorkflowRunRow,
    WorkflowStageTransitionRow,
    WorkspaceRow,
    utcnow,
)
from .ids import build_egress_id


def _graph_node_from_row(row: GraphNodeRow) -> GraphNode:
    return GraphNode(
        node_id=row.node_id,
        node_type=row.node_type,
        canonical_name=row.canonical_name,
        normalized_name=row.normalized_name,
        aliases=list(row.aliases_json or []),
        properties=dict(row.properties_json or {}),
        mention_count=row.mention_count,
        needs_review=bool(row.needs_review),
        first_seen_artifact_id=row.first_seen_artifact_id,
        pagerank=row.pagerank,
        degree=row.degree,
        community_id=row.community_id,
    )


def _graph_edge_from_row(row: GraphEdgeRow) -> GraphEdge:
    return GraphEdge(
        edge_id=row.edge_id,
        src_node_id=row.src_node_id,
        dst_node_id=row.dst_node_id,
        edge_type=row.edge_type,
        confidence=row.confidence,
        weight=row.weight,
        source_artifact_ids=list(row.source_artifact_ids_json or []),
        needs_review=bool(row.needs_review),
    )


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left)) or 1.0
    right_norm = math.sqrt(sum(b * b for b in right)) or 1.0
    return dot / (left_norm * right_norm)


class Repository:
    def __init__(self, database: Database):
        self.database = database

    def create_database_schema(self) -> None:
        self.database.create_database_schema()

    def upsert_workspace(self, policy: WorkspacePolicy) -> None:
        with self.database.session() as session:
            row = session.get(WorkspaceRow, policy.workspace_id)
            payload = {
                "allowed_tools": policy.allowed_tools,
                "forbidden_tools": policy.forbidden_tools,
                "approved_workflowy_parent_ids": policy.approved_workflowy_parent_ids,
                "write_enabled": policy.write_enabled,
                "embed_medical_outputs": policy.embed_medical_outputs,
                "decision_confidence_threshold": policy.decision_confidence_threshold,
            }
            if row is None:
                session.add(
                    WorkspaceRow(
                        workspace_id=policy.workspace_id,
                        root_path=str(policy.root_path),
                        tool_policy_json=payload,
                        model_policy_json={},
                    )
                )
                return
            row.root_path = str(policy.root_path)
            row.tool_policy_json = payload
            row.updated_at = utcnow()

    @dbos_step()
    def register_ingress_event(self, event: IngressEvent) -> bool:
        with self.database.session() as session:
            if session.get(IngressEventRow, event.event_id):
                return False
            session.add(
                IngressEventRow(
                    event_id=event.event_id,
                    source_type=event.source_type.value,
                    source_uri=event.source_uri,
                    event_type=event.event_type,
                    workspace_id=event.workspace_id,
                    content_sha256=event.content_sha256,
                    detected_at=event.detected_at,
                    payload_json=event.payload,
                    status="registered",
                )
            )
            return True

    def mark_ingress_status(self, event_id: str, status: str) -> None:
        with self.database.session() as session:
            row = session.get(IngressEventRow, event_id)
            if row:
                row.status = status

    def start_workflow_run(
        self,
        workflow_id: str,
        workflow_type: str,
        workspace_id: str,
        input_event_id: str | None,
    ) -> bool:
        with self.database.session() as session:
            if session.get(WorkflowRunRow, workflow_id):
                return False
            session.add(
                WorkflowRunRow(
                    workflow_id=workflow_id,
                    workflow_type=workflow_type,
                    workspace_id=workspace_id,
                    status=WorkflowStatus.CREATED.value,
                    current_stage=Stage.REGISTERED.value,
                    input_event_id=input_event_id,
                )
            )
            session.flush()
            session.add(
                WorkflowStageTransitionRow(
                    workflow_id=workflow_id,
                    stage=Stage.REGISTERED.value,
                )
            )
            return True

    def workflow_run_exists(self, workflow_id: str) -> bool:
        with self.database.session() as session:
            return session.get(WorkflowRunRow, workflow_id) is not None

    def get_workflow_run_state(self, workflow_id: str) -> WorkflowRunState | None:
        with self.database.session() as session:
            row = session.get(WorkflowRunRow, workflow_id)
            if row is None:
                return None
            return WorkflowRunState(
                workflow_id=row.workflow_id,
                workflow_type=WorkflowType(row.workflow_type),
                status=WorkflowStatus(row.status),
                current_stage=Stage(row.current_stage),
                retry_count=row.retry_count,
                last_error=row.last_error,
            )

    def list_workflow_stage_transitions(self, workflow_id: str) -> list[Stage]:
        with self.database.session() as session:
            rows = session.scalars(
                select(WorkflowStageTransitionRow)
                .where(WorkflowStageTransitionRow.workflow_id == workflow_id)
                .order_by(WorkflowStageTransitionRow.transition_id)
            ).all()
            return [Stage(row.stage) for row in rows]

    def update_workflow(
        self,
        workflow_id: str,
        *,
        status: WorkflowStatus | str | None = None,
        stage: Stage | str | None = None,
        error: str | None = None,
        clear_error: bool = False,
        retry_increment: bool = False,
    ) -> None:
        with self.database.session() as session:
            row = session.get(WorkflowRunRow, workflow_id)
            if row is None:
                return
            if status is not None:
                row.status = status.value if isinstance(status, WorkflowStatus) else status
                if row.status in {
                    WorkflowStatus.COMPLETED.value,
                    WorkflowStatus.MANUAL_REVIEW.value,
                    WorkflowStatus.FAILED_PERMANENT.value,
                    WorkflowStatus.UNSUPPORTED_STUB.value,
                    WorkflowStatus.CANCELLED.value,
                }:
                    row.completed_at = utcnow()
            if stage is not None:
                next_stage = stage.value if isinstance(stage, Stage) else stage
                if row.current_stage != next_stage:
                    row.current_stage = next_stage
                    session.add(
                        WorkflowStageTransitionRow(
                            workflow_id=workflow_id,
                            stage=next_stage,
                        )
                    )
            if error is not None:
                row.last_error = error
            elif clear_error:
                row.last_error = None
            if retry_increment:
                row.retry_count += 1
            row.updated_at = utcnow()

    def insert_artifact(self, artifact: ArtifactRef, workflow_id: str | None) -> bool:
        with self.database.session() as session:
            if session.get(ArtifactRow, artifact.artifact_id):
                return False
            session.add(
                ArtifactRow(
                    artifact_id=artifact.artifact_id,
                    workflow_id=workflow_id,
                    role=str(artifact.role),
                    uri=artifact.uri,
                    sha256=artifact.sha256,
                    mime_type=artifact.mime_type,
                    size_bytes=artifact.size_bytes,
                    schema_version=artifact.schema_version,
                )
            )
            return True

    def get_artifact(self, artifact_id: str) -> ArtifactRef | None:
        with self.database.session() as session:
            row = session.get(ArtifactRow, artifact_id)
            if row is None:
                return None
            return ArtifactRef(
                artifact_id=row.artifact_id,
                role=row.role,
                uri=row.uri,
                sha256=row.sha256,
                mime_type=row.mime_type,
                size_bytes=row.size_bytes,
                schema_version=row.schema_version,
            )

    def list_artifacts_by_role(
        self,
        roles: list[str],
        limit: int | None = 1000,
    ) -> list[ArtifactRef]:
        with self.database.session() as session:
            stmt = (
                select(ArtifactRow)
                .where(ArtifactRow.role.in_(roles))
                .order_by(ArtifactRow.created_at, ArtifactRow.artifact_id)
            )
            if limit is not None:
                stmt = stmt.limit(limit)
            rows = session.scalars(stmt).all()
            return [
                ArtifactRef(
                    artifact_id=row.artifact_id,
                    role=row.role,
                    uri=row.uri,
                    sha256=row.sha256,
                    mime_type=row.mime_type,
                    size_bytes=row.size_bytes,
                    schema_version=row.schema_version,
                )
                for row in rows
            ]

    def list_workflow_artifacts(
        self,
        workflow_id: str,
        *,
        roles: list[str] | None = None,
    ) -> list[ArtifactRef]:
        with self.database.session() as session:
            stmt = (
                select(ArtifactRow)
                .where(ArtifactRow.workflow_id == workflow_id)
                .order_by(ArtifactRow.created_at, ArtifactRow.artifact_id)
            )
            if roles is not None:
                stmt = stmt.where(ArtifactRow.role.in_(roles))
            rows = session.scalars(stmt).all()
            return [
                ArtifactRef(
                    artifact_id=row.artifact_id,
                    role=row.role,
                    uri=row.uri,
                    sha256=row.sha256,
                    mime_type=row.mime_type,
                    size_bytes=row.size_bytes,
                    schema_version=row.schema_version,
                )
                for row in rows
            ]

    def latest_artifact_by_role(self, role: str) -> ArtifactRef | None:
        with self.database.session() as session:
            row = session.scalars(
                select(ArtifactRow)
                .where(ArtifactRow.role == role)
                .order_by(ArtifactRow.created_at.desc())
                .limit(1)
            ).first()
            if row is None:
                return None
            return ArtifactRef(
                artifact_id=row.artifact_id,
                role=row.role,
                uri=row.uri,
                sha256=row.sha256,
                mime_type=row.mime_type,
                size_bytes=row.size_bytes,
                schema_version=row.schema_version,
            )

    def record_model_invocation(
        self,
        *,
        invocation_id: str,
        workflow_id: str,
        model_role: str,
        model_id: str,
        input_artifact_id: str,
        params: dict[str, Any],
        output_artifact_id: str | None,
        latency_ms: int | None,
        status: str,
        error: str | None = None,
    ) -> None:
        with self.database.session() as session:
            row = session.get(ModelInvocationRow, invocation_id)
            if row is None:
                session.add(
                    ModelInvocationRow(
                        invocation_id=invocation_id,
                        workflow_id=workflow_id,
                        model_role=model_role,
                        model_id=model_id,
                        input_artifact_id=input_artifact_id,
                        params_json=params,
                        output_artifact_id=output_artifact_id,
                        latency_ms=latency_ms,
                        status=status,
                        error=error,
                    )
                )
                return
            row.output_artifact_id = output_artifact_id
            row.latency_ms = latency_ms
            row.status = status
            row.error = error

    @dbos_step()
    def record_pi_turn(
        self,
        *,
        pi_turn_id: str,
        workflow_id: str,
        workspace_id: str,
        prompt_artifact_id: str,
        allowed_tools: list[str],
        decision_schema: str,
        output_artifact_id: str | None,
        status: str,
    ) -> None:
        with self.database.session() as session:
            existing = session.get(PiTurnRow, pi_turn_id)
            if existing:
                existing.output_artifact_id = output_artifact_id
                existing.status = status
                return
            session.add(
                PiTurnRow(
                    pi_turn_id=pi_turn_id,
                    workflow_id=workflow_id,
                    workspace_id=workspace_id,
                    prompt_artifact_id=prompt_artifact_id,
                    allowed_tools_json=allowed_tools,
                    decision_schema=decision_schema,
                    output_artifact_id=output_artifact_id,
                    status=status,
                )
            )

    @dbos_step()
    def record_tool_call(
        self,
        *,
        tool_call_id: str,
        workflow_id: str,
        tool_name: str,
        input_json: dict[str, Any],
        output_json: dict[str, Any],
        status: str,
        pi_turn_id: str | None = None,
        started_at: datetime | None = None,
    ) -> None:
        with self.database.session() as session:
            if session.get(ToolCallRow, tool_call_id):
                return
            session.add(
                ToolCallRow(
                    tool_call_id=tool_call_id,
                    pi_turn_id=pi_turn_id,
                    workflow_id=workflow_id,
                    tool_name=tool_name,
                    input_json=input_json,
                    output_json=output_json,
                    status=status,
                    started_at=started_at or utcnow(),
                    finished_at=utcnow(),
                )
            )

    def upsert_embedding_chunk(
        self,
        *,
        chunk_id: str,
        artifact_id: str,
        workspace_id: str,
        chunk_index: int,
        text_sha256: str,
        text: str,
        embedding_model_id: str,
        embedding: list[float] | None,
        metadata: dict[str, Any],
    ) -> bool:
        with self.database.session() as session:
            row = session.get(EmbeddingChunkRow, chunk_id)
            if row is not None:
                row.artifact_id = artifact_id
                row.workspace_id = workspace_id
                row.chunk_index = chunk_index
                row.text_sha256 = text_sha256
                row.text = text
                row.embedding_model_id = embedding_model_id
                row.embedding = embedding
                row.metadata_json = metadata
                return False
            session.add(
                EmbeddingChunkRow(
                    chunk_id=chunk_id,
                    artifact_id=artifact_id,
                    workspace_id=workspace_id,
                    chunk_index=chunk_index,
                    text_sha256=text_sha256,
                    text=text,
                    embedding_model_id=embedding_model_id,
                    embedding=embedding,
                    metadata_json=metadata,
                )
            )
            return True

    def list_embedding_chunks(self, workspace_id: str | None = None) -> list[EmbeddingChunkRow]:
        with self.database.session() as session:
            stmt = select(EmbeddingChunkRow)
            if workspace_id is not None:
                stmt = stmt.where(EmbeddingChunkRow.workspace_id == workspace_id)
            return list(session.scalars(stmt).all())

    def delete_embedding_chunks(self, chunk_ids: list[str]) -> int:
        if not chunk_ids:
            return 0
        with self.database.session() as session:
            result = session.execute(
                delete(EmbeddingChunkRow).where(EmbeddingChunkRow.chunk_id.in_(chunk_ids))
            )
            return int(getattr(result, "rowcount", 0) or 0)

    def search_embedding_chunks(
        self,
        query_embedding: list[float],
        workspace_id: str | None,
        top_k: int,
    ) -> list[tuple[EmbeddingChunkRow, float]]:
        """Return the top_k chunks ranked by cosine similarity to the query.

        On postgres this is an HNSW-indexed `ORDER BY embedding <=> q` scan; on
        sqlite (tests) it falls back to scoring every chunk in Python.
        """
        if self.database.engine.dialect.name == "postgresql":
            distance = EmbeddingChunkRow.embedding.cosine_distance(query_embedding)
            with self.database.session() as session:
                stmt = select(EmbeddingChunkRow, distance.label("distance"))
                if workspace_id is not None:
                    stmt = stmt.where(EmbeddingChunkRow.workspace_id == workspace_id)
                stmt = stmt.order_by(distance).limit(top_k)
                return [(row, 1.0 - float(dist)) for row, dist in session.execute(stmt).all()]
        rows = self.list_embedding_chunks(workspace_id)
        scored = [
            (row, cosine_similarity(query_embedding, list(row.embedding or []))) for row in rows
        ]
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]

    @dbos_step()
    def create_or_get_egress(
        self,
        *,
        workflow_id: str,
        egress_type: str,
        destination_uri: str,
        content_sha256: str,
        request_json: dict[str, Any],
    ) -> tuple[str, bool]:
        egress_id = build_egress_id(egress_type, destination_uri, content_sha256)
        with self.database.session() as session:
            existing = session.scalar(
                select(EgressWriteRow).where(
                    EgressWriteRow.egress_type == egress_type,
                    EgressWriteRow.destination_uri == destination_uri,
                    EgressWriteRow.content_sha256 == content_sha256,
                )
            )
            if existing is not None:
                if existing.status == EgressStatus.COMPLETED.value:
                    existing.status = EgressStatus.DEDUPED.value
                return existing.egress_id, False
            session.add(
                EgressWriteRow(
                    egress_id=egress_id,
                    workflow_id=workflow_id,
                    egress_type=egress_type,
                    destination_uri=destination_uri,
                    content_sha256=content_sha256,
                    request_json=request_json,
                    status=EgressStatus.PENDING.value,
                )
            )
            return egress_id, True

    @dbos_step()
    def update_egress(
        self,
        egress_id: str,
        *,
        status: EgressStatus | str,
        response_json: dict[str, Any] | None = None,
    ) -> None:
        with self.database.session() as session:
            row = session.get(EgressWriteRow, egress_id)
            if row is None:
                return
            row.status = status.value if isinstance(status, EgressStatus) else status
            row.response_json = response_json
            if row.status in {
                EgressStatus.COMPLETED.value,
                EgressStatus.DEDUPED.value,
                EgressStatus.DENIED.value,
                EgressStatus.FAILED.value,
            }:
                row.completed_at = utcnow()

    def get_fallback_state(self, name: str = "default_role") -> dict[str, str | None] | None:
        with self.database.session() as session:
            row = session.get(FallbackStateRow, name)
            if row is None:
                return None
            return {"fallback_role": row.fallback_role, "reason": row.reason}

    def set_fallback_state(
        self,
        *,
        name: str,
        fallback_role: str | None,
        reason: str | None,
    ) -> None:
        with self.database.session() as session:
            row = session.get(FallbackStateRow, name)
            if row is None:
                session.add(
                    FallbackStateRow(
                        name=name,
                        fallback_role=fallback_role,
                        reason=reason,
                    )
                )
                return
            row.fallback_role = fallback_role
            row.reason = reason
            row.updated_at = utcnow()

    def get_session_context(self, session_id: str, model_id: str) -> dict[str, Any] | None:
        with self.database.session() as session:
            row = session.get(
                SessionContextRow,
                {"session_id": session_id, "model_id": model_id},
            )
            if row is None:
                return None
            return {
                "session_id": row.session_id,
                "model_id": row.model_id,
                "active_context_artifact_id": row.active_context_artifact_id,
                "compacted_summary_artifact_id": row.compacted_summary_artifact_id,
                "snapshot_item_id": row.snapshot_item_id,
                "token_count": row.token_count,
                "max_window_tokens": row.max_window_tokens,
                "export_path": row.export_path,
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
            }

    def upsert_session_context(
        self,
        *,
        session_id: str,
        model_id: str,
        active_context_artifact_id: str | None,
        compacted_summary_artifact_id: str | None,
        snapshot_item_id: str | None,
        token_count: int,
        max_window_tokens: int | None,
        export_path: str | None,
    ) -> None:
        with self.database.session() as session:
            row = session.get(
                SessionContextRow,
                {"session_id": session_id, "model_id": model_id},
            )
            if row is None:
                session.add(
                    SessionContextRow(
                        session_id=session_id,
                        model_id=model_id,
                        active_context_artifact_id=active_context_artifact_id,
                        compacted_summary_artifact_id=compacted_summary_artifact_id,
                        snapshot_item_id=snapshot_item_id,
                        token_count=token_count,
                        max_window_tokens=max_window_tokens,
                        export_path=export_path,
                    )
                )
                return
            row.active_context_artifact_id = active_context_artifact_id
            row.compacted_summary_artifact_id = compacted_summary_artifact_id
            row.snapshot_item_id = snapshot_item_id
            row.token_count = token_count
            row.max_window_tokens = max_window_tokens
            row.export_path = export_path
            row.updated_at = utcnow()

    def list_session_contexts(self, session_id: str | None = None) -> list[dict[str, Any]]:
        with self.database.session() as session:
            stmt = select(SessionContextRow)
            if session_id is not None:
                stmt = stmt.where(SessionContextRow.session_id == session_id)
            rows = session.scalars(stmt).all()
            return [
                {
                    "session_id": row.session_id,
                    "model_id": row.model_id,
                    "active_context_artifact_id": row.active_context_artifact_id,
                    "compacted_summary_artifact_id": row.compacted_summary_artifact_id,
                    "snapshot_item_id": row.snapshot_item_id,
                    "token_count": row.token_count,
                    "max_window_tokens": row.max_window_tokens,
                    "export_path": row.export_path,
                    "created_at": row.created_at.isoformat(),
                    "updated_at": row.updated_at.isoformat(),
                }
                for row in rows
            ]

    @dbos_step()
    def append_session_item(
        self,
        *,
        item_id: str,
        turn_id: str,
        session_id: str,
        model_id: str,
        ordinal: int,
        item_type: str,
        role: str,
        content: str,
        metadata: dict[str, Any],
        created_at: datetime,
    ) -> dict[str, Any]:
        with self.database.session() as session:
            if session.get(SessionItemRow, item_id) is not None:
                return {"turn_id": turn_id, "item_id": item_id, "inserted": False}
            session.add(
                SessionItemRow(
                    item_id=item_id,
                    session_id=session_id,
                    model_id=model_id,
                    turn_id=turn_id,
                    ordinal=ordinal,
                    item_type=item_type,
                    role=role,
                    content=content,
                    metadata_json=metadata,
                    created_at=created_at,
                )
            )
        return {"turn_id": turn_id, "item_id": item_id, "inserted": True}

    def list_session_items(
        self,
        session_id: str,
        model_id: str,
        *,
        after_item_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.scalars(
                select(SessionItemRow)
                .where(
                    SessionItemRow.session_id == session_id,
                    SessionItemRow.model_id == model_id,
                )
                .order_by(
                    SessionItemRow.created_at,
                    SessionItemRow.turn_id,
                    SessionItemRow.ordinal,
                )
            ).all()
            if after_item_id is not None:
                marker_index = next(
                    (index for index, row in enumerate(rows) if row.item_id == after_item_id),
                    None,
                )
                if marker_index is not None:
                    rows = rows[marker_index + 1 :]
            return [
                {
                    "item_id": row.item_id,
                    "session_id": row.session_id,
                    "model_id": row.model_id,
                    "turn_id": row.turn_id,
                    "ordinal": row.ordinal,
                    "item_type": row.item_type,
                    "role": row.role,
                    "content": row.content,
                    "metadata": row.metadata_json,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]

    def latest_session_item_id(self, session_id: str, model_id: str) -> str | None:
        with self.database.session() as session:
            row = session.scalar(
                select(SessionItemRow)
                .where(
                    SessionItemRow.session_id == session_id,
                    SessionItemRow.model_id == model_id,
                )
                .order_by(
                    SessionItemRow.created_at.desc(),
                    SessionItemRow.turn_id.desc(),
                    SessionItemRow.ordinal.desc(),
                )
                .limit(1)
            )
            return row.item_id if row is not None else None

    def list_pending_workflow_runs(self) -> list[tuple[str, str, str | None]]:
        with self.database.session() as session:
            rows = session.scalars(
                select(WorkflowRunRow).where(
                    WorkflowRunRow.status.in_(
                        [
                            WorkflowStatus.CREATED.value,
                            WorkflowStatus.PROCESSING.value,
                        ]
                    )
                )
            ).all()
            return [(row.workflow_id, row.workflow_type, row.input_event_id) for row in rows]

    def get_ingress_event(self, event_id: str) -> IngressEvent | None:
        with self.database.session() as session:
            row = session.get(IngressEventRow, event_id)
            if row is None:
                return None
            from .contracts import SourceType

            return IngressEvent(
                event_id=row.event_id,
                source_type=SourceType(row.source_type),
                event_type=row.event_type,
                workspace_id=row.workspace_id,
                source_uri=row.source_uri,
                content_sha256=row.content_sha256,
                detected_at=row.detected_at,
                payload=row.payload_json or {},
            )

    def list_workflows(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.database.session() as session:
            rows = session.scalars(
                select(WorkflowRunRow).order_by(WorkflowRunRow.updated_at.desc()).limit(limit)
            ).all()
            return [
                {
                    "workflow_id": row.workflow_id,
                    "workflow_type": row.workflow_type,
                    "workspace_id": row.workspace_id,
                    "status": row.status,
                    "current_stage": row.current_stage,
                    "last_error": row.last_error,
                    "retry_count": row.retry_count,
                    "updated_at": row.updated_at.isoformat(),
                }
                for row in rows
            ]

    def dashboard_summary(self) -> dict[str, Any]:
        with self.database.session() as session:
            workflows = session.scalars(select(WorkflowRunRow)).all()
            egress = session.scalars(select(EgressWriteRow)).all()
            chunks = session.scalars(select(EmbeddingChunkRow)).all()
            return {
                "workflow_count": len(workflows),
                "manual_review_queue_depth": sum(
                    1 for row in workflows if row.status == WorkflowStatus.MANUAL_REVIEW.value
                ),
                "failed_workflow_count": sum(
                    1 for row in workflows if row.status.startswith("FAILED")
                ),
                "embedding_chunk_count": len(chunks),
                "egress_write_count": len(egress),
                "deduped_egress_count": sum(
                    1 for row in egress if row.status == EgressStatus.DEDUPED.value
                ),
                "recent_workflows": [
                    {
                        "workflow_id": row.workflow_id,
                        "workflow_type": row.workflow_type,
                        "workspace_id": row.workspace_id,
                        "status": row.status,
                        "current_stage": row.current_stage,
                        "updated_at": row.updated_at.isoformat(),
                    }
                    for row in sorted(
                        workflows,
                        key=lambda item: item.updated_at,
                        reverse=True,
                    )[:10]
                ],
            }

    # ------------------------------------------------------------------
    # Knowledge graph (derived index)
    #
    # The graph holds nothing that is not re-derivable from the immutable
    # `entity_graph.v1` artifacts, so every method here is safe to replay and
    # `drop_graph` is a supported recovery rather than data loss.
    # ------------------------------------------------------------------

    def resolve_graph_node(
        self,
        *,
        node_type: str,
        normalized_name: str,
        embedding: list[float] | None,
        resolution_threshold: float,
    ) -> tuple[str | None, NodeResolutionPath]:
        """Find the node an extracted entity belongs to, if any.

        Exact `normalized_name` first, then embedding similarity above the
        threshold. Both are scoped to `node_type`: two entities of different
        types never resolve onto each other however close their embeddings.
        """
        with self.database.session() as session:
            exact = session.execute(
                select(GraphNodeRow).where(
                    GraphNodeRow.node_type == node_type,
                    GraphNodeRow.normalized_name == normalized_name,
                )
            ).scalar_one_or_none()
            if exact is not None:
                return exact.node_id, NodeResolutionPath.EXACT
            if not embedding:
                return None, NodeResolutionPath.NEW
            candidates = (
                session.execute(select(GraphNodeRow).where(GraphNodeRow.node_type == node_type))
                .scalars()
                .all()
            )
        best_id: str | None = None
        best_score = resolution_threshold
        for candidate in candidates:
            score = cosine_similarity(embedding, list(candidate.embedding_json or []))
            if score >= best_score:
                best_id, best_score = candidate.node_id, score
        if best_id is None:
            return None, NodeResolutionPath.NEW
        return best_id, NodeResolutionPath.EMBEDDING

    @dbos_step()
    def upsert_graph_node(
        self,
        *,
        node_id: str,
        node_type: str,
        canonical_name: str,
        normalized_name: str,
        artifact_id: str,
        embedding: list[float] | None = None,
        properties: dict[str, Any] | None = None,
        needs_review: bool = False,
        alias: str | None = None,
    ) -> GraphWriteOutcome:
        """Create the node or fold this assertion into the existing one.

        On merge the surface form becomes an alias rather than a second node,
        and `mention_count` counts distinct source artifacts, not assertions.
        """
        with self.database.session() as session:
            row = session.get(GraphNodeRow, node_id)
            if row is None:
                session.add(
                    GraphNodeRow(
                        node_id=node_id,
                        node_type=node_type,
                        canonical_name=canonical_name,
                        normalized_name=normalized_name,
                        aliases_json=[],
                        properties_json=properties or {},
                        embedding_json=embedding,
                        mention_count=0,
                        needs_review=needs_review,
                        first_seen_artifact_id=artifact_id,
                    )
                )
                return GraphWriteOutcome.CREATED
            if alias and alias != row.canonical_name and alias not in (row.aliases_json or []):
                row.aliases_json = [*(row.aliases_json or []), alias]
            if properties:
                row.properties_json = {**(row.properties_json or {}), **properties}
            if row.embedding_json is None and embedding:
                row.embedding_json = embedding
            # Quarantine is sticky downward only: one confident assertion is
            # enough to clear a node that a weaker one flagged.
            if not needs_review:
                row.needs_review = False
            row.updated_at = utcnow()
            return GraphWriteOutcome.MERGED

    @dbos_step()
    def upsert_graph_edge(
        self,
        *,
        edge_id: str,
        src_node_id: str,
        dst_node_id: str,
        edge_type: str,
        confidence: float,
        artifact_id: str,
        needs_review: bool = False,
    ) -> GraphWriteOutcome:
        """Create the edge or thicken it.

        Re-assertion raises `weight`, keeps the highest confidence seen, and
        appends provenance, so a repeated claim never duplicates the edge.
        """
        with self.database.session() as session:
            row = session.get(GraphEdgeRow, edge_id)
            if row is None:
                session.add(
                    GraphEdgeRow(
                        edge_id=edge_id,
                        src_node_id=src_node_id,
                        dst_node_id=dst_node_id,
                        edge_type=edge_type,
                        confidence=confidence,
                        weight=1,
                        source_artifact_ids_json=[artifact_id],
                        needs_review=needs_review,
                    )
                )
                return GraphWriteOutcome.CREATED
            if artifact_id not in (row.source_artifact_ids_json or []):
                row.source_artifact_ids_json = [
                    *(row.source_artifact_ids_json or []),
                    artifact_id,
                ]
                row.weight = row.weight + 1
            row.confidence = max(row.confidence, confidence)
            if not needs_review:
                row.needs_review = False
            row.updated_at = utcnow()
            return GraphWriteOutcome.MERGED

    @dbos_step()
    def upsert_graph_mention(
        self,
        *,
        mention_id: str,
        node_id: str,
        artifact_id: str,
        chunk_id: str | None,
        snippet: str,
    ) -> bool:
        """Record that an artifact mentions a node. Returns True if new."""
        with self.database.session() as session:
            if session.get(GraphMentionRow, mention_id) is not None:
                return False
            session.add(
                GraphMentionRow(
                    mention_id=mention_id,
                    node_id=node_id,
                    artifact_id=artifact_id,
                    chunk_id=chunk_id or "",
                    snippet=snippet,
                )
            )
            node = session.get(GraphNodeRow, node_id)
            if node is not None:
                node.mention_count = node.mention_count + 1
            return True

    def get_graph_node(self, node_id: str) -> GraphNode | None:
        with self.database.session() as session:
            row = session.get(GraphNodeRow, node_id)
            return None if row is None else _graph_node_from_row(row)

    def list_graph_nodes(self, *, needs_review: bool | None = None) -> list[GraphNode]:
        with self.database.session() as session:
            stmt = select(GraphNodeRow)
            if needs_review is not None:
                stmt = stmt.where(GraphNodeRow.needs_review == needs_review)
            rows = session.execute(stmt.order_by(GraphNodeRow.node_id)).scalars().all()
            return [_graph_node_from_row(row) for row in rows]

    def list_graph_edges(self, *, needs_review: bool | None = None) -> list[GraphEdge]:
        with self.database.session() as session:
            stmt = select(GraphEdgeRow)
            if needs_review is not None:
                stmt = stmt.where(GraphEdgeRow.needs_review == needs_review)
            rows = session.execute(stmt.order_by(GraphEdgeRow.edge_id)).scalars().all()
            return [_graph_edge_from_row(row) for row in rows]

    def list_graph_mentions(self, node_id: str | None = None) -> list[GraphMention]:
        with self.database.session() as session:
            stmt = select(GraphMentionRow)
            if node_id is not None:
                stmt = stmt.where(GraphMentionRow.node_id == node_id)
            rows = session.execute(stmt.order_by(GraphMentionRow.mention_id)).scalars().all()
            return [
                GraphMention(
                    mention_id=row.mention_id,
                    node_id=row.node_id,
                    artifact_id=row.artifact_id,
                    chunk_id=row.chunk_id or None,
                    snippet=row.snippet,
                )
                for row in rows
            ]

    def graph_seed_nodes(
        self,
        *,
        chunk_ids: list[str],
        artifact_ids: list[str],
    ) -> list[str]:
        """Map vector hits onto graph nodes through the mention bridge.

        This is the whole reason the bridge stays relational: it is a plain
        join against pgvector-world identifiers, not a traversal.
        """
        # A mention with no chunk stores "" rather than NULL, so an empty
        # chunk_ids list must contribute no predicate at all: matching on ""
        # would seed every chunkless mention in the graph.
        predicates = []
        if chunk_ids:
            predicates.append(GraphMentionRow.chunk_id.in_(chunk_ids))
        if artifact_ids:
            predicates.append(GraphMentionRow.artifact_id.in_(artifact_ids))
        if not predicates:
            return []
        with self.database.session() as session:
            stmt = select(GraphMentionRow.node_id).where(or_(*predicates))
            return sorted({row for row in session.execute(stmt).scalars().all()})

    def graph_neighborhood(
        self,
        *,
        seed_node_ids: list[str],
        max_hops: int,
        max_neighbors: int,
    ) -> GraphNeighborhood:
        """Expand a seed set outward, bounded by hops and by neighbor count.

        Expansion is breadth-first so that `max_neighbors` truncates the
        farthest nodes rather than an arbitrary slice: a partial neighborhood
        should still be the closest part of it.
        """
        if not seed_node_ids:
            return GraphNeighborhood()
        with self.database.session() as session:
            edges = session.execute(select(GraphEdgeRow)).scalars().all()
            adjacency: dict[str, list[GraphEdgeRow]] = {}
            for edge in edges:
                adjacency.setdefault(edge.src_node_id, []).append(edge)
                adjacency.setdefault(edge.dst_node_id, []).append(edge)

            seen: set[str] = set(seed_node_ids)
            touched_edges: dict[str, GraphEdgeRow] = {}
            hops: dict[str, int] = {node_id: 0 for node_id in seed_node_ids}
            frontier = list(seed_node_ids)
            neighbors_added = 0
            for depth in range(1, max_hops + 1):
                next_frontier: list[str] = []
                for node_id in frontier:
                    for edge in sorted(adjacency.get(node_id, []), key=lambda e: e.edge_id):
                        other = (
                            edge.dst_node_id if edge.src_node_id == node_id else edge.src_node_id
                        )
                        if other in seen:
                            touched_edges[edge.edge_id] = edge
                            continue
                        if neighbors_added >= max_neighbors:
                            break
                        seen.add(other)
                        hops[other] = depth
                        touched_edges[edge.edge_id] = edge
                        neighbors_added += 1
                        next_frontier.append(other)
                    if neighbors_added >= max_neighbors:
                        break
                frontier = next_frontier
                if not frontier:
                    break

            node_rows = (
                session.execute(select(GraphNodeRow).where(GraphNodeRow.node_id.in_(sorted(seen))))
                .scalars()
                .all()
            )
            mention_rows = (
                session.execute(
                    select(GraphMentionRow).where(GraphMentionRow.node_id.in_(sorted(seen)))
                )
                .scalars()
                .all()
            )
            nodes = [
                {**_graph_node_from_row(row).model_dump(), "hops": hops.get(row.node_id, 0)}
                for row in sorted(node_rows, key=lambda r: (hops.get(r.node_id, 0), r.node_id))
            ]
            return GraphNeighborhood(
                seed_node_ids=sorted(seed_node_ids),
                nodes=nodes,
                edges=[
                    _graph_edge_from_row(edge).model_dump()
                    for edge in sorted(touched_edges.values(), key=lambda e: e.edge_id)
                ],
                mention_snippets=[
                    {
                        "node_id": row.node_id,
                        "artifact_id": row.artifact_id,
                        "snippet": row.snippet,
                    }
                    for row in sorted(mention_rows, key=lambda r: r.mention_id)
                ],
            )

    @dbos_step()
    def set_graph_node_metrics(
        self,
        *,
        node_id: str,
        pagerank: float,
        degree: int,
        community_id: int,
    ) -> None:
        with self.database.session() as session:
            row = session.get(GraphNodeRow, node_id)
            if row is None:
                return
            row.pagerank = pagerank
            row.degree = degree
            row.community_id = community_id
            row.updated_at = utcnow()

    def graph_stats(self, *, top_n: int = 5) -> dict[str, Any]:
        with self.database.session() as session:
            nodes = session.execute(select(GraphNodeRow)).scalars().all()
            edge_count = len(session.execute(select(GraphEdgeRow)).scalars().all())
            ranked = sorted(
                nodes,
                key=lambda row: (-(row.pagerank or 0.0), row.node_id),
            )
            communities = {row.community_id for row in nodes if row.community_id is not None}
            return {
                "node_count": len(nodes),
                "edge_count": edge_count,
                "community_count": len(communities),
                "needs_review_count": sum(1 for row in nodes if row.needs_review),
                "top_nodes": [
                    {
                        "node_id": row.node_id,
                        "canonical_name": row.canonical_name,
                        "node_type": row.node_type,
                        "pagerank": row.pagerank,
                    }
                    for row in ranked[:top_n]
                ],
            }

    @dbos_step()
    def drop_graph(self) -> None:
        """Discard the whole derived graph.

        Safe by construction: `pi /graph rebuild` re-derives every row from the
        `entity_graph.v1` artifacts, which this never touches.
        """
        with self.database.session() as session:
            session.execute(delete(GraphMentionRow))
            session.execute(delete(GraphEdgeRow))
            session.execute(delete(GraphNodeRow))
