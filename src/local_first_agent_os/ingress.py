# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import mimetypes
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import (
    AudioTranscriptionExtension,
    FileBound,
    IngressEvent,
    MedicalImageAnalyzerExtension,
    PaperNotesOcrExtension,
    SourceType,
    WhiteboardOcrExtension,
    WorkflowStatus,
    WorkflowType,
    WorkspaceId,
)
from .ids import build_event_id, sha256_file, sha256_text
from .settings import Settings

WHITEBOARD_OCR_EXTENSIONS: set[WhiteboardOcrExtension] = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".heic",
}
PAPER_NOTES_OCR_EXTENSIONS: set[PaperNotesOcrExtension] = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".heic",
    ".pdf",
}
AUDIO_TRANSCRIPTION_EXTENSIONS: set[AudioTranscriptionExtension] = {
    ".m4a",
    ".mp3",
    ".wav",
    ".aac",
    ".flac",
}
MEDICAL_IMAGE_ANALYZER_EXTENSIONS: set[MedicalImageAnalyzerExtension] = {
    ".jpg",
    ".jpeg",
    ".png",
    ".dcm",
}

FILE_BOUNDS: dict[WorkflowType, FileBound] = {
    WorkflowType.WHITEBOARD_OCR: FileBound(
        extensions=WHITEBOARD_OCR_EXTENSIONS,
        max_bytes=50 * 1024 * 1024,
    ),
    WorkflowType.WHITEBOARD_INTENT: FileBound(
        extensions=WHITEBOARD_OCR_EXTENSIONS,
        max_bytes=50 * 1024 * 1024,
    ),
    WorkflowType.PAPER_NOTES_OCR: FileBound(
        extensions=PAPER_NOTES_OCR_EXTENSIONS,
        max_bytes=100 * 1024 * 1024,
        max_pages=50,
    ),
    WorkflowType.AUDIO_TRANSCRIPTION: FileBound(
        extensions=AUDIO_TRANSCRIPTION_EXTENSIONS,
        max_bytes=500 * 1024 * 1024,
        terminal_on_violation=WorkflowStatus.UNSUPPORTED_STUB,
    ),
    WorkflowType.MEDICAL_IMAGE_ANALYZER: FileBound(
        extensions=MEDICAL_IMAGE_ANALYZER_EXTENSIONS,
        max_bytes=100 * 1024 * 1024,
        terminal_on_violation=WorkflowStatus.MANUAL_REVIEW,
    ),
}


class BoundsError(ValueError):
    def __init__(self, reason: str, terminal_status: WorkflowStatus):
        super().__init__(reason)
        self.reason = reason
        self.terminal_status = terminal_status


def wait_until_stable(path: Path, quiet_period_seconds: int = 3) -> tuple[int, float]:
    time.sleep(quiet_period_seconds)
    first = path.stat()
    time.sleep(1)
    second = path.stat()
    if (first.st_size, first.st_mtime) != (second.st_size, second.st_mtime):
        raise BoundsError("file_changed_during_stabilization", WorkflowStatus.FAILED_RETRYABLE)
    return second.st_size, second.st_mtime


def validate_file_bounds(path: Path, workflow_type: WorkflowType) -> dict[str, Any]:
    bound = FILE_BOUNDS[workflow_type]
    ext = path.suffix.lower()
    size = path.stat().st_size
    if ext not in bound.extensions:
        raise BoundsError(f"unsupported_extension:{ext}", bound.terminal_on_violation)
    if size > bound.max_bytes:
        raise BoundsError(f"file_too_large:{size}>{bound.max_bytes}", bound.terminal_on_violation)
    return {
        "extension": ext,
        "size_bytes": size,
        "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        "max_pages": bound.max_pages,
    }


def normalize_file_event(
    *,
    path: Path,
    workspace_id: str,
    workflow_type: WorkflowType,
    event_type: str = "created",
    detected_at: datetime | None = None,
    stable: bool = False,
) -> IngressEvent:
    if stable:
        size_bytes, mtime = wait_until_stable(path)
    else:
        stat = path.stat()
        size_bytes, mtime = stat.st_size, stat.st_mtime
    bounds = validate_file_bounds(path, workflow_type)
    digest = sha256_file(path)
    source_uri = f"file://{path.expanduser().resolve()}"
    return IngressEvent(
        event_id=build_event_id(SourceType.FILE, workspace_id, source_uri, event_type, digest),
        source_type=SourceType.FILE,
        event_type=event_type,
        workspace_id=workspace_id,
        source_uri=source_uri,
        content_sha256=digest,
        detected_at=detected_at or datetime.now(UTC),
        payload={
            **bounds,
            "workflow_type": workflow_type.value,
            "stable_size_bytes": size_bytes,
            "stable_mtime": mtime,
        },
    )


def normalize_prompt_event(
    prompt: str,
    workspace_id: str = WorkspaceId.GENERAL.value,
) -> IngressEvent:
    if len(prompt) > 256_000:
        raise BoundsError("prompt_too_large", WorkflowStatus.FAILED_PERMANENT)
    digest = sha256_text(prompt)
    source_uri = f"manual://prompt/{digest[:16]}"
    return IngressEvent(
        event_id=build_event_id(
            SourceType.MANUAL,
            workspace_id,
            source_uri,
            "prompt.submitted",
            digest,
        ),
        source_type=SourceType.MANUAL,
        event_type="prompt.submitted",
        workspace_id=workspace_id,
        source_uri=source_uri,
        content_sha256=digest,
        payload={"prompt": prompt, "size_chars": len(prompt)},
    )


def normalize_scheduled_event(
    *,
    source_type: SourceType,
    workspace_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
) -> IngressEvent:
    timestamp = datetime.now(UTC).isoformat()
    source_uri = f"{source_type.value}://scheduled/{event_type}/{timestamp}"
    digest = sha256_text(json.dumps(payload or {}, sort_keys=True) + timestamp)
    return IngressEvent(
        event_id=build_event_id(source_type, workspace_id, source_uri, event_type, digest),
        source_type=source_type,
        event_type=event_type,
        workspace_id=workspace_id,
        source_uri=source_uri,
        content_sha256=digest,
        payload=payload or {},
    )


class DiskSpool:
    def __init__(self, settings: Settings):
        self.spool_dir = settings.spool_dir
        self.spool_dir.mkdir(parents=True, exist_ok=True)

    def append(self, source_type: SourceType | str, event: IngressEvent) -> Path:
        path = self.spool_dir / f"{source_type}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json() + "\n")
        return path

    def read_all(self) -> list[IngressEvent]:
        events: list[IngressEvent] = []
        for path in sorted(self.spool_dir.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    events.append(IngressEvent.model_validate_json(line))
        return events
