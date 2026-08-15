# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from pathlib import Path

import httpx

from local_first_agent_os.contracts import SourceType, WorkflowType, WorkspaceId
from local_first_agent_os.ids import build_chunk_id
from local_first_agent_os.ingress import normalize_scheduled_event
from local_first_agent_os.runtime import build_runtime
from local_first_agent_os.settings import Settings
from local_first_agent_os.vector_store_io import dump_vector_store, restore_vector_store
from local_first_agent_os.workflow import WorkflowEngine


def _embed_directory(runtime, directory: Path) -> None:
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=WorkspaceId.GENERAL.value,
        event_type="pi.directive",
        payload={"directive": f"/store {directory}"},
    )
    WorkflowEngine(runtime).directory_embedding(event)


def test_dump_then_restore_round_trip(runtime, tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.md").write_text("DBOS owns durable workflow state.", encoding="utf-8")
    (source / "second.md").write_text("Pi orchestrates local agent rules.", encoding="utf-8")
    _embed_directory(runtime, source)
    dump_path = tmp_path / "dump.tar.gz"
    summary = dump_vector_store(runtime, dump_path)
    assert dump_path.exists()
    assert summary.chunks_written >= 2
    assert summary.artifacts_written >= 1

    fresh_path = tmp_path / "fresh"
    settings = Settings(
        database_url=f"sqlite:///{fresh_path / 'test.sqlite3'}",
        artifact_root=fresh_path / "artifacts",
        spool_dir=fresh_path / "spool",
        config_dir=fresh_path / "configs",
        mock_models=True,
        use_dbos=False,
    )
    fresh_runtime = build_runtime(settings)
    assert fresh_runtime.repository.list_embedding_chunks(None) == []
    restored = restore_vector_store(fresh_runtime, dump_path)
    assert restored.chunks_restored >= 2
    assert restored.artifacts_restored >= 1
    assert len(fresh_runtime.repository.list_embedding_chunks(None)) >= 2


def test_dump_with_no_chunks_writes_empty_archive(runtime, tmp_path: Path) -> None:
    dump_path = tmp_path / "empty_dump.tar.gz"
    summary = dump_vector_store(runtime, dump_path)
    assert dump_path.exists()
    assert summary.chunks_written == 0
    assert summary.artifacts_written == 0


def test_workflowy_import_filters_records_and_embeds_in_batches(
    runtime, tmp_path: Path, monkeypatch
) -> None:
    records = [
        {
            "chunk_idx": 1,
            "top_level": "/jobs",
            "headings": ["/jobs"],
            "context_text": "[Path] /jobs\n\n- /jobs",
        },
        {
            "chunk_idx": 2,
            "top_level": "/ideas",
            "headings": ["/ideas", "idea: first"],
            "context_text": "[Path] /ideas > idea: first\n\n- /ideas\n  - idea: first",
        },
        {
            "chunk_idx": 3,
            "top_level": "/ideas",
            "headings": ["/ideas", "idea: second"],
            "context_text": (
                "[Path] /ideas > idea: second\n\n- /ideas\n  - idea: second\n    - "
                + "long detail " * 400
            ),
        },
    ]
    source = tmp_path / "workflowy.jsonl"
    source.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    event = normalize_scheduled_event(
        source_type=SourceType.WORKFLOWY,
        workspace_id=WorkspaceId.WORKFLOWY.value,
        event_type="workflowy.import_chunks",
        payload={"path": str(source)},
    )
    runtime.repository.register_ingress_event(event)
    workflow_id = "workflowy-import-test"
    runtime.repository.start_workflow_run(
        workflow_id=workflow_id,
        workflow_type=WorkflowType.WORKFLOWY_SYNC.value,
        workspace_id=WorkspaceId.WORKFLOWY.value,
        input_event_id=event.event_id,
    )
    original_embed = runtime.model_manager.embed_texts
    batches: list[list[str]] = []

    def record_batch(texts: list[str], workflow_id_arg: str) -> list[list[float]]:
        batches.append(texts)
        return original_embed(texts, workflow_id_arg)

    monkeypatch.setattr(runtime.model_manager, "embed_texts", record_batch)

    added = runtime.retrieval.import_workflowy_chunks_jsonl(
        source,
        workspace_id=WorkspaceId.WORKFLOWY.value,
        workflow_id=workflow_id,
        limit=2,
        top_level="/ideas",
        batch_size=16,
    )

    rows = runtime.repository.list_embedding_chunks(WorkspaceId.WORKFLOWY.value)
    assert added == 2
    assert len(batches) == 1
    assert len(batches[0]) == 2
    assert [row.metadata_json["workflowy_chunk_idx"] for row in rows] == [2, 3]
    assert len(rows[1].text) > 2_000
    assert "sub_chunk" not in rows[1].metadata_json


def test_complete_workflowy_import_prunes_stale_split_rows(runtime, tmp_path: Path) -> None:
    source = tmp_path / "workflowy.jsonl"
    record = {
        "chunk_idx": 7,
        "top_level": "/ideas",
        "headings": ["/ideas", "idea: atomic"],
        "context_text": "[Path] /ideas > idea: atomic\n\n- /ideas\n  - idea: atomic",
    }
    source.write_text(json.dumps(record) + "\n", encoding="utf-8")
    artifact = runtime.artifact_store.write_json(
        role="workflowy_node_snapshot",
        payload={"schema_version": "workflowy_node_snapshot.v1", **record},
        workflow_id=None,
        schema_version="workflowy_node_snapshot.v1",
    )
    model_id = runtime.model_manager.registry.resolve_model("embedder").model_id
    atomic_chunk_id = build_chunk_id(artifact.sha256, 0, model_id)
    runtime.repository.upsert_embedding_chunk(
        chunk_id=atomic_chunk_id,
        artifact_id=artifact.artifact_id,
        workspace_id=WorkspaceId.WORKFLOWY.value,
        chunk_index=0,
        text_sha256="legacy",
        text="truncated atomic prefix",
        embedding_model_id=model_id,
        embedding=[1.0],
        metadata={
            "source": "workflowy_chunks_jsonl",
            "source_path": str(source.resolve()),
            "top_level": "/ideas",
            "sub_chunk": 0,
        },
    )
    runtime.repository.upsert_embedding_chunk(
        chunk_id="legacy-split-tail",
        artifact_id=artifact.artifact_id,
        workspace_id=WorkspaceId.WORKFLOWY.value,
        chunk_index=1,
        text_sha256="legacy-tail",
        text="split tail",
        embedding_model_id=model_id,
        embedding=[1.0],
        metadata={
            "source": "workflowy_chunks_jsonl",
            "source_path": str(source.resolve()),
            "top_level": "/ideas",
            "sub_chunk": 1,
        },
    )

    runtime.retrieval.import_workflowy_chunks_jsonl(
        source,
        workspace_id=WorkspaceId.WORKFLOWY.value,
        workflow_id="workflowy-atomic-refresh",
        top_level="/ideas",
    )

    rows = runtime.repository.list_embedding_chunks(WorkspaceId.WORKFLOWY.value)
    assert len(rows) == 1
    assert rows[0].text == record["context_text"]
    assert "sub_chunk" not in rows[0].metadata_json


def test_workflowy_import_reloads_and_retries_atomic_record(
    runtime, tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "workflowy.jsonl"
    text = "[Path] /ideas > idea: retry\n\n- /ideas\n  - idea: retry"
    source.write_text(
        json.dumps(
            {
                "chunk_idx": 9,
                "top_level": "/ideas",
                "context_text": text,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    original_embed = runtime.model_manager.embed_texts
    attempts = 0
    reloads = 0

    def flaky_embed(texts: list[str], workflow_id: str) -> list[list[float]]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            request = httpx.Request("POST", "http://127.0.0.1/v1/embeddings")
            response = httpx.Response(500, request=request)
            raise httpx.HTTPStatusError("transient", request=request, response=response)
        return original_embed(texts, workflow_id)

    def record_reload(*_args, **_kwargs) -> None:
        nonlocal reloads
        reloads += 1

    monkeypatch.setattr(runtime.model_manager, "embed_texts", flaky_embed)
    monkeypatch.setattr(runtime.model_manager, "ensure_loaded", record_reload)

    added = runtime.retrieval.import_workflowy_chunks_jsonl(
        source,
        workspace_id=WorkspaceId.WORKFLOWY.value,
        workflow_id="workflowy-retry",
        top_level="/ideas",
    )

    rows = runtime.repository.list_embedding_chunks(WorkspaceId.WORKFLOWY.value)
    assert added == 1
    assert attempts == 2
    assert reloads == 1
    assert rows[0].text == text
