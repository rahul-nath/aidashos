# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Downscaling before OCR is decided by the image's long edge and the pixel
budget of the model that will read it, not by file size."""

import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from local_first_agent_os.contracts import ModelRole, ModelSpec
from local_first_agent_os.model_registry import DEFAULT_MODELS
from local_first_agent_os.workflow.knowledge import KnowledgeWorkflowMixin

pytestmark = pytest.mark.skipif(
    shutil.which("sips") is None, reason="requires the macOS sips utility"
)


def _write_image(path: Path, width: int, height: int, fmt: str = "png") -> Path:
    subprocess.run(
        [
            "sips",
            "-s",
            "format",
            fmt,
            "-z",
            str(height),
            str(width),
            "/System/Library/CoreServices/DefaultDesktop.heic",
            "--out",
            str(path),
        ],
        capture_output=True,
        check=False,
    )
    if not path.is_file():
        pytest.skip("no system image available to build a fixture from")
    return path


def test_long_edge_is_read_from_pixels(tmp_path: Path) -> None:
    image = _write_image(tmp_path / "wide.png", 400, 200)
    assert KnowledgeWorkflowMixin._image_long_edge(image) == 400


def test_long_edge_is_none_for_unreadable_file(tmp_path: Path) -> None:
    junk = tmp_path / "not-an-image.png"
    junk.write_bytes(b"not a png")
    assert KnowledgeWorkflowMixin._image_long_edge(junk) is None


def test_compressed_high_resolution_photo_is_caught(tmp_path: Path) -> None:
    """The case the old byte threshold missed: a well-compressed photo whose
    resolution far exceeds what the model consumes, while its file size sits
    under any reasonable byte limit."""
    image = _write_image(tmp_path / "big.jpg", 4000, 3000, fmt="jpeg")
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
