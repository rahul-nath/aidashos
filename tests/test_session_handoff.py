# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the session-export handoff compactor and receive-time hydration."""

from __future__ import annotations

import json
import struct
import zlib
from base64 import b64encode
from pathlib import Path

import pytest

from local_first_agent_os.ids import sha256_bytes, sha256_file
from local_first_agent_os.session_handoff import (
    ARTIFACT_REF_SCHEME,
    RESOLVE_ARTIFACT_TOOL_NAME,
    ArtifactResolutionError,
    ContentAddressedBlobStore,
    HandoffIntegrityError,
    ImageSummary,
    ResolveDetail,
    SummaryCache,
    SummaryStatus,
    export_handoff_bundle,
    externalize_session_in_place,
    extract_embedded_images,
    initialize_handoff_context,
    parse_rollout_jsonl,
    pointerize_large_outputs,
    sniff_image_dimensions,
    verify_handoff_bundle,
)


def make_png(width: int = 1, height: int = 1) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def make_gif(width: int = 3, height: int = 2) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 20


def make_jpeg_header(width: int = 640, height: int = 480) -> bytes:
    sof0 = struct.pack(">HBHHB", 8 + 3 * 3, 8, height, width, 3) + b"\x01\x11\x00" * 3
    return b"\xff\xd8" + b"\xff\xc0" + sof0 + b"\xff\xd9"


def data_uri(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{b64encode(data).decode('ascii')}"


def write_rollout(path: Path, events: list[dict]) -> Path:
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return path


class CountingSummarizer:
    model_id = "gemma4-vision"
    prompt_version = "image_summary.v1"

    def __init__(self) -> None:
        self.calls = 0

    def summarize(self, image_path: Path, mime_type: str) -> ImageSummary:
        self.calls += 1
        return ImageSummary(
            summary=f"Summary of {image_path.name}",
            visible_text=["FileNotFoundError"],
            summary_model=self.model_id,
            summary_version=self.prompt_version,
        )


class FakeOcr:
    def extract_text(self, image_path: Path, mime_type: str) -> str:
        return f"ocr:{image_path.name}"


@pytest.fixture
def png_bytes() -> bytes:
    return make_png(2, 3)


@pytest.fixture
def rollout_events(png_bytes: bytes) -> list[dict]:
    gif = make_gif()
    return [
        {"role": "user", "content": "Please look at this screenshot"},
        {"role": "user", "content": [{"type": "input_image", "image_url": data_uri(png_bytes)}]},
        {"role": "assistant", "content": "I see a failing test"},
        {
            "role": "tool",
            "content": f"screenshot follows {data_uri(png_bytes)} end",
        },
        {
            "role": "user",
            "content": [{"type": "input_image", "image_url": data_uri(gif, "image/gif")}],
        },
        {"role": "assistant", "content": "x" * 2000},
    ]


def test_sniff_image_dimensions() -> None:
    assert sniff_image_dimensions(make_png(2, 3)) == (2, 3)
    assert sniff_image_dimensions(make_gif(3, 2)) == (3, 2)
    assert sniff_image_dimensions(make_jpeg_header(640, 480)) == (640, 480)
    assert sniff_image_dimensions(b"not an image") == (None, None)


def test_extraction_deduplicates_and_rewrites(
    tmp_path: Path, rollout_events: list[dict], png_bytes: bytes
) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    result = extract_embedded_images(rollout_events, store)

    assert len(result.images) == 2
    digest = sha256_bytes(png_bytes)
    ref = f"{ARTIFACT_REF_SCHEME}{digest}"
    rewritten = json.dumps(result.events)
    assert "base64" not in rewritten
    assert result.events[1]["content"][0]["image_url"] == ref
    assert result.events[3]["content"] == f"screenshot follows {ref} end"
    assert result.original_bytes > result.rewritten_bytes
    png_reference = result.images[digest]
    assert (png_reference.width, png_reference.height) == (2, 3)
    assert png_reference.mime_type == "image/png"
    assert png_reference.summary_status == SummaryStatus.MISSING
    assert sha256_file(store.path_for(digest)) == digest


def test_invalid_base64_left_untouched(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    events = [{"content": "data:image/png;base64,abc"}]
    result = extract_embedded_images(events, store)
    assert result.events[0]["content"] == "data:image/png;base64,abc"
    assert result.images == {}


def test_parse_rollout_rejects_invalid_lines(tmp_path: Path) -> None:
    with pytest.raises(HandoffIntegrityError):
        parse_rollout_jsonl('{"ok": true}\nnot json\n')
    with pytest.raises(HandoffIntegrityError):
        parse_rollout_jsonl('["a", "list"]\n')


def test_pointerize_large_and_duplicate_outputs(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    big = "x" * 40000 + "\nFileNotFoundError: tests/fixtures/sample.json\n" + "y" * 200
    dup = "d" * 5000
    events = [
        {"role": "tool", "command": "pytest tests/unit -q", "exit_code": 1, "content": big},
        {"role": "tool", "content": dup},
        {"role": "assistant", "content": dup},
        {"role": "user", "content": "u" * 100000},
        {"role": "tool", "content": "small"},
    ]
    rewritten, references = pointerize_large_outputs(events, store)

    record = rewritten[0]["content"]
    assert record["type"] == "tool_output_reference"
    assert record["byte_size"] == len(big.encode("utf-8"))
    assert record["line_count"] == big.count("\n") + 1
    assert record["preview_head"].startswith("xxx")
    assert record["preview_tail"].endswith("yyy")
    assert any("FileNotFoundError" in line for line in record["exact_error_strings"])
    assert record["full_output_available"] is True
    # The operational envelope stays inline on the event itself.
    assert rewritten[0]["command"] == "pytest tests/unit -q"
    assert rewritten[0]["exit_code"] == 1
    # Exact duplicates below the size threshold are pointerized to one blob.
    assert rewritten[1]["content"]["artifact_ref"] == rewritten[2]["content"]["artifact_ref"]
    # User content is never pointerized; small unique strings stay inline.
    assert rewritten[3]["content"] == "u" * 100000
    assert rewritten[4]["content"] == "small"
    assert len(references) == 2
    big_digest = sha256_bytes(big.encode("utf-8"))
    assert store.path_for(big_digest).read_text(encoding="utf-8") == big


def test_summary_cache_prevents_resummarization(tmp_path: Path, rollout_events: list[dict]) -> None:
    raw = write_rollout(tmp_path / "rollout.jsonl", rollout_events)
    summarizer = CountingSummarizer()
    first = initialize_handoff_context(
        session_id="s1",
        raw_jsonl_path=raw,
        workspace_root=tmp_path / "ws",
        summarizer=summarizer,
    )
    assert summarizer.calls == 2
    second = initialize_handoff_context(
        session_id="s1",
        raw_jsonl_path=raw,
        workspace_root=tmp_path / "ws",
        summarizer=summarizer,
    )
    assert summarizer.calls == 2
    assert first.context.context_text == second.context.context_text
    for reference in first.images.values():
        assert reference.summary_status == SummaryStatus.AVAILABLE
        assert reference.summary.startswith("Summary of")
        assert reference.summary_model == "gemma4-vision"


def test_initialize_handoff_context_contract(tmp_path: Path, rollout_events: list[dict]) -> None:
    raw = write_rollout(tmp_path / "rollout.jsonl", rollout_events)
    session = initialize_handoff_context(
        session_id="s1",
        raw_jsonl_path=raw,
        workspace_root=tmp_path / "ws",
        raw_tail_event_count=2,
    )
    context = session.context
    assert context.schema_version == "initialized_agent_context.v1"
    assert context.enabled_tools == [RESOLVE_ARTIFACT_TOOL_NAME]
    assert context.token_count > 0
    assert context.original_bytes > context.rewritten_bytes
    assert len(context.artifact_refs) == 2
    assert "base64" not in context.context_text
    # The raw tail keeps the last events verbatim.
    tail_event = json.dumps(
        {k: v for k, v in rollout_events[-1].items()}, sort_keys=True, ensure_ascii=False
    )
    assert tail_event in context.context_text
    # Head user prompts are preserved verbatim in compacted history.
    assert "Please look at this screenshot" in context.context_text


def test_resolver_detail_levels(
    tmp_path: Path, rollout_events: list[dict], png_bytes: bytes
) -> None:
    raw = write_rollout(tmp_path / "rollout.jsonl", rollout_events)
    session = initialize_handoff_context(
        session_id="s1",
        raw_jsonl_path=raw,
        workspace_root=tmp_path / "ws",
        ocr_adapter=FakeOcr(),
    )
    ref = f"{ARTIFACT_REF_SCHEME}{sha256_bytes(png_bytes)}"

    full = session.resolver.resolve(ref, ResolveDetail.FULL)
    assert full.local_path is not None
    assert sha256_file(full.local_path) == sha256_bytes(png_bytes)

    ocr = session.resolver.resolve(ref, ResolveDetail.OCR)
    assert ocr.text is not None and ocr.text.startswith("ocr:")

    with pytest.raises(ArtifactResolutionError):
        session.resolver.resolve(ref, ResolveDetail.THUMBNAIL)
    with pytest.raises(ArtifactResolutionError):
        session.resolver.resolve(f"{ARTIFACT_REF_SCHEME}{'0' * 64}")
    with pytest.raises(ArtifactResolutionError):
        session.resolver.resolve("not-a-ref")


def test_resolver_serves_tool_output_text_without_adapter(tmp_path: Path) -> None:
    big = "log line\n" * 8000
    events = [{"role": "tool", "content": big}, {"role": "user", "content": "done"}]
    raw = write_rollout(tmp_path / "rollout.jsonl", events)
    session = initialize_handoff_context(
        session_id="s-tool",
        raw_jsonl_path=raw,
        workspace_root=tmp_path / "ws",
    )
    assert len(session.tool_outputs) == 1
    ref = next(iter(session.tool_outputs.values())).artifact_ref
    resolved = session.resolver.resolve(ref, ResolveDetail.OCR)
    assert resolved.text == big
    assert resolved.mime_type == "text/plain"


def test_export_bundle_layout_and_portability(tmp_path: Path, rollout_events: list[dict]) -> None:
    raw = write_rollout(tmp_path / "rollout.jsonl", rollout_events)
    bundle = export_handoff_bundle(
        session_id="019f8a41-test",
        raw_jsonl_path=raw,
        output_root=tmp_path / "handoffs",
        summarizer=CountingSummarizer(),
    )
    bundle_dir = bundle.bundle_dir
    assert (bundle_dir / "handoff.jsonl").exists()
    assert (bundle_dir / "manifest.json").exists()
    assert (bundle_dir / "summary.md").exists()
    assert len(list((bundle_dir / "artifacts").iterdir())) == 2
    assert not (bundle_dir / ".blobs").exists()
    assert not (bundle_dir / ".summaries").exists()

    transcript = (bundle_dir / "handoff.jsonl").read_text(encoding="utf-8")
    assert "base64" not in transcript
    assert str(tmp_path) not in transcript
    assert "/Users/" not in transcript

    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "handoff_manifest.v1"
    for entry in manifest["artifacts"].values():
        assert not Path(entry["relative_path"]).is_absolute()
        blob = bundle_dir / entry["relative_path"]
        assert sha256_file(blob) == entry["sha256"]
        assert blob.stat().st_size == entry["byte_size"]
    assert len(manifest["image_references"]) == 2
    assert all(ref["summary"] for ref in manifest["image_references"])
    assert all(ref["summary_status"] == "available" for ref in manifest["image_references"])

    summary_md = (bundle_dir / "summary.md").read_text(encoding="utf-8")
    assert "Unique image artifacts: 2" in summary_md


def test_verify_bundle_detects_corruption(tmp_path: Path, rollout_events: list[dict]) -> None:
    raw = write_rollout(tmp_path / "rollout.jsonl", rollout_events)
    bundle = export_handoff_bundle(
        session_id="s-corrupt",
        raw_jsonl_path=raw,
        output_root=tmp_path / "handoffs",
    )
    blob = next((bundle.bundle_dir / "artifacts").iterdir())
    blob.write_bytes(blob.read_bytes() + b"tampered")
    with pytest.raises(HandoffIntegrityError, match="hash mismatch"):
        verify_handoff_bundle(bundle.bundle_dir)


def test_verify_bundle_detects_unmanifested_ref(tmp_path: Path, rollout_events: list[dict]) -> None:
    raw = write_rollout(tmp_path / "rollout.jsonl", rollout_events)
    bundle = export_handoff_bundle(
        session_id="s-unmanifested",
        raw_jsonl_path=raw,
        output_root=tmp_path / "handoffs",
    )
    with (bundle.bundle_dir / "handoff.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"content": f"{ARTIFACT_REF_SCHEME}{'f' * 64}"}) + "\n")
    with pytest.raises(HandoffIntegrityError, match="unmanifested"):
        verify_handoff_bundle(bundle.bundle_dir)


def test_externalize_session_in_place(tmp_path: Path, rollout_events: list[dict]) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    session_path = write_rollout(session_dir / "session.jsonl", rollout_events)
    original_text = session_path.read_text(encoding="utf-8")

    result = externalize_session_in_place(session_jsonl_path=session_path)

    replaced_text = session_path.read_text(encoding="utf-8")
    assert "base64," not in replaced_text
    assert result.generation.generation == 1
    assert result.image_count == 2
    assert result.original_bytes > result.rewritten_bytes
    assert not session_path.with_name("session.jsonl.compacting").exists()
    assert not session_path.with_name("session.jsonl.lock").exists()
    # The replaced file parses and its records are intact JSON objects.
    assert len(parse_rollout_jsonl(replaced_text)) == len(rollout_events)
    # Blobs live once in the sibling content-addressed store.
    assert len(list((session_dir / "artifacts").glob("sha256-*"))) == 2
    # The prior generation is backed up verbatim.
    backup = session_dir / "backups" / "session-g0.jsonl"
    assert backup.read_text(encoding="utf-8") == original_text
    # The generation record identifies the exact canonical bytes.
    record = json.loads((session_dir / "current-context.json").read_text(encoding="utf-8"))
    assert record["schema_version"] == "context_generation.v1"
    assert record["generation"] == 1
    assert record["sha256"] == sha256_file(session_path)
    assert record["record_count"] == len(rollout_events)


def test_in_place_promotion_is_idempotent_with_bounded_backups(
    tmp_path: Path, rollout_events: list[dict]
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    session_path = write_rollout(session_dir / "session.jsonl", rollout_events)

    first = externalize_session_in_place(session_jsonl_path=session_path)
    text_after_first = session_path.read_text(encoding="utf-8")
    second = externalize_session_in_place(session_jsonl_path=session_path)
    third = externalize_session_in_place(session_jsonl_path=session_path)

    assert (first.generation.generation, second.generation.generation) == (1, 2)
    assert third.generation.generation == 3
    # Re-promotion of an already-externalized file is a byte-stable no-op.
    assert session_path.read_text(encoding="utf-8") == text_after_first
    backups = sorted(path.name for path in (session_dir / "backups").glob("session-g*.jsonl"))
    assert backups == ["session-g1.jsonl", "session-g2.jsonl"]


def test_in_place_promotion_refuses_concurrent_lock(
    tmp_path: Path, rollout_events: list[dict]
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    session_path = write_rollout(session_dir / "session.jsonl", rollout_events)
    lock_path = session_path.with_name("session.jsonl.lock")
    lock_path.write_text("12345", encoding="utf-8")
    original_text = session_path.read_text(encoding="utf-8")

    with pytest.raises(HandoffIntegrityError, match="locked"):
        externalize_session_in_place(session_jsonl_path=session_path)

    assert session_path.read_text(encoding="utf-8") == original_text
    assert lock_path.exists()


def test_deterministic_compaction_dedupes_and_truncates(tmp_path: Path) -> None:
    repeated = {"role": "tool", "content": "same output"}
    events = [repeated, dict(repeated), {"role": "assistant", "content": "y" * 5000}]
    raw = write_rollout(tmp_path / "rollout.jsonl", events + [{"role": "user", "content": "tail"}])
    session = initialize_handoff_context(
        session_id="s-compact",
        raw_jsonl_path=raw,
        workspace_root=tmp_path / "ws",
        raw_tail_event_count=1,
    )
    text = session.context.context_text
    assert "[1 exact duplicate events dropped]" in text
    assert '"tail"' in text


def test_summary_store_roundtrip(tmp_path: Path) -> None:
    cache = SummaryCache(tmp_path / "summaries")
    summary = ImageSummary(
        summary="a terminal",
        visible_text=["error"],
        summary_model="m",
        summary_version="v1",
    )
    assert cache.get("a" * 64, "m", "v1") is None
    cache.put("a" * 64, "m", "v1", summary)
    loaded = cache.get("a" * 64, "m", "v1")
    assert loaded == summary
    assert cache.get("a" * 64, "other-model", "v1") is None
