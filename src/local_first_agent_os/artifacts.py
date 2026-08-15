# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import mimetypes
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ._dbos_runtime import dbos_step
from .contracts import ArtifactRef
from .ids import build_artifact_id, sha256_bytes, sha256_file
from .observability import ARTIFACT_BYTES_TOTAL, ARTIFACT_WRITES_TOTAL
from .repository import Repository
from .settings import Settings

ROLE_EXTENSIONS = {
    "source_file": "bin",
    "source_image": "img",
    "ocr_input_image": "png",
    "prompt": "json",
    "ocr_text": "json",
    "ocr_batch_manifest": "json",
    "normalized_text": "txt",
    "notes_snapshot": "json",
    "workflowy_node_snapshot": "json",
    "transcript": "json",
    "med_report": "json",
    "candidate_set": "json",
    "pi_decision": "json",
    "directive_result": "json",
    "store_manifest": "json",
    "context_compaction": "json",
    "session_context": "md",
    "model_output": "json",
    "answer": "json",
    "training_manifest": "jsonl",
    "agent_execution_transcript": "jsonl",
    "agent_checkpoint_patch": "patch",
    "agent_checkpoint_git_status": "txt",
    "agent_checkpoint_test_summary": "txt",
    "unsupported_stub": "json",
    "send_to_wf_payload": "json",
    "done_recall_result": "json",
    "chrome_control_result": "json",
    "browser_acceptance_request": "json",
    "browser_acceptance_evidence": "json",
    "browser_preview_process_evidence": "json",
    "browser_screenshot": "png",
    "browser_trace": "zip",
    "entity_graph": "json",
    "graph_metrics": "json",
}


class ArtifactStore:
    def __init__(self, root: Path, repository: Repository, settings: Settings | None = None):
        self.root = root
        self.repository = repository
        self.settings = settings
        self.backend = settings.artifact_backend if settings else "filesystem"
        self._minio_client: Any | None = None
        self.root.mkdir(parents=True, exist_ok=True)

    def _role_dir(self, role: str) -> Path:
        path = self.root / role
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _promote_bytes(
        self,
        *,
        role: str,
        data: bytes,
        workflow_id: str | None,
        mime_type: str,
        schema_version: str,
        extension: str | None = None,
    ) -> ArtifactRef:
        digest = sha256_bytes(data)
        artifact_id = build_artifact_id(role, digest)
        ext = extension or ROLE_EXTENSIONS.get(role, "bin")
        final_path = self._role_dir(role) / f"sha256_{digest}.{ext}"
        if not final_path.exists():
            fd, tmp_name = tempfile.mkstemp(prefix=f".{role}.", dir=str(final_path.parent))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, final_path)
            finally:
                tmp_path = Path(tmp_name)
                if tmp_path.exists():
                    tmp_path.unlink()
        uri = f"file://{final_path}"
        if self.backend == "minio":
            object_name = self._object_name(role, digest, ext)
            self._put_minio_object(object_name, final_path, mime_type)
            uri = f"s3://{self._minio_bucket}/{object_name}"
        ref = ArtifactRef(
            artifact_id=artifact_id,
            role=role,
            uri=uri,
            sha256=digest,
            mime_type=mime_type,
            size_bytes=len(data),
            schema_version=schema_version,
        )
        self.repository.insert_artifact(ref, workflow_id)
        ARTIFACT_WRITES_TOTAL.labels(role=role, backend=self.backend).inc()
        ARTIFACT_BYTES_TOTAL.labels(role=role, backend=self.backend).inc(len(data))
        return ref

    @dbos_step(retries_allowed=True, max_attempts=3, interval_seconds=1.0, backoff_rate=2.0)
    def write_bytes(
        self,
        *,
        role: str,
        data: bytes,
        workflow_id: str | None,
        mime_type: str,
        schema_version: str,
        extension: str | None = None,
    ) -> ArtifactRef:
        return self._promote_bytes(
            role=role,
            data=data,
            workflow_id=workflow_id,
            mime_type=mime_type,
            schema_version=schema_version,
            extension=extension,
        )

    @dbos_step(retries_allowed=True, max_attempts=3, interval_seconds=1.0, backoff_rate=2.0)
    def write_text(
        self,
        *,
        role: str,
        text: str,
        workflow_id: str | None,
        schema_version: str,
        mime_type: str = "text/plain",
    ) -> ArtifactRef:
        return self._promote_bytes(
            role=role,
            data=text.encode("utf-8"),
            workflow_id=workflow_id,
            mime_type=mime_type,
            schema_version=schema_version,
            extension=ROLE_EXTENSIONS.get(role, "txt"),
        )

    @dbos_step(retries_allowed=True, max_attempts=3, interval_seconds=1.0, backoff_rate=2.0)
    def write_json(
        self,
        *,
        role: str,
        payload: dict[str, Any] | list[Any],
        workflow_id: str | None,
        schema_version: str,
    ) -> ArtifactRef:
        data = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
        return self._promote_bytes(
            role=role,
            data=data,
            workflow_id=workflow_id,
            mime_type="application/json",
            schema_version=schema_version,
            extension=ROLE_EXTENSIONS.get(role, "json"),
        )

    @dbos_step(retries_allowed=True, max_attempts=3, interval_seconds=1.0, backoff_rate=2.0)
    def import_file(
        self,
        *,
        role: str,
        source_path: Path,
        workflow_id: str | None,
        schema_version: str,
    ) -> ArtifactRef:
        digest = sha256_file(source_path)
        mime_type = mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
        ext = source_path.suffix.lstrip(".") or ROLE_EXTENSIONS.get(role, "bin")
        final_path = self._role_dir(role) / f"sha256_{digest}.{ext}"
        if not final_path.exists():
            fd, tmp_name = tempfile.mkstemp(prefix=f".{role}.", dir=str(final_path.parent))
            try:
                with source_path.open("rb") as src, os.fdopen(fd, "wb") as dst:
                    while chunk := src.read(1024 * 1024):
                        dst.write(chunk)
                    dst.flush()
                    os.fsync(dst.fileno())
                os.replace(tmp_name, final_path)
            finally:
                tmp_path = Path(tmp_name)
                if tmp_path.exists():
                    tmp_path.unlink()
        uri = f"file://{final_path}"
        if self.backend == "minio":
            object_name = self._object_name(role, digest, ext)
            self._put_minio_object(object_name, final_path, mime_type)
            uri = f"s3://{self._minio_bucket}/{object_name}"
        ref = ArtifactRef(
            artifact_id=build_artifact_id(role, digest),
            role=role,
            uri=uri,
            sha256=digest,
            mime_type=mime_type,
            size_bytes=final_path.stat().st_size,
            schema_version=schema_version,
        )
        self.repository.insert_artifact(ref, workflow_id)
        ARTIFACT_WRITES_TOTAL.labels(role=role, backend=self.backend).inc()
        ARTIFACT_BYTES_TOTAL.labels(role=role, backend=self.backend).inc(ref.size_bytes)
        return ref

    def local_path(self, ref: ArtifactRef) -> Path:
        if ref.uri.startswith("file://"):
            return Path(ref.path)
        if ref.uri.startswith("s3://"):
            return self._materialize_minio_ref(ref)
        return Path(ref.path)

    def read_text(self, artifact_id: str) -> str:
        ref = self.repository.get_artifact(artifact_id)
        if ref is None:
            raise KeyError(f"Artifact not found: {artifact_id}")
        return self.local_path(ref).read_text(encoding="utf-8", errors="replace")

    def read_json(self, artifact_id: str) -> Any:
        return json.loads(self.read_text(artifact_id))

    @property
    def _minio_bucket(self) -> str:
        if self.settings is None:
            raise RuntimeError("MinIO artifact backend requires Settings")
        return self.settings.minio_artifact_bucket

    @property
    def _minio(self) -> Any:
        if self.settings is None:
            raise RuntimeError("MinIO artifact backend requires Settings")
        if self._minio_client is None:
            from minio import Minio

            self._minio_client = Minio(
                self.settings.minio_endpoint,
                access_key=self.settings.minio_access_key,
                secret_key=self.settings.minio_secret_key,
                secure=self.settings.minio_secure,
            )
            if not self._minio_client.bucket_exists(self._minio_bucket):
                self._minio_client.make_bucket(self._minio_bucket)
        return self._minio_client

    def _object_name(self, role: str, digest: str, extension: str) -> str:
        return f"{role}/sha256_{digest}.{extension}"

    def _put_minio_object(self, object_name: str, path: Path, content_type: str) -> None:
        self._minio.fput_object(
            self._minio_bucket,
            object_name,
            str(path),
            content_type=content_type,
        )

    def _materialize_minio_ref(self, ref: ArtifactRef) -> Path:
        parsed = urlparse(ref.uri)
        bucket = parsed.netloc
        object_name = parsed.path.lstrip("/")
        if bucket != self._minio_bucket:
            raise ValueError(f"Unexpected artifact bucket: {bucket}")
        cache_path = self.root / object_name
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        if cache_path.exists() and sha256_file(cache_path) == ref.sha256:
            return cache_path
        self._minio.fget_object(bucket, object_name, str(cache_path))
        return cache_path
