# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path
from typing import Any

from .contracts import DumpSummary, RestoreSummary
from .runtime import AppRuntime

VECTOR_STORE_MANIFEST_NAME = "vector_store_manifest.json"
EMBEDDING_CHUNKS_NAME = "embedding_chunks.jsonl"
ARTIFACTS_DIR_NAME = "artifacts"


def _embedding_to_list(value: Any) -> list[float] | None:
    """Coerce a stored embedding (halfvec on postgres, JSON on sqlite) to a
    plain list so it can be JSON-serialized into a dump."""
    if value is None:
        return None
    if hasattr(value, "to_list"):
        return value.to_list()
    if hasattr(value, "tolist"):
        return value.tolist()
    return list(value)


def dump_vector_store(runtime: AppRuntime, output_path: Path) -> DumpSummary:
    """Dump embedding_chunks plus their referenced artifacts to a tarball."""

    chunks = runtime.repository.list_embedding_chunks(None)
    artifact_ids = {chunk.artifact_id for chunk in chunks if chunk.artifact_id}
    refs = []
    for artifact_id in artifact_ids:
        ref = runtime.repository.get_artifact(artifact_id)
        if ref is not None:
            refs.append(ref)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    chunks_payload: list[dict[str, Any]] = []
    for chunk in chunks:
        chunks_payload.append(
            {
                "chunk_id": chunk.chunk_id,
                "artifact_id": chunk.artifact_id,
                "workspace_id": chunk.workspace_id,
                "chunk_index": chunk.chunk_index,
                "text_sha256": chunk.text_sha256,
                "text": chunk.text,
                "embedding_model_id": chunk.embedding_model_id,
                "embedding": _embedding_to_list(chunk.embedding),
                "metadata_json": chunk.metadata_json,
            }
        )
    artifacts_written = 0
    with tarfile.open(output_path, "w:gz") as tar:
        manifest = {
            "schema_version": "vector_store_dump.v1",
            "chunk_count": len(chunks_payload),
            "artifact_count": len(refs),
        }
        _add_text_member(tar, VECTOR_STORE_MANIFEST_NAME, json.dumps(manifest, indent=2))
        chunks_data = "\n".join(json.dumps(item, sort_keys=True) for item in chunks_payload) + (
            "\n" if chunks_payload else ""
        )
        _add_text_member(tar, EMBEDDING_CHUNKS_NAME, chunks_data)
        for ref in refs:
            artifact_path = runtime.artifact_store.local_path(ref)
            if not artifact_path.exists():
                continue
            arcname = f"{ARTIFACTS_DIR_NAME}/{ref.artifact_id}"
            tar.add(str(artifact_path), arcname=arcname)
            ref_payload = {
                "artifact_id": ref.artifact_id,
                "role": str(ref.role),
                "uri": ref.uri,
                "sha256": ref.sha256,
                "mime_type": ref.mime_type,
                "size_bytes": ref.size_bytes,
                "schema_version": ref.schema_version,
            }
            _add_text_member(
                tar,
                f"{ARTIFACTS_DIR_NAME}/{ref.artifact_id}.meta.json",
                json.dumps(ref_payload, indent=2, sort_keys=True),
            )
            artifacts_written += 1
    return DumpSummary(
        chunks_written=len(chunks_payload),
        artifacts_written=artifacts_written,
        output_path=output_path,
    )


def restore_vector_store(runtime: AppRuntime, source_path: Path) -> RestoreSummary:
    """Restore a vector-store dump produced by `dump_vector_store`."""

    if not source_path.exists():
        raise FileNotFoundError(f"Vector-store dump not found: {source_path}")
    chunks_restored = 0
    artifacts_restored = 0
    with tarfile.open(source_path, "r:gz") as tar:
        manifest_member = tar.extractfile(VECTOR_STORE_MANIFEST_NAME)
        if manifest_member is not None:
            manifest_member.read()
        chunks_member = tar.extractfile(EMBEDDING_CHUNKS_NAME)
        chunk_lines: list[str] = []
        if chunks_member is not None:
            chunk_lines = chunks_member.read().decode("utf-8").splitlines()
        artifact_meta: dict[str, dict[str, Any]] = {}
        for member in tar.getmembers():
            if member.name.endswith(".meta.json") and member.name.startswith(
                f"{ARTIFACTS_DIR_NAME}/"
            ):
                payload = tar.extractfile(member)
                if payload is None:
                    continue
                meta = json.loads(payload.read().decode("utf-8"))
                artifact_meta[meta["artifact_id"]] = meta
        for member in tar.getmembers():
            if not member.name.startswith(f"{ARTIFACTS_DIR_NAME}/"):
                continue
            if member.name.endswith(".meta.json"):
                continue
            if not member.isfile():
                continue
            artifact_id = Path(member.name).name
            meta = artifact_meta.get(artifact_id)
            if meta is None:
                continue
            data_member = tar.extractfile(member)
            if data_member is None:
                continue
            data = data_member.read()
            ref = runtime.artifact_store.write_bytes(
                role=str(meta["role"]),
                data=data,
                workflow_id=None,
                mime_type=str(meta["mime_type"]),
                schema_version=str(meta["schema_version"]),
                extension=None,
            )
            artifacts_restored += int(ref.artifact_id != "")
        for line in chunk_lines:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            inserted = runtime.repository.upsert_embedding_chunk(
                chunk_id=payload["chunk_id"],
                artifact_id=payload["artifact_id"],
                workspace_id=payload["workspace_id"],
                chunk_index=int(payload["chunk_index"]),
                text_sha256=payload["text_sha256"],
                text=payload["text"],
                embedding_model_id=payload["embedding_model_id"],
                embedding=payload.get("embedding"),
                metadata=payload.get("metadata_json") or {},
            )
            if inserted:
                chunks_restored += 1
    return RestoreSummary(
        chunks_restored=chunks_restored,
        artifacts_restored=artifacts_restored,
        source_path=source_path,
    )


def _add_text_member(tar: tarfile.TarFile, name: str, text: str) -> None:
    data = text.encode("utf-8")
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))
