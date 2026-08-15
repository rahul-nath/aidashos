# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from local_first_agent_os.contracts import ArtifactRole, ModelCallRequest, ModelRole
from local_first_agent_os.model_manager import VisualInputRoutingError

VALID_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def _import_png(runtime, tmp_path: Path, workflow_id: str):
    image = tmp_path / "screen.png"
    image.write_bytes(VALID_ONE_PIXEL_PNG)
    return runtime.artifact_store.import_file(
        role=ArtifactRole.SOURCE_IMAGE.value,
        source_path=image,
        workflow_id=workflow_id,
        schema_version="source_image.v1",
    )


def test_visual_input_to_text_only_role_fails_closed(runtime, tmp_path: Path) -> None:
    """An image routed to a role without a projector must die loudly, never
    produce a plausible text answer that silently ignored the image."""
    artifact = _import_png(runtime, tmp_path, "wf-visual-routing-general")
    with pytest.raises(VisualInputRoutingError):
        runtime.model_manager.call_model(
            ModelCallRequest(
                workflow_id="wf-visual-routing-general",
                model_role=ModelRole.GENERAL,
                input_artifact_id=artifact.artifact_id,
                payload={"prompt": "What does this image show?"},
                params={},
                timeout_seconds=30,
            )
        )


def test_visual_input_to_projector_role_is_accepted(runtime, tmp_path: Path) -> None:
    artifact = _import_png(runtime, tmp_path, "wf-visual-routing-ocr")
    result = runtime.model_manager.call_model(
        ModelCallRequest(
            workflow_id="wf-visual-routing-ocr",
            model_role=ModelRole.OCR,
            input_artifact_id=artifact.artifact_id,
            payload={"prompt": "OCR this image."},
            params={},
            timeout_seconds=30,
        )
    )
    assert result.output_artifact is not None


def test_surya_message_places_image_before_training_prompt(runtime, tmp_path: Path) -> None:
    artifact = _import_png(runtime, tmp_path, "wf-surya-order")
    model = runtime.model_registry.resolve_model(ModelRole.OCR)
    messages = runtime.model_manager._messages_for_request(
        ModelCallRequest(
            workflow_id="wf-surya-order",
            model_role=ModelRole.OCR,
            input_artifact_id=artifact.artifact_id,
            payload={"prompt": "OCR this image to HTML."},
            params={},
            timeout_seconds=30,
        ),
        artifact,
        model,
    )

    content = messages[0]["content"]
    assert [part["type"] for part in content] == ["image_url", "text"]
