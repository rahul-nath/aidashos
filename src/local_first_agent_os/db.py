# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.types import JSON

from .settings import Settings

# Embedding width stored and indexed. The embedder (Qwen3-Embedding-8B) emits
# 4096-dim vectors; pgvector's HNSW index caps at 2000 dims for `vector` and
# 4000 for `halfvec`, so embeddings are Matryoshka-truncated to this width and
# stored as halfvec to stay within the indexable range.
EMBEDDING_DIM = 2048


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class WorkspaceRow(Base):
    __tablename__ = "workspaces"

    workspace_id: Mapped[str] = mapped_column(String, primary_key=True)
    root_path: Mapped[str] = mapped_column(Text)
    tool_policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    model_policy_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class IngressEventRow(Base):
    __tablename__ = "ingress_events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    source_type: Mapped[str] = mapped_column(String, index=True)
    source_uri: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(String)
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    content_sha256: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, default="registered")


class WorkflowRunRow(Base):
    __tablename__ = "workflow_runs"

    workflow_id: Mapped[str] = mapped_column(String, primary_key=True)
    workflow_type: Mapped[str] = mapped_column(String, index=True)
    workspace_id: Mapped[str] = mapped_column(String, ForeignKey("workspaces.workspace_id"))
    status: Mapped[str] = mapped_column(String, index=True)
    current_stage: Mapped[str] = mapped_column(String, index=True)
    input_event_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("ingress_events.event_id"), nullable=True
    )
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkflowStageTransitionRow(Base):
    """One durable workflow-stage transition, in observed order."""

    __tablename__ = "workflow_stage_transitions"

    transition_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("workflow_runs.workflow_id", ondelete="CASCADE"),
        index=True,
    )
    stage: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    artifact_id: Mapped[str] = mapped_column(String, primary_key=True)
    workflow_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("workflow_runs.workflow_id"), nullable=True, index=True
    )
    role: Mapped[str] = mapped_column(String, index=True)
    uri: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String, index=True)
    mime_type: Mapped[str] = mapped_column(String)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    schema_version: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ModelInvocationRow(Base):
    __tablename__ = "model_invocations"

    invocation_id: Mapped[str] = mapped_column(String, primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String, ForeignKey("workflow_runs.workflow_id"))
    model_role: Mapped[str] = mapped_column(String, index=True)
    model_id: Mapped[str] = mapped_column(String)
    input_artifact_id: Mapped[str] = mapped_column(String)
    params_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_artifact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PiTurnRow(Base):
    __tablename__ = "pi_turns"

    pi_turn_id: Mapped[str] = mapped_column(String, primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String, ForeignKey("workflow_runs.workflow_id"))
    workspace_id: Mapped[str] = mapped_column(String)
    prompt_artifact_id: Mapped[str] = mapped_column(String)
    allowed_tools_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    decision_schema: Mapped[str] = mapped_column(String)
    output_artifact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ToolCallRow(Base):
    __tablename__ = "tool_calls"

    tool_call_id: Mapped[str] = mapped_column(String, primary_key=True)
    pi_turn_id: Mapped[str | None] = mapped_column(String, nullable=True)
    workflow_id: Mapped[str] = mapped_column(String, ForeignKey("workflow_runs.workflow_id"))
    tool_name: Mapped[str] = mapped_column(String, index=True)
    input_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    output_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EmbeddingChunkRow(Base):
    __tablename__ = "embedding_chunks"

    chunk_id: Mapped[str] = mapped_column(String, primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String, ForeignKey("artifacts.artifact_id"))
    workspace_id: Mapped[str] = mapped_column(String, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    text_sha256: Mapped[str] = mapped_column(String, index=True)
    text: Mapped[str] = mapped_column(Text)
    embedding_model_id: Mapped[str] = mapped_column(String, index=True)
    embedding: Mapped[list[float] | None] = mapped_column(
        HALFVEC(EMBEDDING_DIM).with_variant(JSON(), "sqlite"),
        nullable=True,
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# pgvector wiring: the `vector` extension must exist before the halfvec column
# is created, and an HNSW index over `embedding` gives sublinear cosine search.
# Both are postgres-only; on sqlite the column degrades to plain JSON.
event.listen(
    Base.metadata,
    "before_create",
    DDL("CREATE EXTENSION IF NOT EXISTS vector").execute_if(dialect="postgresql"),
)
event.listen(
    EmbeddingChunkRow.__table__,
    "after_create",
    DDL(
        "CREATE INDEX IF NOT EXISTS embedding_chunks_embedding_hnsw "
        "ON embedding_chunks USING hnsw (embedding halfvec_cosine_ops)"
    ).execute_if(dialect="postgresql"),
)


class GraphNodeRow(Base):
    """One resolved entity in the derived knowledge graph.

    `node_id` is hash(node_type + normalized_name), so the primary key *is* the
    dedup rule: re-asserting an entity can only ever hit the same row.
    """

    __tablename__ = "graph_nodes"

    node_id: Mapped[str] = mapped_column(String, primary_key=True)
    node_type: Mapped[str] = mapped_column(String, index=True)
    canonical_name: Mapped[str] = mapped_column(Text)
    normalized_name: Mapped[str] = mapped_column(String, index=True)
    aliases_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    properties_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    embedding_json: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    mention_count: Mapped[int] = mapped_column(Integer, default=0)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    first_seen_artifact_id: Mapped[str] = mapped_column(String, default="")
    pagerank: Mapped[float | None] = mapped_column(Float, nullable=True)
    degree: Mapped[int | None] = mapped_column(Integer, nullable=True)
    community_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class GraphEdgeRow(Base):
    """One directed, typed assertion between two nodes.

    `edge_id` is hash(src + edge_type + dst); direction is inside the key, so
    the reverse assertion is a distinct edge rather than a merge.
    """

    __tablename__ = "graph_edges"

    edge_id: Mapped[str] = mapped_column(String, primary_key=True)
    src_node_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("graph_nodes.node_id", ondelete="CASCADE"),
        index=True,
    )
    dst_node_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("graph_nodes.node_id", ondelete="CASCADE"),
        index=True,
    )
    edge_type: Mapped[str] = mapped_column(String, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    source_artifact_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class GraphMentionRow(Base):
    """The bridge from graph nodes back to pgvector-world rows.

    This is what makes GraphRAG cheap: given vector hits, look up the touched
    node ids with a plain relational join before any traversal happens.
    """

    __tablename__ = "graph_node_mentions"
    __table_args__ = (
        UniqueConstraint("node_id", "artifact_id", "chunk_id", name="graph_node_mentions_unique"),
    )

    mention_id: Mapped[str] = mapped_column(String, primary_key=True)
    node_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("graph_nodes.node_id", ondelete="CASCADE"),
        index=True,
    )
    artifact_id: Mapped[str] = mapped_column(
        String, ForeignKey("artifacts.artifact_id"), index=True
    )
    chunk_id: Mapped[str] = mapped_column(String, default="", index=True)
    snippet: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EgressWriteRow(Base):
    __tablename__ = "egress_writes"
    __table_args__ = (
        UniqueConstraint(
            "egress_type",
            "destination_uri",
            "content_sha256",
            name="egress_write_dedupe_idx",
        ),
    )

    egress_id: Mapped[str] = mapped_column(String, primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String, ForeignKey("workflow_runs.workflow_id"))
    egress_type: Mapped[str] = mapped_column(String, index=True)
    destination_uri: Mapped[str] = mapped_column(Text)
    content_sha256: Mapped[str] = mapped_column(String, index=True)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FallbackStateRow(Base):
    __tablename__ = "fallback_state"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    fallback_role: Mapped[str | None] = mapped_column(String, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class SessionItemRow(Base):
    __tablename__ = "session_items"
    __table_args__ = (
        UniqueConstraint("turn_id", "ordinal", name="session_item_turn_ordinal_idx"),
        Index(
            "session_items_session_order_idx",
            "session_id",
            "model_id",
            "created_at",
            "turn_id",
            "ordinal",
        ),
    )

    item_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, index=True)
    model_id: Mapped[str] = mapped_column(String, index=True)
    turn_id: Mapped[str] = mapped_column(String, index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    item_type: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String)
    content: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SessionContextRow(Base):
    __tablename__ = "session_contexts"

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    model_id: Mapped[str] = mapped_column(String, primary_key=True)
    active_context_artifact_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("artifacts.artifact_id"),
        nullable=True,
    )
    compacted_summary_artifact_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("artifacts.artifact_id"),
        nullable=True,
    )
    snapshot_item_id: Mapped[str | None] = mapped_column(
        String,
        ForeignKey("session_items.item_id"),
        nullable=True,
    )
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    max_window_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    export_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine = self._make_engine(settings.database_url)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def _make_engine(self, url: str) -> Engine:
        if url.startswith("sqlite:///"):
            db_path = Path(url.removeprefix("sqlite:///"))
            if str(db_path) != ":memory:":
                db_path.parent.mkdir(parents=True, exist_ok=True)
            return create_engine(url, connect_args={"check_same_thread": False})
        # Bounded so an unreachable host fails instead of hanging. A refused
        # connection already returns at once; a host that drops packets - the
        # laptop asleep, the container gone from under a live socket - does not,
        # and without this the wait is the OS TCP timeout. `/health` probes
        # through this engine, and a health check that can hang answers no
        # faster than the outage it is meant to report. Ten seconds is what the
        # DBOS system engine already uses against the same server.
        return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 10})

    def create_database_schema(self) -> None:
        Base.metadata.create_all(self.engine)
        self._upgrade_session_context_schema()

    def _upgrade_session_context_schema(self) -> None:
        columns = {
            column["name"] for column in inspect(self.engine).get_columns("session_contexts")
        }
        if "snapshot_item_id" in columns:
            return
        if self.engine.dialect.name == "postgresql":
            statement = """
                ALTER TABLE session_contexts
                ADD COLUMN IF NOT EXISTS snapshot_item_id text
                REFERENCES session_items(item_id)
            """
        else:
            statement = """
                ALTER TABLE session_contexts
                ADD COLUMN snapshot_item_id varchar
                REFERENCES session_items(item_id)
            """
        with self.engine.begin() as connection:
            connection.execute(text(statement))

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
