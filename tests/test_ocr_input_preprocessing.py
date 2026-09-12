# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Downscaling before OCR is decided by the image's long edge and the pixel
budget of the model that will read it, not by file size."""

import os
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pytest
from host_test_scope import require_uncontained_scope
from pydantic import ValidationError

from local_first_agent_os.contracts import ModelRole, ModelSpec
from local_first_agent_os.model_registry import DEFAULT_MODELS
from local_first_agent_os.workflow.knowledge import KnowledgeWorkflowMixin


@pytest.fixture
def host_image_metadata() -> None:
    """Real sips metadata uses host image services outside the verifier's command scope."""
    required_flag = "AIDASHOS_REQUIRE_HOST_IMAGE_METADATA"
    require_uncontained_scope(
        reason="real sips image metadata requires host image services",
        required_flag=required_flag,
    )
    if shutil.which("sips") is None:
        if os.environ.get(required_flag) == "1":
            pytest.fail("required macOS sips utility is unavailable", pytrace=False)
        pytest.skip("requires the macOS sips utility")


def _write_image(path: Path, width: int, height: int) -> Path:
    """Write a complete, highly compressible RGB PNG without external decoding."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    pixels = (b"\x00" + b"\x00\x00\x00" * width) * height
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )
    return path


def test_long_edge_is_read_from_pixels(host_image_metadata, tmp_path: Path) -> None:
    image = _write_image(tmp_path / "wide.png", 400, 200)
    assert KnowledgeWorkflowMixin._image_long_edge(image) == 400


def test_long_edge_is_none_for_unreadable_file(tmp_path: Path) -> None:
    junk = tmp_path / "not-an-image.png"
    junk.write_bytes(b"not a png")
    assert KnowledgeWorkflowMixin._image_long_edge(junk) is None


def test_long_edge_timeout_preserves_the_unreadable_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from local_first_agent_os.workflow import knowledge

    image = _write_image(tmp_path / "wide.png", 400, 200)
    observed = []

    def time_out(command, *, capture_output, text, timeout, check):
        observed.append(command)
        assert command[1:] == ["-g", "pixelWidth", "-g", "pixelHeight", str(image)]
        assert capture_output and text and not check
        assert timeout == 30
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(knowledge.shutil, "which", lambda name: "/fixture/sips")
    monkeypatch.setattr(knowledge.subprocess, "run", time_out)
    assert KnowledgeWorkflowMixin._image_long_edge(image) is None
    assert len(observed) == 1


def test_compressed_high_resolution_image_is_caught(host_image_metadata, tmp_path: Path) -> None:
    """The case the old byte threshold missed: a well-compressed image whose
    resolution far exceeds what the model consumes, while its file size sits
    under any reasonable byte limit."""
    image = _write_image(tmp_path / "big.png", 4000, 3000)
    long_edge = KnowledgeWorkflowMixin._image_long_edge(image)
    assert long_edge == 4000
    assert long_edge > 2048
    # The property that matters: a size-based rule would not have fired here.
    assert image.stat().st_size < 8_000_000


def test_registry_declares_a_pixel_budget_for_every_ocr_role() -> None:
    """Both OCR roles must carry their own ceiling; falling through to the
    directive default would reintroduce a single global number."""
    ocr_roles = {ModelRole.OCR, ModelRole.HARD_OCR}
    ocr_specs = [spec for spec in DEFAULT_MODELS if spec.role in ocr_roles]
    assert len(ocr_specs) == len(ocr_roles)
    for spec in ocr_specs:
        assert spec.ocr_max_dimension is not None, spec.alias
        assert spec.ocr_max_dimension >= 256


def test_reasoning_format_is_declared_where_the_model_needs_it() -> None:
    chandra = next(s for s in DEFAULT_MODELS if s.role == ModelRole.HARD_OCR)
    assert chandra.reasoning_format == "none"


def test_reasoning_format_rejects_unknown_modes() -> None:
    # The literal type already rejects this statically; the test covers the
    # registry TOML path, where the value arrives untyped and only validation
    # stands between a typo and a silently mis-parsed model reply.
    with pytest.raises(ValidationError):
        ModelSpec(
            alias="x",
            role=ModelRole.OCR,
            model_id="x",
            server_model_name="x",
            reasoning_format="chain-of-thought",  # pyright: ignore[reportArgumentType]
        )
