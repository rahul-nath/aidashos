# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from local_first_agent_os.contracts import ArtifactRole, EgressStatus, WorkspaceId
from local_first_agent_os.db import EMBEDDING_DIM, EmbeddingChunkRow


def test_artifact_store_is_content_addressed(runtime) -> None:
    first = runtime.artifact_store.write_text(
        role=ArtifactRole.NORMALIZED_TEXT.value,
        text="durable artifact",
        workflow_id=None,
        schema_version="normalized_text.v1",
    )
    second = runtime.artifact_store.write_text(
        role=ArtifactRole.NORMALIZED_TEXT.value,
        text="durable artifact",
        workflow_id=None,
        schema_version="normalized_text.v1",
    )
    assert first.artifact_id == second.artifact_id
    assert first.sha256 == second.sha256


def test_egress_write_dedupes(runtime) -> None:
    runtime.repository.start_workflow_run(
        "wf1",
        "workflowy_write",
        WorkspaceId.WORKFLOWY.value,
        None,
    )
    first_id, first_created = runtime.repository.create_or_get_egress(
        workflow_id="wf1",
        egress_type="workflowy_insert",
        destination_uri="workflowy://node/abc",
        content_sha256="hash",
        request_json={"content": "hello"},
    )
    second_id, second_created = runtime.repository.create_or_get_egress(
        workflow_id="wf1",
        egress_type="workflowy_insert",
        destination_uri="workflowy://node/abc",
        content_sha256="hash",
        request_json={"content": "hello"},
    )
    runtime.repository.update_egress(first_id, status=EgressStatus.COMPLETED, response_json={})
    assert first_id == second_id
    assert first_created is True
    assert second_created is False


def test_postgres_embedding_distance_is_a_labelable_sql_expression() -> None:
    distance = EmbeddingChunkRow.embedding.cosine_distance([0.0] * EMBEDDING_DIM)
    statement = select(EmbeddingChunkRow, distance.label("distance")).order_by(distance)

    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "embedding <=>" in sql
    assert "ORDER BY embedding_chunks.embedding <=>" in sql
