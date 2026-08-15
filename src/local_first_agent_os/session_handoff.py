# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Session-export handoff compaction and receive-time context hydration.

This module owns the portable session-handoff boundary:

- Level 0 (deterministic, no model): `externalize_events` replaces every
  embedded ``data:image/...;base64`` blob and every oversized or exactly
  duplicated tool output with a content-addressed ``artifact://sha256/...``
  reference backed by a write-once blob store.
- Level 0.5 (derived, cached): `summarize_images` attaches bounded visual
  summaries cached by ``image_sha256 + summarizer_model_id + prompt_version``
  so identical images are never re-summarized.
- Sender side: `export_handoff_bundle` emits a self-contained portable bundle
  (`handoff.jsonl`, `manifest.json`, `summary.md`, `artifacts/`).
- Canonical promotion: `externalize_session_in_place` destructively replaces a
  vendor rollout JSONL with its externalized form via lock, validate, fsync,
  and atomic rename, recording the new generation in `current-context.json`
  and keeping a bounded number of rollback backups.
- Receiver side: `initialize_handoff_context` performs the same deterministic
  externalization inside the receiving runtime's context-initialization
  transaction and assembles a prompt-ready `InitializedAgentContext` before
  any model sees the transcript.

Identity and interpretation are kept separate: the artifact reference is pure
content hash; summaries are derived metadata that can be regenerated without
rewriting any transcript that cites the reference. The receiving model
dereferences pixels or full tool output lazily through `ArtifactResolver`
(``resolve_artifact``) at ``ocr``, ``thumbnail``, or ``full`` detail.

This is deliberately distinct from `workflow/compaction.py`, which compacts
the active model context mid-session. Here the transformation is
deterministic, model-optional, and produces durable, portable outputs.
See `docs/session_handoff_canonical_context_design.md` for the full design.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import time
from base64 import b64decode
from collections import Counter
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .ids import sha256_bytes, sha256_file
from .utils import estimate_tokens

ARTIFACT_REF_SCHEME = "artifact://sha256/"

SCHEMA_VERSION_IMAGE_REFERENCE = "image_reference.v1"
SCHEMA_VERSION_TOOL_OUTPUT_REFERENCE = "tool_output_reference.v1"
SCHEMA_VERSION_HANDOFF_MANIFEST = "handoff_manifest.v1"
SCHEMA_VERSION_INITIALIZED_CONTEXT = "initialized_agent_context.v1"
SCHEMA_VERSION_IMAGE_SUMMARY = "image_summary.v1"
SCHEMA_VERSION_CONTEXT_GENERATION = "context_generation.v1"

RESOLVE_ARTIFACT_TOOL_NAME = "resolve_artifact"

# Receive-time compaction keeps this many trailing events verbatim so the
# receiving agent sees an exact recent conversational tail.
RAW_TAIL_EVENT_COUNT = 10

# Older non-user events are truncated to this serialized-character budget; the
# head and tail of the serialization are kept so commands and error strings at
# either end survive.
HEAD_EVENT_CHAR_BUDGET = 600

# Bound on derived image summaries, mirroring the compactor design's 100-200
# token guidance for image state.
IMAGE_SUMMARY_MAX_TOKENS = 120

# Level 0 pointerization rules for tool output strings in non-user events:
# unique strings at or above the threshold are externalized; smaller strings
# are externalized only when their exact bytes repeat across the transcript.
TOOL_OUTPUT_POINTERIZE_THRESHOLD_BYTES = 32 * 1024
DUPLICATE_POINTERIZE_MIN_BYTES = 4 * 1024

# Bounded operational envelope retained inline when output is pointerized.
TOOL_OUTPUT_PREVIEW_CHARS = 200
EXACT_ERROR_STRINGS_MAX = 5

# In-place promotion keeps this many prior canonical generations as rollback
# backups; older backups are deleted.
BACKUP_RETENTION_GENERATIONS = 2

CURRENT_CONTEXT_FILE_NAME = "current-context.json"

_MIME_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "text/plain": "txt",
}
_EXTENSION_MIMES = {extension: mime for mime, extension in _MIME_EXTENSIONS.items()}

_DATA_URI_PATTERN = re.compile(
    r"data:(image/(?:png|jpeg|jpg|gif|webp));base64,([A-Za-z0-9+/]+={0,2})"
)

_ARTIFACT_REF_PATTERN = re.compile(r"artifact://sha256/([0-9a-f]{64})")

_ERROR_LINE_PATTERN = re.compile(
    r"[A-Z][A-Za-z]*Error\b|\bException\b|\bTraceback\b|\bFAILED\b|\bpanic\b|error:"
)


class HandoffIntegrityError(RuntimeError):
    """A handoff bundle, session file, or workspace violates its own contract."""


class ArtifactResolutionError(RuntimeError):
    """A requested resolution detail cannot be produced at runtime."""


def build_image_artifact_ref(sha256: str) -> str:
    return f"{ARTIFACT_REF_SCHEME}{sha256}"


class SummaryStatus(StrEnum):
    MISSING = "missing"
    AVAILABLE = "available"


class ImageReference(BaseModel):
    """Derived, regenerable metadata about one content-addressed image.

    The artifact reference is the identity; everything else is interpretation
    and can change without touching any transcript that cites the reference.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION_IMAGE_REFERENCE
    type: str = "image_reference"
    artifact_ref: str
    mime_type: str
    byte_size: int
    width: int | None = None
    height: int | None = None
    summary_status: SummaryStatus = SummaryStatus.MISSING
    summary: str = ""
    visible_text: list[str] = Field(default_factory=list)
    summary_max_tokens: int = IMAGE_SUMMARY_MAX_TOKENS
    summary_model: str | None = None
    summary_version: str | None = None
    original_available: bool = True

    @property
    def sha256(self) -> str:
        return self.artifact_ref.removeprefix(ARTIFACT_REF_SCHEME)


class ToolOutputReference(BaseModel):
    """Pointerized tool output: bytes live once in the blob store while a
    bounded operational envelope stays inline in the transcript."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION_TOOL_OUTPUT_REFERENCE
    type: str = "tool_output_reference"
    artifact_ref: str
    mime_type: str = "text/plain"
    byte_size: int
    line_count: int
    preview_head: str
    preview_tail: str
    exact_error_strings: list[str] = Field(default_factory=list)
    full_output_available: bool = True

    @property
    def sha256(self) -> str:
        return self.artifact_ref.removeprefix(ARTIFACT_REF_SCHEME)


class ManifestArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relative_path: str
    mime_type: str
    sha256: str
    byte_size: int


class HandoffManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION_HANDOFF_MANIFEST
    session_id: str
    artifacts: dict[str, ManifestArtifact] = Field(default_factory=dict)
    image_references: list[ImageReference] = Field(default_factory=list)

    @staticmethod
    def artifact_key(sha256: str) -> str:
        return f"sha256:{sha256}"


class InitializedAgentContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION_INITIALIZED_CONTEXT
    source_session_id: str
    context_text: str
    artifact_refs: list[str] = Field(default_factory=list)
    enabled_tools: list[str] = Field(default_factory=list)
    token_count: int
    original_bytes: int
    rewritten_bytes: int


class ContextGenerationRecord(BaseModel):
    """Sidecar record identifying the exact canonical bytes of a generation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION_CONTEXT_GENERATION
    generation: int
    context_path: str
    sha256: str
    byte_count: int
    record_count: int


class ImageSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION_IMAGE_SUMMARY
    summary: str
    visible_text: list[str] = Field(default_factory=list)
    summary_model: str
    summary_version: str


class ImageSummarizer(Protocol):
    """Vision/OCR sidecar boundary; called at most once per unique image."""

    model_id: str
    prompt_version: str

    def summarize(self, image_path: Path, mime_type: str) -> ImageSummary: ...


class TranscriptCompactor(Protocol):
    """Reduces pre-tail history to bounded lines; model-driven variants plug in here."""

    def compact(self, head_events: list[dict[str, Any]]) -> list[str]: ...


def _sniff_png(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _sniff_gif(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    width, height = struct.unpack("<HH", data[6:10])
    return width, height


def _sniff_jpeg(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    offset = 2
    while offset + 9 < len(data):
        if data[offset] != 0xFF:
            return None
        marker = data[offset + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        length = struct.unpack(">H", data[offset + 2 : offset + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[offset + 5 : offset + 9])
            return width, height
        offset += 2 + length
    return None


def _sniff_webp(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    if data[12:16] == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    return None


def sniff_image_dimensions(data: bytes) -> tuple[int | None, int | None]:
    for sniffer in (_sniff_png, _sniff_gif, _sniff_jpeg, _sniff_webp):
        dimensions = sniffer(data)
        if dimensions is not None:
            return dimensions
    return None, None


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ContentAddressedBlobStore:
    """Write-once filesystem store keyed by full SHA-256 digest.

    Physical location is a module secret; transcripts carry only
    ``artifact://sha256/...`` references and manifests carry relative paths.
    Writes are fsynced and promoted atomically so a crash cannot leave a
    half-written blob at a final path.
    """

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes, mime_type: str) -> str:
        digest = sha256_bytes(data)
        extension = _MIME_EXTENSIONS.get(mime_type, "bin")
        path = self.root / f"sha256-{digest}.{extension}"
        if not path.exists():
            tmp_path = path.with_name(f".{path.name}.tmp")
            with tmp_path.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            tmp_path.replace(path)
        return digest

    def find(self, sha256: str) -> Path | None:
        matches = list(self.root.glob(f"sha256-{sha256}.*"))
        return matches[0] if matches else None

    def path_for(self, sha256: str) -> Path:
        path = self.find(sha256)
        if path is None:
            raise HandoffIntegrityError(f"Missing blob for sha256 {sha256}")
        return path

    @staticmethod
    def mime_type_of(path: Path) -> str:
        return _EXTENSION_MIMES.get(path.suffix.lstrip("."), "application/octet-stream")


class SummaryCache:
    """Derived-summary store keyed by image hash, model id, and prompt version."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, image_sha256: str, model_id: str, prompt_version: str) -> Path:
        key = sha256_bytes(f"{image_sha256}:{model_id}:{prompt_version}".encode())
        return self.root / f"summary-{key}.json"

    def get(self, image_sha256: str, model_id: str, prompt_version: str) -> ImageSummary | None:
        path = self._cache_path(image_sha256, model_id, prompt_version)
        if not path.exists():
            return None
        return ImageSummary.model_validate_json(path.read_text(encoding="utf-8"))

    def put(
        self, image_sha256: str, model_id: str, prompt_version: str, summary: ImageSummary
    ) -> None:
        path = self._cache_path(image_sha256, model_id, prompt_version)
        tmp_path = path.with_name(f".{path.name}.tmp")
        tmp_path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")
        tmp_path.replace(path)


class ExternalizationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[dict[str, Any]]
    images: dict[str, ImageReference] = Field(default_factory=dict)
    tool_outputs: dict[str, ToolOutputReference] = Field(default_factory=dict)
    original_bytes: int
    rewritten_bytes: int


def _rewrite_string(
    text: str, store: ContentAddressedBlobStore, images: dict[str, ImageReference]
) -> str:
    def replace(match: re.Match[str]) -> str:
        mime_type = match.group(1)
        if mime_type == "image/jpg":
            mime_type = "image/jpeg"
        try:
            data = b64decode(match.group(2), validate=True)
        except ValueError:
            return match.group(0)
        digest = store.put(data, mime_type)
        if digest not in images:
            width, height = sniff_image_dimensions(data)
            images[digest] = ImageReference(
                artifact_ref=build_image_artifact_ref(digest),
                mime_type=mime_type,
                byte_size=len(data),
                width=width,
                height=height,
            )
        return build_image_artifact_ref(digest)

    return _DATA_URI_PATTERN.sub(replace, text)


def _rewrite_value(
    value: Any, store: ContentAddressedBlobStore, images: dict[str, ImageReference]
) -> Any:
    if isinstance(value, str):
        return _rewrite_string(value, store, images)
    if isinstance(value, dict):
        return {key: _rewrite_value(item, store, images) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_value(item, store, images) for item in value]
    return value


def _serialize_event(event: dict[str, Any]) -> str:
    return json.dumps(event, sort_keys=True, ensure_ascii=False)


def _events_byte_size(events: list[dict[str, Any]]) -> int:
    return sum(len(_serialize_event(event).encode("utf-8")) + 1 for event in events)


def parse_rollout_jsonl(raw_text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw_text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError as error:
            raise HandoffIntegrityError(
                f"Rollout JSONL line {line_number} is not valid JSON: {error}"
            ) from error
        if not isinstance(event, dict):
            raise HandoffIntegrityError(f"Rollout JSONL line {line_number} is not a JSON object")
        events.append(event)
    return events


def extract_embedded_images(
    events: list[dict[str, Any]], store: ContentAddressedBlobStore
) -> ExternalizationResult:
    """Deterministically externalize every embedded base64 image.

    Every ``data:image/...;base64`` occurrence, whether it is a whole string
    value or embedded inside a larger string, is replaced in place by its
    ``artifact://sha256/...`` reference so event shapes stay stable for replay.
    """

    original_bytes = _events_byte_size(events)
    images: dict[str, ImageReference] = {}
    rewritten = [_rewrite_value(event, store, images) for event in events]
    return ExternalizationResult(
        events=rewritten,
        images=images,
        original_bytes=original_bytes,
        rewritten_bytes=_events_byte_size(rewritten),
    )


def _is_user_event(event: dict[str, Any]) -> bool:
    if event.get("role") == "user":
        return True
    payload = event.get("payload")
    return isinstance(payload, dict) and payload.get("role") == "user"


def _iter_event_strings(event: dict[str, Any]) -> list[str]:
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            found.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(event)
    return found


def _extract_error_lines(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and _ERROR_LINE_PATTERN.search(stripped) and stripped not in lines:
            lines.append(stripped[:TOOL_OUTPUT_PREVIEW_CHARS])
        if len(lines) >= EXACT_ERROR_STRINGS_MAX:
            break
    return lines


def _make_tool_output_reference(text: str, digest: str) -> ToolOutputReference:
    return ToolOutputReference(
        artifact_ref=build_image_artifact_ref(digest),
        byte_size=len(text.encode("utf-8")),
        line_count=text.count("\n") + 1,
        preview_head=text[:TOOL_OUTPUT_PREVIEW_CHARS],
        preview_tail=text[-TOOL_OUTPUT_PREVIEW_CHARS:],
        exact_error_strings=_extract_error_lines(text),
    )


def pointerize_large_outputs(
    events: list[dict[str, Any]], store: ContentAddressedBlobStore
) -> tuple[list[dict[str, Any]], dict[str, ToolOutputReference]]:
    """Level 0 pointerization of heavy tool output strings in non-user events.

    Two deterministic rules, no similarity heuristics: unique strings at or
    above the size threshold are externalized, and exact byte-duplicates above
    a smaller floor are externalized wherever they repeat. The string value is
    replaced by an inline ``tool_output_reference`` record that keeps the
    bounded operational envelope (previews, line count, exact error strings).
    User events are never pointerized.
    """

    duplicate_counts: Counter[str] = Counter()
    for event in events:
        if _is_user_event(event):
            continue
        for text in _iter_event_strings(event):
            if len(text.encode("utf-8")) >= DUPLICATE_POINTERIZE_MIN_BYTES:
                duplicate_counts[sha256_bytes(text.encode("utf-8"))] += 1

    references: dict[str, ToolOutputReference] = {}

    def rewrite(value: Any) -> Any:
        if isinstance(value, str):
            size = len(value.encode("utf-8"))
            if size < DUPLICATE_POINTERIZE_MIN_BYTES:
                return value
            digest = sha256_bytes(value.encode("utf-8"))
            is_duplicate = duplicate_counts[digest] >= 2
            if size < TOOL_OUTPUT_POINTERIZE_THRESHOLD_BYTES and not is_duplicate:
                return value
            store.put(value.encode("utf-8"), "text/plain")
            if digest not in references:
                references[digest] = _make_tool_output_reference(value, digest)
            return references[digest].model_dump(mode="json")
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        return value

    rewritten = [event if _is_user_event(event) else rewrite(event) for event in events]
    return rewritten, references


def externalize_events(
    events: list[dict[str, Any]], store: ContentAddressedBlobStore
) -> ExternalizationResult:
    """Full Level 0 pass: image externalization, then output pointerization.

    Images go first so base64 payloads embedded inside big tool outputs become
    small references before the size rules run.
    """

    original_bytes = _events_byte_size(events)
    image_pass = extract_embedded_images(events, store)
    rewritten, tool_outputs = pointerize_large_outputs(image_pass.events, store)
    return ExternalizationResult(
        events=rewritten,
        images=image_pass.images,
        tool_outputs=tool_outputs,
        original_bytes=original_bytes,
        rewritten_bytes=_events_byte_size(rewritten),
    )


def summarize_images(
    images: dict[str, ImageReference],
    store: ContentAddressedBlobStore,
    cache: SummaryCache,
    summarizer: ImageSummarizer | None,
) -> dict[str, ImageReference]:
    """Attach cached or freshly generated summaries to image references.

    Without a summarizer the references stay purely structural with
    ``summary_status: missing``, which is a valid degraded state: identity and
    resolvability never depend on interpretation.
    """

    if summarizer is None:
        return images
    enriched: dict[str, ImageReference] = {}
    for digest, reference in images.items():
        summary = cache.get(digest, summarizer.model_id, summarizer.prompt_version)
        if summary is None:
            summary = summarizer.summarize(store.path_for(digest), reference.mime_type)
            cache.put(digest, summarizer.model_id, summarizer.prompt_version, summary)
        enriched[digest] = reference.model_copy(
            update={
                "summary_status": SummaryStatus.AVAILABLE,
                "summary": summary.summary,
                "visible_text": summary.visible_text,
                "summary_model": summary.summary_model,
                "summary_version": summary.summary_version,
            }
        )
    return enriched


def _truncate_middle(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    head = text[: budget * 2 // 3]
    tail = text[-(budget // 3) :]
    omitted = len(text) - len(head) - len(tail)
    return f"{head} …[truncated {omitted} chars]… {tail}"


class DeterministicTranscriptCompactor:
    """Model-free default: dedupe exact repeats, keep user turns verbatim,
    truncate other events to a bounded serialization."""

    def __init__(self, event_char_budget: int = HEAD_EVENT_CHAR_BUDGET):
        self.event_char_budget = event_char_budget

    def compact(self, head_events: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        seen: set[str] = set()
        duplicates = 0
        for event in head_events:
            serialized = _serialize_event(event)
            if serialized in seen:
                duplicates += 1
                continue
            seen.add(serialized)
            if _is_user_event(event):
                lines.append(serialized)
            else:
                lines.append(_truncate_middle(serialized, self.event_char_budget))
        if duplicates:
            lines.append(f"[{duplicates} exact duplicate events dropped]")
        return lines


def _collect_artifact_refs(events: list[dict[str, Any]]) -> set[str]:
    refs: set[str] = set()
    for event in events:
        refs.update(
            build_image_artifact_ref(digest)
            for digest in _ARTIFACT_REF_PATTERN.findall(_serialize_event(event))
        )
    return refs


def _format_image_state(images: dict[str, ImageReference]) -> str:
    if not images:
        return "(none)"
    return "\n".join(
        f"- {reference.model_dump_json(exclude_none=True)}" for reference in images.values()
    )


def assemble_context_text(
    *,
    session_id: str,
    events: list[dict[str, Any]],
    images: dict[str, ImageReference],
    compactor: TranscriptCompactor,
    raw_tail_event_count: int = RAW_TAIL_EVENT_COUNT,
) -> str:
    head = events[:-raw_tail_event_count] if raw_tail_event_count else list(events)
    tail = events[-raw_tail_event_count:] if raw_tail_event_count else []
    if len(events) <= raw_tail_event_count:
        head, tail = [], list(events)
    sections = [
        "# Handoff Provenance",
        "\n".join(
            [
                f"- source_session_id: {session_id}",
                f"- total_events: {len(events)}",
                f"- raw_tail_events: {len(tail)}",
                f"- unique_images: {len(images)}",
                f"- image_resolution_tool: {RESOLVE_ARTIFACT_TOOL_NAME}"
                " (detail: ocr | thumbnail | full)",
            ]
        ),
        "# Image State",
        _format_image_state(images),
        "# Compacted History",
        "\n".join(compactor.compact(head)) if head else "(none)",
        "# Recent Raw Tail",
        "\n".join(_serialize_event(event) for event in tail) if tail else "(none)",
    ]
    return "\n\n".join(sections)


class HandoffBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle_dir: Path
    manifest: HandoffManifest
    original_bytes: int
    rewritten_bytes: int

    @property
    def handoff_jsonl_path(self) -> Path:
        return self.bundle_dir / "handoff.jsonl"

    @property
    def manifest_path(self) -> Path:
        return self.bundle_dir / "manifest.json"


def _bundle_relative_path(sha256: str, mime_type: str) -> str:
    extension = _MIME_EXTENSIONS.get(mime_type, "bin")
    return f"artifacts/sha256-{sha256[:16]}.{extension}"


def _write_summary_markdown(
    path: Path,
    session_id: str,
    manifest: HandoffManifest,
    tool_output_count: int,
    original_bytes: int,
    rewritten_bytes: int,
) -> None:
    lines = [
        f"# Handoff Summary: {session_id}",
        "",
        f"- Original transcript bytes: {original_bytes}",
        f"- Rewritten transcript bytes: {rewritten_bytes}",
        f"- Unique image artifacts: {len(manifest.image_references)}",
        f"- Pointerized tool outputs: {tool_output_count}",
        "",
        "## Images",
        "",
    ]
    if not manifest.image_references:
        lines.append("(none)")
    for reference in manifest.image_references:
        descriptor = reference.summary or "(no summary generated)"
        lines.append(f"- `{reference.artifact_ref}`: {descriptor}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def export_handoff_bundle(
    *,
    session_id: str,
    raw_jsonl_path: Path,
    output_root: Path,
    summarizer: ImageSummarizer | None = None,
) -> HandoffBundle:
    """Produce the portable bundle: rewritten JSONL, manifest, summary, blobs."""

    bundle_dir = output_root / session_id
    artifacts_dir = bundle_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    scratch_store = ContentAddressedBlobStore(bundle_dir / ".blobs")
    cache = SummaryCache(bundle_dir / ".summaries")

    events = parse_rollout_jsonl(raw_jsonl_path.read_text(encoding="utf-8"))
    externalized = externalize_events(events, scratch_store)
    images = summarize_images(externalized.images, scratch_store, cache, summarizer)

    manifest = HandoffManifest(session_id=session_id)
    blob_mimes: dict[str, str] = {digest: ref.mime_type for digest, ref in images.items()}
    blob_mimes.update({digest: ref.mime_type for digest, ref in externalized.tool_outputs.items()})
    for digest, mime_type in blob_mimes.items():
        relative_path = _bundle_relative_path(digest, mime_type)
        target = bundle_dir / relative_path
        if not target.exists():
            shutil.copyfile(scratch_store.path_for(digest), target)
        manifest.artifacts[HandoffManifest.artifact_key(digest)] = ManifestArtifact(
            relative_path=relative_path,
            mime_type=mime_type,
            sha256=digest,
            byte_size=target.stat().st_size,
        )
    manifest.image_references = list(images.values())

    handoff_path = bundle_dir / "handoff.jsonl"
    handoff_path.write_text(
        "".join(_serialize_event(event) + "\n" for event in externalized.events),
        encoding="utf-8",
    )
    (bundle_dir / "manifest.json").write_text(
        manifest.model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    _write_summary_markdown(
        bundle_dir / "summary.md",
        session_id,
        manifest,
        len(externalized.tool_outputs),
        externalized.original_bytes,
        externalized.rewritten_bytes,
    )
    shutil.rmtree(bundle_dir / ".blobs")
    shutil.rmtree(bundle_dir / ".summaries")
    bundle = HandoffBundle(
        bundle_dir=bundle_dir,
        manifest=manifest,
        original_bytes=externalized.original_bytes,
        rewritten_bytes=externalized.rewritten_bytes,
    )
    verify_handoff_bundle(bundle_dir)
    return bundle


def verify_handoff_bundle(bundle_dir: Path) -> HandoffManifest:
    """Check-or-die reference integrity for an emitted bundle.

    Every ``artifact://sha256/...`` reference in the rewritten transcript must
    resolve through the manifest to a file whose recomputed hash matches, and
    no base64 image data may remain.
    """

    manifest_path = bundle_dir / "manifest.json"
    handoff_path = bundle_dir / "handoff.jsonl"
    if not manifest_path.exists() or not handoff_path.exists():
        raise HandoffIntegrityError(f"Bundle at {bundle_dir} is missing manifest or transcript")
    manifest = HandoffManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    transcript = handoff_path.read_text(encoding="utf-8")
    if _DATA_URI_PATTERN.search(transcript):
        raise HandoffIntegrityError("Rewritten transcript still contains base64 image data")
    for digest in _ARTIFACT_REF_PATTERN.findall(transcript):
        key = HandoffManifest.artifact_key(digest)
        entry = manifest.artifacts.get(key)
        if entry is None:
            raise HandoffIntegrityError(f"Transcript references unmanifested artifact {key}")
        blob_path = bundle_dir / entry.relative_path
        if not blob_path.exists():
            raise HandoffIntegrityError(f"Manifest entry {key} points at missing file")
        actual = sha256_file(blob_path)
        if actual != entry.sha256:
            raise HandoffIntegrityError(
                f"Artifact {key} content hash mismatch: expected {entry.sha256}, got {actual}"
            )
    return manifest


def _validate_externalized_text(text: str, store: ContentAddressedBlobStore) -> int:
    """Validate a rewritten transcript against the blob store before promotion.

    Returns the record count. Raises `HandoffIntegrityError` on residual
    base64, unparseable records, unresolvable references, or hash mismatches.
    """

    events = parse_rollout_jsonl(text)
    if _DATA_URI_PATTERN.search(text):
        raise HandoffIntegrityError("Externalized transcript still contains base64 image data")
    for digest in sorted(set(_ARTIFACT_REF_PATTERN.findall(text))):
        blob_path = store.find(digest)
        if blob_path is None:
            raise HandoffIntegrityError(f"Transcript references missing blob sha256 {digest}")
        actual = sha256_file(blob_path)
        if actual != digest:
            raise HandoffIntegrityError(
                f"Blob for sha256 {digest} has corrupted content hash {actual}"
            )
    return len(events)


def _read_generation(session_dir: Path) -> int:
    record_path = session_dir / CURRENT_CONTEXT_FILE_NAME
    if not record_path.exists():
        return 0
    record = ContextGenerationRecord.model_validate_json(record_path.read_text(encoding="utf-8"))
    return record.generation


def _write_generation_record(session_dir: Path, record: ContextGenerationRecord) -> None:
    record_path = session_dir / CURRENT_CONTEXT_FILE_NAME
    tmp_path = record_path.with_name(f".{record_path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(record.model_dump_json(indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, record_path)


def _prune_backups(backups_dir: Path) -> None:
    backups = sorted(
        backups_dir.glob("session-g*.jsonl"),
        key=lambda path: int(path.stem.removeprefix("session-g")),
    )
    for stale in backups[:-BACKUP_RETENTION_GENERATIONS]:
        stale.unlink()


class SessionExternalizationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_path: Path
    generation: ContextGenerationRecord
    backup_path: Path
    original_bytes: int
    rewritten_bytes: int
    image_count: int
    tool_output_count: int


def externalize_session_in_place(*, session_jsonl_path: Path) -> SessionExternalizationResult:
    """Lossless Level 0 promotion: atomically replace a session JSONL in place.

    Sequence: lock, read, externalize blobs into the sibling content-addressed
    ``artifacts/`` store, write ``session.jsonl.compacting``, validate every
    record and reference against the store, fsync blobs and the temporary
    file, back up the prior generation under bounded retention, atomically
    rename over the original, and record the new canonical generation in
    ``current-context.json``. Consumers see either the complete old file or
    the complete new file, never a half-written transcript.

    The caller owns pausing any native Claude/Codex writer first; the lock
    file only serializes AI-OS-side promotions of the same session. After
    promotion the file is the AI-OS canonical transcript, and continuation is
    expected to flow through `initialize_handoff_context`, not vendor resume.

    This mode is lossless by construction: bytes move into the artifact store
    and nothing semantic is summarized or dropped.
    """

    session_dir = session_jsonl_path.parent
    lock_path = session_jsonl_path.with_name(f"{session_jsonl_path.name}.lock")
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as error:
        raise HandoffIntegrityError(
            f"Session is already locked for compaction: {lock_path}"
        ) from error
    temp_path = session_jsonl_path.with_name(f"{session_jsonl_path.name}.compacting")
    try:
        os.write(lock_fd, str(os.getpid()).encode("ascii"))
        store = ContentAddressedBlobStore(session_dir / "artifacts")
        events = parse_rollout_jsonl(session_jsonl_path.read_text(encoding="utf-8"))
        externalized = externalize_events(events, store)
        rewritten_text = "".join(_serialize_event(event) + "\n" for event in externalized.events)
        with temp_path.open("w", encoding="utf-8") as handle:
            handle.write(rewritten_text)
            handle.flush()
            os.fsync(handle.fileno())
        record_count = _validate_externalized_text(rewritten_text, store)

        prior_generation = _read_generation(session_dir)
        backups_dir = session_dir / "backups"
        backups_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backups_dir / f"session-g{prior_generation}.jsonl"
        shutil.copyfile(session_jsonl_path, backup_path)
        _fsync_dir(backups_dir)
        _prune_backups(backups_dir)

        os.replace(temp_path, session_jsonl_path)
        _fsync_dir(session_dir)
        generation = ContextGenerationRecord(
            generation=prior_generation + 1,
            context_path=session_jsonl_path.name,
            sha256=sha256_file(session_jsonl_path),
            byte_count=session_jsonl_path.stat().st_size,
            record_count=record_count,
        )
        _write_generation_record(session_dir, generation)
        return SessionExternalizationResult(
            session_path=session_jsonl_path,
            generation=generation,
            backup_path=backup_path,
            original_bytes=externalized.original_bytes,
            rewritten_bytes=externalized.rewritten_bytes,
            image_count=len(externalized.images),
            tool_output_count=len(externalized.tool_outputs),
        )
    finally:
        os.close(lock_fd)
        temp_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


class ResolveDetail(StrEnum):
    OCR = "ocr"
    THUMBNAIL = "thumbnail"
    FULL = "full"


class ResolvedArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_ref: str
    detail: ResolveDetail
    mime_type: str
    local_path: Path | None = None
    text: str | None = None


class OcrAdapter(Protocol):
    def extract_text(self, image_path: Path, mime_type: str) -> str: ...


class Thumbnailer(Protocol):
    def thumbnail(self, image_path: Path, mime_type: str) -> Path: ...


class ArtifactResolver:
    """The bounded ``resolve_artifact`` tool handed to the receiving agent.

    The ``artifact://sha256/...`` reference is the stable identity; this
    resolver decides how to materialize it for the current environment. Text
    blobs answer ``ocr`` directly; image blobs delegate to injected adapters.
    """

    def __init__(
        self,
        *,
        store: ContentAddressedBlobStore,
        ocr_adapter: OcrAdapter | None = None,
        thumbnailer: Thumbnailer | None = None,
    ):
        self._store = store
        self._ocr_adapter = ocr_adapter
        self._thumbnailer = thumbnailer

    def _blob_path(self, artifact_ref: str) -> Path:
        digest = artifact_ref.removeprefix(ARTIFACT_REF_SCHEME)
        blob_path = self._store.find(digest) if digest != artifact_ref else None
        if blob_path is None:
            raise ArtifactResolutionError(f"Unknown artifact reference: {artifact_ref}")
        return blob_path

    def resolve(
        self, artifact_ref: str, detail: ResolveDetail = ResolveDetail.FULL
    ) -> ResolvedArtifact:
        blob_path = self._blob_path(artifact_ref)
        mime_type = ContentAddressedBlobStore.mime_type_of(blob_path)
        if detail is ResolveDetail.FULL:
            return ResolvedArtifact(
                artifact_ref=artifact_ref,
                detail=detail,
                mime_type=mime_type,
                local_path=blob_path,
            )
        if detail is ResolveDetail.OCR:
            if mime_type.startswith("text/"):
                return ResolvedArtifact(
                    artifact_ref=artifact_ref,
                    detail=detail,
                    mime_type=mime_type,
                    text=blob_path.read_text(encoding="utf-8", errors="replace"),
                )
            if self._ocr_adapter is None:
                raise ArtifactResolutionError("No OCR adapter registered for detail=ocr")
            return ResolvedArtifact(
                artifact_ref=artifact_ref,
                detail=detail,
                mime_type=mime_type,
                text=self._ocr_adapter.extract_text(blob_path, mime_type),
            )
        if self._thumbnailer is None:
            raise ArtifactResolutionError("No thumbnailer registered for detail=thumbnail")
        return ResolvedArtifact(
            artifact_ref=artifact_ref,
            detail=detail,
            mime_type=mime_type,
            local_path=self._thumbnailer.thumbnail(blob_path, mime_type),
        )


class InitializedHandoffSession(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    context: InitializedAgentContext
    resolver: ArtifactResolver
    images: dict[str, ImageReference]
    tool_outputs: dict[str, ToolOutputReference]


def initialize_handoff_context(
    *,
    session_id: str,
    raw_jsonl_path: Path,
    workspace_root: Path,
    summarizer: ImageSummarizer | None = None,
    compactor: TranscriptCompactor | None = None,
    ocr_adapter: OcrAdapter | None = None,
    thumbnailer: Thumbnailer | None = None,
    raw_tail_event_count: int = RAW_TAIL_EVENT_COUNT,
) -> InitializedHandoffSession:
    """Receive-time context hydration for a handed-off session.

    Runs the deterministic externalization pipeline inside the receiving
    runtime before any model turn: parse, externalize, deduplicate, summarize
    through the cache, compact, assemble, and verify that every reference in
    the assembled context resolves. The resolver is returned alongside the
    context so the harness can register ``resolve_artifact`` before the first
    receiving-model turn.
    """

    store = ContentAddressedBlobStore(workspace_root / "artifacts")
    cache = SummaryCache(workspace_root / "summaries")
    events = parse_rollout_jsonl(raw_jsonl_path.read_text(encoding="utf-8"))
    externalized = externalize_events(events, store)
    images = summarize_images(externalized.images, store, cache, summarizer)
    context_text = assemble_context_text(
        session_id=session_id,
        events=externalized.events,
        images=images,
        compactor=compactor or DeterministicTranscriptCompactor(),
        raw_tail_event_count=raw_tail_event_count,
    )
    resolver = ArtifactResolver(
        store=store,
        ocr_adapter=ocr_adapter,
        thumbnailer=thumbnailer,
    )
    referenced = _collect_artifact_refs(externalized.events)
    referenced.update(build_image_artifact_ref(digest) for digest in images)
    referenced.update(build_image_artifact_ref(digest) for digest in externalized.tool_outputs)
    for artifact_ref in sorted(referenced):
        resolved = resolver.resolve(artifact_ref, ResolveDetail.FULL)
        if resolved.local_path is None or sha256_file(resolved.local_path) != (
            artifact_ref.removeprefix(ARTIFACT_REF_SCHEME)
        ):
            raise HandoffIntegrityError(f"Reference failed to resolve: {artifact_ref}")
    context = InitializedAgentContext(
        source_session_id=session_id,
        context_text=context_text,
        artifact_refs=sorted(referenced),
        enabled_tools=[RESOLVE_ARTIFACT_TOOL_NAME],
        token_count=estimate_tokens(context_text),
        original_bytes=externalized.original_bytes,
        rewritten_bytes=externalized.rewritten_bytes,
    )
    return InitializedHandoffSession(
        context=context,
        resolver=resolver,
        images=images,
        tool_outputs=externalized.tool_outputs,
    )


class ArtifactSweepResult(BaseModel):
    """What a reachability sweep found, and what it removed."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "artifact_sweep_result.v1"
    session_dir: Path
    scanned_transcripts: int
    reachable_digests: int
    unreferenced_blobs: int
    reclaimed_bytes: int
    deleted: bool
    skipped_recent: int


def sweep_unreferenced_artifacts(
    session_dir: Path,
    *,
    delete: bool = False,
    min_age_seconds: float = 3600.0,
) -> ArtifactSweepResult:
    """Find blobs no live transcript references, and optionally remove them.

    The store deduplicates by content, so it is bounded in the number of
    distinct images an operator ever pastes and unbounded in time: compacting a
    transcript rewrites the text that referenced a blob, and nothing has ever
    removed the blob afterwards. This is the reachability half the store was
    missing, shaped like `gc_ledger`: enumerate the roots, collect what they
    reach, and collect the rest.

    The roots are every transcript in the session directory, which means the
    session file, every retained generation under `backups/`, and any bundle
    transcript beside them. Backups are roots rather than garbage precisely
    because they exist to be restored; a sweep that ignored them would make
    restoring a generation produce dangling references.

    Read-only by default, because deleting bytes on a reachability argument
    deserves a look at the argument first. `min_age_seconds` additionally spares
    blobs younger than the window, so a sweep running while another process is
    mid-externalization cannot collect a blob written seconds before the
    transcript that will reference it.
    """

    artifacts_dir = session_dir / "artifacts"
    if not artifacts_dir.is_dir():
        return ArtifactSweepResult(
            session_dir=session_dir,
            scanned_transcripts=0,
            reachable_digests=0,
            unreferenced_blobs=0,
            reclaimed_bytes=0,
            deleted=delete,
            skipped_recent=0,
        )

    reachable: set[str] = set()
    scanned = 0
    for transcript in sorted(session_dir.rglob("*.jsonl")):
        if artifacts_dir in transcript.parents:
            continue
        try:
            text = transcript.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            # An unreadable transcript is not evidence that its blobs are dead.
            # Refuse the whole sweep rather than under-count reachability.
            raise HandoffIntegrityError(
                f"Could not read transcript for sweep: {transcript}"
            ) from error
        reachable.update(_ARTIFACT_REF_PATTERN.findall(text))
        scanned += 1

    cutoff = time.time() - min_age_seconds
    unreferenced = 0
    reclaimed = 0
    skipped_recent = 0
    for blob in sorted(artifacts_dir.iterdir()):
        if not blob.is_file() or not blob.name.startswith("sha256-"):
            continue
        digest = blob.name.removeprefix("sha256-").split(".", 1)[0]
        if digest in reachable:
            continue
        if blob.stat().st_mtime > cutoff:
            skipped_recent += 1
            continue
        unreferenced += 1
        reclaimed += blob.stat().st_size
        if delete:
            blob.unlink()

    return ArtifactSweepResult(
        session_dir=session_dir,
        scanned_transcripts=scanned,
        reachable_digests=len(reachable),
        unreferenced_blobs=unreferenced,
        reclaimed_bytes=reclaimed,
        deleted=delete,
        skipped_recent=skipped_recent,
    )
