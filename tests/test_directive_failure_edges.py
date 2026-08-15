# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import pytest

from local_first_agent_os import model_manager as model_manager_module
from local_first_agent_os.contracts import (
    ArtifactRole,
    ModelRole,
    SourceType,
    WorkflowStatus,
    WorkflowType,
    WorkspaceId,
)
from local_first_agent_os.ingress import (
    BoundsError,
    normalize_file_event,
    normalize_scheduled_event,
)
from local_first_agent_os.workflow import WorkflowEngine


def _directive_event(directive: str, workspace_id: str = WorkspaceId.GENERAL.value):
    return normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id=workspace_id,
        event_type="pi.directive",
        payload={"directive": directive},
    )


def test_compact_failure_when_compactor_load_raises(runtime, monkeypatch) -> None:
    def boom(role):
        if role == ModelRole.COMPACTOR:
            raise RuntimeError("simulated compactor load failure")

    monkeypatch.setattr(runtime.model_manager, "ensure_loaded", boom)
    long_context = "word " * 10_000
    event = _directive_event("/compact")
    event = event.model_copy(
        update={
            "payload": {
                **event.payload,
                "context": long_context,
                "max_window_tokens": 1024,
                "threshold_ratio": 0.5,
            }
        }
    )
    result = WorkflowEngine(runtime).model_directive(event)
    assert result.status == WorkflowStatus.FAILED_PERMANENT
    assert result.help is not None


def test_get_against_empty_store_returns_empty_hits(runtime) -> None:
    event = _directive_event("/get nothing-here")
    result = WorkflowEngine(runtime).model_directive(event)
    assert result.status == WorkflowStatus.COMPLETED
    payloads = [a for a in result.artifacts if str(a.role) == "directive_result"]
    assert payloads
    payload = runtime.artifact_store.read_json(payloads[0].artifact_id)
    assert payload["action"] == "get"
    assert payload["hits"] == []
    assert payload["ranked_ids"] == []


def test_fetch_workflowy_returns_first_indexed_idea_by_chunk_order(runtime) -> None:
    for chunk_idx, text in (
        (41, "[Path] /ideas > idea: first\n\n- /ideas\n  - idea: first"),
        (42, "[Path] /ideas > idea: second\n\n- /ideas\n  - idea: second"),
    ):
        artifact = runtime.artifact_store.write_json(
            role=ArtifactRole.WORKFLOWY_NODE_SNAPSHOT.value,
            payload={"chunk_idx": chunk_idx, "text": text},
            workflow_id=None,
            schema_version="workflowy_node_snapshot.v1",
        )
        runtime.repository.upsert_embedding_chunk(
            chunk_id=f"workflowy:{chunk_idx}",
            artifact_id=artifact.artifact_id,
            workspace_id=WorkspaceId.WORKFLOWY.value,
            chunk_index=0,
            text_sha256=f"sha:{chunk_idx}",
            text=text,
            embedding_model_id="mock-embedder",
            embedding=[1.0],
            metadata={
                "source": "workflowy_chunks_jsonl",
                "top_level": "/ideas",
                "workflowy_chunk_idx": chunk_idx,
                "sub_chunk": 0,
            },
        )

    result = WorkflowEngine(runtime).model_directive(
        _directive_event("/fetch /workflowy give me the first idea bullet under /ideas")
    )

    artifact = next(a for a in result.artifacts if str(a.role) == "directive_result")
    payload = runtime.artifact_store.read_json(artifact.artifact_id)
    assert result.status == WorkflowStatus.COMPLETED
    assert payload["action"] == "fetch"
    assert payload["retrieval_source"] == "workflowy"
    assert "idea: first" in payload["report"]
    assert "idea: second" not in payload["report"]
    assert payload["hits"][0]["metadata"]["retrieval_mode"] == ("structured_rag_metadata")


def test_model_unload_treats_router_400_as_not_loaded(runtime, monkeypatch) -> None:
    runtime.model_manager.settings.mock_models = False

    class Response:
        status_code = 400

        def raise_for_status(self):
            raise AssertionError("400 unload responses should not raise")

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(model_manager_module.httpx, "Client", Client)
    result = runtime.model_manager.unload(ModelRole.GENERAL)
    assert result["models"][0]["status"] == "not_loaded"


def test_screenshot_against_non_image_extension(runtime, tmp_path: Path) -> None:
    fake = tmp_path / "definitely-not-an-image.exe"
    fake.write_bytes(b"MZ-fake-binary")
    result = WorkflowEngine(runtime).model_directive(_directive_event(f"/screenshot {fake}"))
    # /screenshot routes to directory_embedding, which fails when no supported
    # text or image files are found under the path.
    assert result.workflow_type == WorkflowType.DIRECTORY_EMBEDDING
    assert result.status == WorkflowStatus.FAILED_PERMANENT
    assert result.help is not None


def test_audio_ingress_rejects_unsupported_extension(runtime, tmp_path: Path) -> None:
    blob = tmp_path / "voice.xyz"
    blob.write_bytes(b"not-audio")
    with pytest.raises(BoundsError):
        normalize_file_event(
            path=blob,
            workspace_id=WorkspaceId.AUDIO.value,
            workflow_type=WorkflowType.AUDIO_TRANSCRIPTION,
        )


def test_audio_ingress_rejects_oversized_file(runtime, tmp_path: Path, monkeypatch) -> None:
    audio = tmp_path / "huge.mp3"
    audio.write_bytes(b"id3-mock")
    from local_first_agent_os.ingress import FILE_BOUNDS

    monkeypatch.setitem(
        FILE_BOUNDS,
        WorkflowType.AUDIO_TRANSCRIPTION,
        FILE_BOUNDS[WorkflowType.AUDIO_TRANSCRIPTION].model_copy(update={"max_bytes": 1}),
    )
    with pytest.raises(BoundsError):
        normalize_file_event(
            path=audio,
            workspace_id=WorkspaceId.AUDIO.value,
            workflow_type=WorkflowType.AUDIO_TRANSCRIPTION,
        )


def test_send_to_wf_unsupported_extension_returns_help(runtime, tmp_path: Path) -> None:
    blob = tmp_path / "weird.bin"
    blob.write_bytes(b"binary-mock")
    result = WorkflowEngine(runtime).model_directive(_directive_event(f"/send-to-wf {blob} 04/28"))
    assert result.workflow_type == WorkflowType.SEND_TO_WORKFLOWY
    assert result.status == WorkflowStatus.FAILED_PERMANENT
    assert result.help is not None
