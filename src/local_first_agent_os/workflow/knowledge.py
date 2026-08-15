# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""KnowledgeWorkflow methods split from the workflow facade."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from ..capability_gate import SystemWorkflow
from ..constants import DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS
from ..contracts import (
    ArtifactRef,
    ArtifactRole,
    IngressEvent,
    ModelCallRequest,
    ModelRole,
    PiTask,
    Stage,
    WorkflowResult,
    WorkflowStatus,
    WorkflowType,
)
from ..directives import DirectiveParser
from ..directives_help import help_payload
from ..ids import sha256_text
from .base import WorkflowMixinBase
from .core import (
    build_completed_workflow_result,
)

logger = logging.getLogger(__name__)


class _SpatialHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.regions: list[dict[str, Any]] = []
        self._active: dict[str, Any] | None = None
        self._depth = 0
        self._text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._active is not None:
            self._depth += 1
            return
        values = dict(attrs)
        bbox_text = values.get("data-bbox")
        if bbox_text is None:
            return
        try:
            bbox = [float(value) for value in bbox_text.split()]
        except ValueError:
            return
        if len(bbox) != 4:
            return
        self._active = {
            "bbox": [int(value) if value.is_integer() else value for value in bbox],
            "label": values.get("data-label"),
        }
        self._depth = 1
        self._text_parts = []

    def handle_endtag(self, tag: str) -> None:
        if self._active is None:
            return
        self._depth -= 1
        if self._depth > 0:
            return
        text = " ".join("".join(self._text_parts).split())
        self.regions.append({**self._active, "text": text})
        self._active = None
        self._text_parts = []

    def handle_data(self, data: str) -> None:
        if self._active is not None:
            self._text_parts.append(data)


SURYA_OCR_PROMPT_VERSION = "surya_high_accuracy_bbox_with_block_fallback.v1"
SURYA_OCR_PROMPT = (
    "OCR this image to HTML. Each block is a div with data-label and data-bbox "
    "(x0 y0 x1 y1, normalized 0-1000)."
)
SURYA_BLOCK_OCR_PROMPT = "OCR this block image to HTML."
CHANDRA_OCR_PROMPT_VERSION = "chandra_native_ocr.v2"
CHANDRA_OCR_PROMPT = "OCR this image. Preserve text faithfully."


class KnowledgeWorkflowMixin(WorkflowMixinBase):
    def ocr_capture(self, event: IngressEvent) -> WorkflowResult:
        """Transcribe one image or a recursive image directory without indexing it."""
        workflow_id = self._start(WorkflowType.OCR_CAPTURE, event)
        parser = DirectiveParser(self.runtime.settings)
        directive = str(event.payload.get("directive", ""))
        try:
            spec = parser.parse(directive)
        except Exception as exc:
            return self._fail_ocr_capture(workflow_id, directive, str(exc), parser)
        if spec.action != "ocr_capture" or spec.path is None:
            return self._fail_ocr_capture(
                workflow_id,
                directive,
                "/ocr requires exactly one absolute image or directory path.",
                parser,
            )

        root = spec.path.resolve()
        if not root.exists():
            return self._fail_ocr_capture(
                workflow_id,
                directive,
                f"/ocr requires an existing image or directory: {root}",
                parser,
            )
        paths = [root] if root.is_file() else sorted(root.rglob("*"))
        candidates = [
            path
            for path in paths
            if path.is_file() and path.suffix.lower() in parser.ocr_image_extensions
        ]
        if not candidates:
            return self._fail_ocr_capture(
                workflow_id,
                directive,
                f"/ocr found no supported image files under: {root}",
                parser,
            )
        model_role = spec.model_role or ModelRole.OCR
        prompt, prompt_version = self._ocr_prompt_for_role(model_role)
        try:
            self.runtime.model_manager.require_loaded(model_role)
        except Exception as exc:
            return self._fail_ocr_capture(workflow_id, directive, str(exc), parser)
        # The pixel budget belongs to the model that will read the image; the
        # directive default only applies to roles that declare none.
        ocr_model = self.runtime.model_registry.resolve_model(model_role)
        max_dimension = ocr_model.ocr_max_dimension or parser.ocr_max_dimension

        artifacts: list[ArtifactRef] = []
        items: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        failures: list[dict[str, str]] = []
        for index, path in enumerate(candidates):
            size_bytes = path.stat().st_size
            if size_bytes > parser.ocr_max_file_bytes:
                skipped.append(
                    {
                        "source_path": str(path),
                        "reason": (
                            f"file is {size_bytes} bytes; limit is "
                            f"{parser.ocr_max_file_bytes} bytes"
                        ),
                    }
                )
                continue
            try:
                model_reloaded_before_ocr = index > 0 and model_role == ModelRole.HARD_OCR
                if model_reloaded_before_ocr:
                    self.runtime.model_manager.reload(model_role)
                source_artifact = self.runtime.artifact_store.import_file(
                    role=ArtifactRole.SOURCE_IMAGE.value,
                    source_path=path,
                    workflow_id=workflow_id,
                    schema_version="source_image.v1",
                )
                artifacts.append(source_artifact)
                ocr_input_artifact, preprocessing = self._prepare_ocr_input(
                    path,
                    source_artifact=source_artifact,
                    workflow_id=workflow_id,
                    max_dimension=max_dimension,
                )
                if ocr_input_artifact.artifact_id != source_artifact.artifact_id:
                    artifacts.append(ocr_input_artifact)
                model_result = self.runtime.model_manager.call_model(
                    ModelCallRequest(
                        workflow_id=workflow_id,
                        model_role=model_role,
                        input_artifact_id=ocr_input_artifact.artifact_id,
                        payload={"prompt": prompt},
                        params={
                            "temperature": 0,
                            "max_tokens": 2048,
                            "cache_prompt": False,
                        },
                        timeout_seconds=DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
                    )
                )
                artifacts.append(model_result.output_artifact)
                model_output_artifact_ids = [model_result.output_artifact.artifact_id]
                model_invocation_ids = [model_result.invocation_id]
                model_payload = self.runtime.artifact_store.read_json(
                    model_result.output_artifact.artifact_id
                )
                raw_output = model_payload.get("output", {})
                transcription = self._ocr_transcription_text(raw_output)
                if not transcription.strip():
                    model = self.runtime.model_registry.resolve_model(model_role)
                    raise RuntimeError(
                        f"{model.model_id} returned an empty transcription for {path}"
                    )
                incomplete_trailing_region_removed = False
                if model_role == ModelRole.HARD_OCR:
                    transcription, incomplete_trailing_region_removed = (
                        self._trim_incomplete_trailing_div(transcription)
                    )
                output_format = self._ocr_output_format(raw_output, transcription)
                spatial_regions = self._ocr_spatial_regions(
                    transcription,
                    source_artifact_id=source_artifact.artifact_id,
                    captured_at=event.detected_at.isoformat(),
                    confidence=(
                        raw_output.get("confidence")
                        if isinstance(raw_output, dict)
                        and isinstance(raw_output.get("confidence"), int | float)
                        else None
                    ),
                )
                surya_block_fallback_used = False
                if model_role == ModelRole.OCR and self._surya_needs_block_fallback(
                    transcription, spatial_regions
                ):
                    fallback_result = self.runtime.model_manager.call_model(
                        ModelCallRequest(
                            workflow_id=workflow_id,
                            model_role=model_role,
                            input_artifact_id=ocr_input_artifact.artifact_id,
                            payload={"prompt": SURYA_BLOCK_OCR_PROMPT},
                            params={
                                "temperature": 0,
                                "max_tokens": 4096,
                                "cache_prompt": False,
                            },
                            timeout_seconds=DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
                        )
                    )
                    artifacts.append(fallback_result.output_artifact)
                    model_output_artifact_ids.append(fallback_result.output_artifact.artifact_id)
                    model_invocation_ids.append(fallback_result.invocation_id)
                    fallback_payload = self.runtime.artifact_store.read_json(
                        fallback_result.output_artifact.artifact_id
                    )
                    fallback_raw_output = fallback_payload.get("output", {})
                    fallback_transcription = self._ocr_transcription_text(fallback_raw_output)
                    if fallback_transcription.strip():
                        model_result = fallback_result
                        raw_output = fallback_raw_output
                        transcription = fallback_transcription
                        output_format = self._ocr_output_format(raw_output, transcription)
                        surya_block_fallback_used = True
                ocr_artifact = self.runtime.artifact_store.write_json(
                    role=ArtifactRole.OCR_TEXT.value,
                    payload={
                        "schema_version": "ocr_capture_item.v1",
                        "source_path": str(path),
                        "source_artifact_id": source_artifact.artifact_id,
                        "ocr_input_artifact_id": ocr_input_artifact.artifact_id,
                        "model_output_artifact_id": model_result.output_artifact.artifact_id,
                        "model_output_artifact_ids": model_output_artifact_ids,
                        "model_invocation_id": model_result.invocation_id,
                        "model_invocation_ids": model_invocation_ids,
                        "model_id": model_result.model_id,
                        "model_role": model_role.value,
                        "prompt_version": prompt_version,
                        "output_format": output_format,
                        "transcription": transcription,
                        "spatial_regions": spatial_regions,
                        "surya_block_fallback_used": surya_block_fallback_used,
                        "incomplete_trailing_region_removed": (incomplete_trailing_region_removed),
                        "preprocessing": preprocessing,
                        "model_reloaded_before_ocr": model_reloaded_before_ocr,
                    },
                    workflow_id=workflow_id,
                    schema_version="ocr_capture_item.v1",
                )
                artifacts.append(ocr_artifact)
                items.append(
                    {
                        "source_path": str(path),
                        "source_artifact_id": source_artifact.artifact_id,
                        "ocr_input_artifact_id": ocr_input_artifact.artifact_id,
                        "model_output_artifact_id": model_result.output_artifact.artifact_id,
                        "model_output_artifact_ids": model_output_artifact_ids,
                        "ocr_artifact_id": ocr_artifact.artifact_id,
                        "model_invocation_id": model_result.invocation_id,
                        "model_invocation_ids": model_invocation_ids,
                        "model_id": model_result.model_id,
                        "output_format": output_format,
                        "spatial_region_count": len(spatial_regions),
                        "surya_block_fallback_used": surya_block_fallback_used,
                        "incomplete_trailing_region_removed": (incomplete_trailing_region_removed),
                        "preprocessing": preprocessing,
                        "model_reloaded_before_ocr": model_reloaded_before_ocr,
                    }
                )
            except Exception as exc:
                logger.exception("ocr_capture_item_failed", extra={"source_path": str(path)})
                failures.append({"source_path": str(path), "error": str(exc)})

        if not items:
            detail = failures[0]["error"] if failures else "all candidate images exceeded the limit"
            return self._fail_ocr_capture(
                workflow_id,
                directive,
                f"/ocr did not successfully transcribe any images: {detail}",
                parser,
                artifacts=artifacts,
            )

        manifest = self.runtime.artifact_store.write_json(
            role=ArtifactRole.OCR_BATCH_MANIFEST.value,
            payload={
                "schema_version": "ocr_batch_manifest.v1",
                "root": str(root),
                "model_role": model_role.value,
                "prompt_version": prompt_version,
                "recursive": root.is_dir(),
                "images_transcribed": items,
                "images_skipped": skipped,
                "images_failed": failures,
                "retrieval_indexed": False,
                "semantic_interpretation_performed": False,
            },
            workflow_id=workflow_id,
            schema_version="ocr_batch_manifest.v1",
        )
        artifacts.append(manifest)
        status = WorkflowStatus.MANUAL_REVIEW if failures else WorkflowStatus.COMPLETED
        stage = Stage.MANUAL_REVIEW if failures else Stage.COMPLETED
        self.runtime.repository.update_workflow(
            workflow_id,
            status=status,
            stage=stage,
            error=f"{len(failures)} image(s) failed OCR" if failures else None,
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.OCR_CAPTURE,
            status,
            stage,
            artifacts,
            manual_review_reason=(
                f"{len(failures)} image(s) failed OCR; inspect the batch manifest."
                if failures
                else None
            ),
        )

    @staticmethod
    def _ocr_prompt_for_role(model_role: ModelRole) -> tuple[str, str]:
        if model_role == ModelRole.HARD_OCR:
            return CHANDRA_OCR_PROMPT, CHANDRA_OCR_PROMPT_VERSION
        if model_role == ModelRole.OCR:
            return SURYA_OCR_PROMPT, SURYA_OCR_PROMPT_VERSION
        raise ValueError(f"Unsupported OCR model role: {model_role.value}")

    @staticmethod
    def _surya_needs_block_fallback(
        transcription: str,
        spatial_regions: list[dict[str, Any]],
    ) -> bool:
        if not spatial_regions:
            return True
        if transcription.rfind("<div") > transcription.rfind("</div>"):
            return True
        lines = [line.strip() for line in transcription.splitlines() if line.strip()]
        if any(lines.count(line) >= 8 for line in set(lines)):
            return True
        return re.search(r"(.)\1{31,}", transcription, flags=re.DOTALL) is not None

    @staticmethod
    def _trim_incomplete_trailing_div(transcription: str) -> tuple[str, bool]:
        """Discard an unterminated final spatial block instead of persisting its tail."""
        last_open = transcription.rfind("<div")
        if last_open <= transcription.rfind("</div>"):
            return transcription, False
        return transcription[:last_open].rstrip(), True

    @staticmethod
    def _ocr_transcription_text(raw_output: Any) -> str:
        if isinstance(raw_output, dict):
            text = raw_output.get("text")
            if isinstance(text, str):
                return text
            return json.dumps(raw_output, indent=2, sort_keys=True, ensure_ascii=False)
        if isinstance(raw_output, str):
            return raw_output
        return json.dumps(raw_output, indent=2, sort_keys=True, ensure_ascii=False)

    @staticmethod
    def _image_long_edge(path: Path) -> int | None:
        """Return the image's longest side in pixels.

        Returns None when the dimensions cannot be read, which leaves the image
        untouched: oversized input costs decode time but still transcribes, so a
        host without `sips` degrades in speed rather than failing outright.
        """
        sips = shutil.which("sips")
        if sips is None:
            return None
        completed = subprocess.run(
            [sips, "-g", "pixelWidth", "-g", "pixelHeight", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            return None
        sides = [
            int(value) for value in re.findall(r"pixel(?:Width|Height):\s*(\d+)", completed.stdout)
        ]
        return max(sides) if sides else None

    def _prepare_ocr_input(
        self,
        path: Path,
        *,
        source_artifact: ArtifactRef,
        workflow_id: str,
        max_dimension: int,
    ) -> tuple[ArtifactRef, dict[str, Any]]:
        normalize_suffixes = {".heic", ".tif", ".tiff"}
        reasons: list[str] = []
        if path.suffix.lower() in normalize_suffixes:
            reasons.append(f"normalize {path.suffix.lower()} to PNG")
        # Resolution, not file size, decides whether pixels are wasted: a
        # well-compressed photo can carry more detail than the model consumes
        # while sitting well under any byte threshold.
        long_edge = self._image_long_edge(path)
        if long_edge is not None and long_edge > max_dimension:
            reasons.append(f"long edge {long_edge}px exceeds {max_dimension}px")
        if not reasons:
            return source_artifact, {
                "applied": False,
                "long_edge": long_edge,
                "max_dimension": max_dimension,
                "ocr_input_artifact_id": source_artifact.artifact_id,
            }
        reason = "; ".join(reasons)

        sips = shutil.which("sips")
        if sips is None:
            raise RuntimeError(
                "OCR input preprocessing requires the macOS sips utility for large, "
                "HEIC, or TIFF images."
            )
        output_dir = self.runtime.settings.spool_dir / "ocr_inputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{source_artifact.sha256}.png"
        completed = subprocess.run(
            [
                sips,
                "-s",
                "format",
                "png",
                "-Z",
                str(max_dimension),
                str(path),
                "--out",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0 or not output_path.is_file():
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"Failed to prepare OCR input for {path}: {detail}")
        ocr_input_artifact = self.runtime.artifact_store.import_file(
            role=ArtifactRole.OCR_INPUT_IMAGE.value,
            source_path=output_path,
            workflow_id=workflow_id,
            schema_version="ocr_input_image.v1",
        )
        return ocr_input_artifact, {
            "applied": True,
            "reason": reason,
            "format": "png",
            "long_edge": long_edge,
            "max_dimension": max_dimension,
            "ocr_input_artifact_id": ocr_input_artifact.artifact_id,
        }

    @staticmethod
    def _ocr_output_format(raw_output: Any, transcription: str) -> str:
        if isinstance(raw_output, dict) and not isinstance(raw_output.get("text"), str):
            return "json"
        stripped = transcription.lstrip().lower()
        if stripped.startswith("<") and "data-bbox" in stripped:
            return "html_with_bboxes"
        if stripped.startswith("<"):
            return "html"
        return "markdown_or_plain_text"

    @staticmethod
    def _ocr_spatial_regions(
        transcription: str,
        *,
        source_artifact_id: str,
        captured_at: str,
        confidence: int | float | None,
    ) -> list[dict[str, Any]]:
        parser = _SpatialHtmlParser()
        parser.feed(transcription)
        return [
            {
                **region,
                "source_artifact_id": source_artifact_id,
                "timestamp": captured_at,
                "confidence": confidence,
                "depth": None,
            }
            for region in parser.regions
        ]

    def _fail_ocr_capture(
        self,
        workflow_id: str,
        directive: str,
        error: str,
        parser: DirectiveParser,
        *,
        artifacts: list[ArtifactRef] | None = None,
    ) -> WorkflowResult:
        help_block = help_payload(parser, directive, error)
        manifest = self.runtime.artifact_store.write_json(
            role=ArtifactRole.OCR_BATCH_MANIFEST.value,
            payload={
                "schema_version": "ocr_batch_manifest.v1",
                "directive": directive,
                "status": "failed",
                "error": error,
                "retrieval_indexed": False,
                "help": help_block,
            },
            workflow_id=workflow_id,
            schema_version="ocr_batch_manifest.v1",
        )
        result_artifacts = [*(artifacts or []), manifest]
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.FAILED_PERMANENT,
            stage=Stage.COMPLETED,
            error=error,
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.OCR_CAPTURE,
            WorkflowStatus.FAILED_PERMANENT,
            Stage.COMPLETED,
            result_artifacts,
            manual_review_reason=help_block.get("summary"),
            help=help_block,
        )

    def directory_embedding(self, event: IngressEvent) -> WorkflowResult:
        workflow_id = self._start(WorkflowType.DIRECTORY_EMBEDDING, event)
        parser = DirectiveParser(self.runtime.settings)
        directive = str(event.payload.get("directive", ""))
        spec = parser.parse(directive) if directive else None
        raw_path = spec.path if spec and spec.path else Path(str(event.payload["path"]))
        root = raw_path.expanduser().resolve()
        if not root.exists():
            return self._fail_directory_embedding(
                workflow_id,
                directive,
                f"/store requires an existing file or directory: {root}",
                parser,
            )
        self.runtime.model_manager.ensure_loaded(ModelRole.EMBEDDER)
        paths = [root] if root.is_file() else sorted(root.rglob("*"))
        image_candidates = [
            path
            for path in paths
            if path.is_file() and path.suffix.lower() in parser.image_extensions
        ]
        if image_candidates:
            self.runtime.model_manager.ensure_loaded(ModelRole.OCR)
            self.runtime.model_manager.ensure_loaded(ModelRole.GENERAL)
        added = 0
        skipped = 0
        files: list[str] = []
        images: list[str] = []
        image_artifacts: list[str] = []
        for path in paths:
            if not path.is_file():
                continue
            if path.stat().st_size > parser.max_file_bytes:
                skipped += 1
                continue
            suffix = path.suffix.lower()
            if suffix in parser.text_extensions:
                text = path.read_text(encoding="utf-8", errors="replace")
                artifact = self.runtime.artifact_store.write_text(
                    role=ArtifactRole.NORMALIZED_TEXT.value,
                    text=text,
                    workflow_id=workflow_id,
                    schema_version="directory_text.v1",
                    mime_type="text/plain",
                )
                added += self.runtime.retrieval.embed_artifact(
                    artifact,
                    event.workspace_id,
                    workflow_id,
                )
                files.append(str(path))
                continue
            if suffix in parser.image_extensions:
                source_artifact = self.runtime.artifact_store.import_file(
                    role=ArtifactRole.SOURCE_IMAGE.value,
                    source_path=path,
                    workflow_id=workflow_id,
                    schema_version="source_image.v1",
                )
                model_result = self.runtime.model_manager.call_model(
                    ModelCallRequest(
                        workflow_id=workflow_id,
                        model_role=ModelRole.OCR,
                        input_artifact_id=source_artifact.artifact_id,
                        payload={
                            "prompt": (
                                "OCR this screenshot or image. Preserve text faithfully, then "
                                "summarize any non-text visual context as metadata."
                            )
                        },
                        params={"temperature": 0, "max_tokens": 4096},
                        timeout_seconds=DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
                    )
                )
                raw = self.runtime.artifact_store.read_json(
                    model_result.output_artifact.artifact_id
                )
                text = str(raw["output"].get("text", ""))
                image_text = self.runtime.artifact_store.write_text(
                    role=ArtifactRole.NORMALIZED_TEXT.value,
                    text=(
                        f"Image path: {path}\n"
                        f"Image artifact: {source_artifact.artifact_id}\n\n{text}"
                    ),
                    workflow_id=workflow_id,
                    schema_version="image_ocr_text.v1",
                    mime_type="text/plain",
                )
                added += self.runtime.retrieval.embed_artifact(
                    image_text,
                    event.workspace_id,
                    workflow_id,
                )
                images.append(str(path))
                image_artifacts.append(source_artifact.artifact_id)
                continue
            skipped += 1
        if not files and not images:
            return self._fail_directory_embedding(
                workflow_id,
                directive,
                f"/store found no supported text or image files under: {root}",
                parser,
            )
        manifest = self.runtime.artifact_store.write_json(
            role=ArtifactRole.STORE_MANIFEST.value,
            payload={
                "schema_version": "store_manifest.v1",
                "root": str(root),
                "remote_requested": bool(spec.remote if spec else event.payload.get("remote")),
                "files_embedded": files,
                "images_embedded": images,
                "image_artifacts": image_artifacts,
                "files_skipped": skipped,
                "chunks_added": added,
                "future_vision_store_note": (
                    "Image bytes are stored as durable artifacts and OCR text is embedded into "
                    "the current vector store. Future image-native vectors can attach to the "
                    "recorded image_artifacts without changing directive semantics."
                ),
            },
            workflow_id=workflow_id,
            schema_version="store_manifest.v1",
        )
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.COMPLETED,
            stage=Stage.COMPLETED,
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.DIRECTORY_EMBEDDING,
            WorkflowStatus.COMPLETED,
            Stage.COMPLETED,
            [manifest],
        )

    def send_to_workflowy(self, event: IngressEvent) -> WorkflowResult:
        workflow_id = self._start(WorkflowType.SEND_TO_WORKFLOWY, event)
        parser = DirectiveParser(self.runtime.settings)
        directive = str(event.payload.get("directive", ""))
        try:
            spec = parser.parse(directive) if directive else None
        except Exception as exc:
            return self._fail_send_to_wf(workflow_id, directive, str(exc), parser)
        if spec is None or spec.path is None or spec.month_day is None:
            return self._fail_send_to_wf(
                workflow_id,
                directive,
                "/send-to-wf requires a file path and a MM/DD argument.",
                parser,
            )
        path = spec.path.expanduser().resolve()
        if not path.exists() or not path.is_file():
            return self._fail_send_to_wf(
                workflow_id,
                directive,
                f"/send-to-wf could not find the file: {path}",
                parser,
            )
        suffix = path.suffix.lower()
        try:
            if suffix in parser.text_extensions:
                processing = self._send_text_to_workflowy(workflow_id, path)
            elif suffix in parser.audio_extensions:
                processing = self._send_audio_to_workflowy(workflow_id, event, path)
            elif suffix in parser.image_extensions:
                processing = self._send_image_to_workflowy(workflow_id, path)
            else:
                return self._fail_send_to_wf(
                    workflow_id,
                    directive,
                    f"/send-to-wf does not know how to process: {suffix}",
                    parser,
                )
        except Exception as exc:
            return self._fail_send_to_wf(workflow_id, directive, str(exc), parser)

        content = str(processing["content"]).strip()
        content_sha = sha256_text(content + "|" + spec.month_day)
        payload_artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.SEND_TO_WF_PAYLOAD.value,
            payload={
                "schema_version": "send_to_wf_payload.v1",
                "directive": directive,
                "month_day": spec.month_day,
                "source_path": str(path),
                "source_kind": processing["kind"],
                "content": content,
                "image_classification": processing.get("classification"),
                "downstream": processing.get("downstream"),
            },
            workflow_id=workflow_id,
            schema_version="send_to_wf_payload.v1",
        )
        artifacts: list[ArtifactRef] = [payload_artifact]
        try:
            insert_response = self.runtime.tool_registry.run(
                workflow_id=workflow_id,
                workspace_id=event.workspace_id,
                caller=SystemWorkflow(source=event.source_type.value),
                tool_name="workflowy_day_bullet_insert",
                # The one bypass left, and it marks an unresolved contradiction
                # rather than a decision. Two existing tests cannot both hold with
                # this enforced: `test_day_bullet_tool_is_denied_in_general_workspace`
                # says the general workspace must not write today's daily log,
                # and `/send-to-wf <file> 11/15` runs that write from the general
                # workspace. Enforcing denies a dated send-to-wf; granting makes
                # the policy test a lie. Both were true only because nothing
                # checked. Resolving it is an operator decision: either general
                # may write the daily log, or a dated send-to-wf belongs to the
                # workflowy workspace.
                enforce_policy=False,
                payload={
                    "month_day": spec.month_day,
                    "content": content,
                    "content_sha256": content_sha,
                    "source_kind": processing["kind"],
                },
            )
        except Exception as exc:
            return self._fail_send_to_wf(workflow_id, directive, str(exc), parser)
        response_artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.MODEL_OUTPUT.value,
            payload=insert_response,
            workflow_id=workflow_id,
            schema_version="workflowy_day_bullet_insert.v2",
        )
        artifacts.append(response_artifact)
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.COMPLETED,
            stage=Stage.COMPLETED,
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.SEND_TO_WORKFLOWY,
            WorkflowStatus.COMPLETED,
            Stage.COMPLETED,
            artifacts,
        )

    def _send_text_to_workflowy(self, workflow_id: str, path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        artifact = self.runtime.artifact_store.write_text(
            role=ArtifactRole.NORMALIZED_TEXT.value,
            text=text,
            workflow_id=workflow_id,
            schema_version="send_to_wf_text.v1",
            mime_type="text/plain",
        )
        return {
            "kind": "text",
            "content": text,
            "downstream": {"normalized_text_artifact": artifact.artifact_id},
        }

    def _send_audio_to_workflowy(
        self,
        workflow_id: str,
        event: IngressEvent,
        path: Path,
    ) -> dict[str, Any]:
        audio_event = IngressEvent(
            event_id=f"{event.event_id}:audio",
            source_type=event.source_type,
            event_type="file.created",
            workspace_id=event.workspace_id,
            source_uri=f"file://{path}",
            content_sha256=event.content_sha256,
            payload={"source_uri": f"file://{path}"},
        )
        result = self.audio_transcription(audio_event)
        transcript_text = ""
        transcript_artifact_id: str | None = None
        for artifact in result.artifacts:
            if str(artifact.role) == ArtifactRole.TRANSCRIPT.value:
                payload = self.runtime.artifact_store.read_json(artifact.artifact_id)
                transcript_text = str(payload.get("text", "")).strip()
                transcript_artifact_id = artifact.artifact_id
                break
        if not transcript_text:
            transcript_text = (
                f"Audio transcription returned no text for {path.name} "
                f"(workflow status {result.status.value})."
            )
        return {
            "kind": "audio",
            "content": transcript_text,
            "downstream": {
                "audio_workflow_id": result.workflow_id,
                "audio_workflow_status": result.status.value,
                "transcript_artifact_id": transcript_artifact_id,
            },
        }

    def _send_image_to_workflowy(self, workflow_id: str, path: Path) -> dict[str, Any]:
        source_artifact = self.runtime.artifact_store.import_file(
            role=ArtifactRole.SOURCE_IMAGE.value,
            source_path=path,
            workflow_id=workflow_id,
            schema_version="source_image.v1",
        )
        # Only the ocr role can see the image; the junior general model is
        # text-only by registry contract, so it classifies from the vision
        # model's extract rather than from the image itself.
        ocr_result = self.runtime.model_manager.call_model(
            ModelCallRequest(
                workflow_id=workflow_id,
                model_role=ModelRole.OCR,
                input_artifact_id=source_artifact.artifact_id,
                payload={
                    "prompt": (
                        "OCR this image. Preserve text faithfully without commentary. "
                        "If the image contains little or no text, describe what it "
                        "shows in two sentences instead."
                    )
                },
                params={"temperature": 0, "max_tokens": 4096},
                timeout_seconds=DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
            )
        )
        ocr_payload = self.runtime.artifact_store.read_json(ocr_result.output_artifact.artifact_id)
        extract = str(ocr_payload.get("output", {}).get("text", "")).strip()
        classification = self.runtime.model_manager.call_model(
            ModelCallRequest(
                workflow_id=workflow_id,
                model_role=ModelRole.GENERAL,
                input_artifact_id=ocr_result.output_artifact.artifact_id,
                payload={
                    "prompt": self.runtime.pi_prompts.get("image_classifier").render(
                        extract=extract
                    )
                },
                params={"temperature": 0, "max_tokens": 256},
                timeout_seconds=DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
            )
        )
        classification_payload = self.runtime.artifact_store.read_json(
            classification.output_artifact.artifact_id
        )
        classification_data = classification_payload.get("output", {})
        is_text = bool(classification_data.get("is_text"))
        durable_image_uri = source_artifact.uri
        durable_image_sha = source_artifact.sha256
        if is_text:
            content = extract
            kind = "image_text"
            downstream: dict[str, Any] = {
                "image_artifact": source_artifact.artifact_id,
                "image_artifact_uri": durable_image_uri,
                "image_sha256": durable_image_sha,
                "ocr_invocation": ocr_result.invocation_id,
            }
        else:
            content = str(classification_data.get("description", "")).strip()
            kind = "image_picture"
            downstream = {
                "image_artifact": source_artifact.artifact_id,
                "image_artifact_uri": durable_image_uri,
                "image_sha256": durable_image_sha,
            }
        return {
            "kind": kind,
            "content": content or "(model returned no content for image)",
            "classification": classification_data,
            "downstream": downstream,
        }

    def _fail_send_to_wf(
        self,
        workflow_id: str,
        directive: str,
        error: str,
        parser: DirectiveParser,
    ) -> WorkflowResult:
        help_block = help_payload(parser, directive, error)
        artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.SEND_TO_WF_PAYLOAD.value,
            payload={
                "schema_version": "send_to_wf_payload.v1",
                "directive": directive,
                "status": "failed",
                "error": error,
                "help": help_block,
            },
            workflow_id=workflow_id,
            schema_version="send_to_wf_payload.v1",
        )
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.FAILED_PERMANENT,
            stage=Stage.COMPLETED,
            error=error,
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.SEND_TO_WORKFLOWY,
            WorkflowStatus.FAILED_PERMANENT,
            Stage.COMPLETED,
            [artifact],
            manual_review_reason=help_block.get("summary"),
            help=help_block,
        )

    def done_recall(self, event: IngressEvent) -> WorkflowResult:
        workflow_id = self._start(WorkflowType.DONE_RECALL, event)
        parser = DirectiveParser(self.runtime.settings)
        directive = str(event.payload.get("directive", ""))
        try:
            spec = parser.parse(directive) if directive else None
        except Exception as exc:
            help_block = help_payload(parser, directive, str(exc))
            artifact = self.runtime.artifact_store.write_json(
                role=ArtifactRole.DONE_RECALL_RESULT.value,
                payload={
                    "schema_version": "done_recall_result.v1",
                    "directive": directive,
                    "status": "failed",
                    "error": str(exc),
                    "help": help_block,
                },
                workflow_id=workflow_id,
                schema_version="done_recall_result.v1",
            )
            self.runtime.repository.update_workflow(
                workflow_id,
                status=WorkflowStatus.FAILED_PERMANENT,
                stage=Stage.COMPLETED,
                error=str(exc),
            )
            return build_completed_workflow_result(
                workflow_id,
                WorkflowType.DONE_RECALL,
                WorkflowStatus.FAILED_PERMANENT,
                Stage.COMPLETED,
                [artifact],
                manual_review_reason=help_block.get("summary"),
                help=help_block,
            )
        query = (spec.query or "").strip() if spec else ""
        if not query:
            help_block = help_payload(parser, directive, "/done expects a search query.")
            artifact = self.runtime.artifact_store.write_json(
                role=ArtifactRole.DONE_RECALL_RESULT.value,
                payload={
                    "schema_version": "done_recall_result.v1",
                    "directive": directive,
                    "status": "failed",
                    "error": "/done expects a search query.",
                    "help": help_block,
                },
                workflow_id=workflow_id,
                schema_version="done_recall_result.v1",
            )
            self.runtime.repository.update_workflow(
                workflow_id,
                status=WorkflowStatus.FAILED_PERMANENT,
                stage=Stage.COMPLETED,
                error="missing_query",
            )
            return build_completed_workflow_result(
                workflow_id,
                WorkflowType.DONE_RECALL,
                WorkflowStatus.FAILED_PERMANENT,
                Stage.COMPLETED,
                [artifact],
                manual_review_reason=help_block.get("summary"),
                help=help_block,
            )
        hits = self.runtime.retrieval.search(query, workspace_id=None, top_k=8)
        ranked = list(hits[:8])
        snippets = [hit.text[:600] for hit in ranked]
        aggregation_prompt = self.runtime.pi_prompts.get("done_aggregation").render(
            query=query,
            snippets="\n\n".join(f"- {snippet}" for snippet in snippets),
        )
        prompt_artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.PROMPT.value,
            payload={"schema_version": "prompt.v1", "prompt": aggregation_prompt},
            workflow_id=workflow_id,
            schema_version="prompt.v1",
        )
        if self.runtime.model_manager.is_default_fallback_active() or not snippets:
            answer_text = (
                "Default model unavailable; returning vector-store snippets only."
                if self.runtime.model_manager.is_default_fallback_active()
                else "No matching embeddings were found."
            )
            aggregation_artifact = None
        else:
            aggregation_result = self.runtime.model_manager.call_model(
                ModelCallRequest(
                    workflow_id=workflow_id,
                    model_role=ModelRole.GENERAL,
                    input_artifact_id=prompt_artifact.artifact_id,
                    payload={"prompt": aggregation_prompt},
                    params={"temperature": 0.2, "max_tokens": 1024},
                    timeout_seconds=DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
                )
            )
            aggregation_artifact = aggregation_result.output_artifact
            aggregation_payload = self.runtime.artifact_store.read_json(
                aggregation_artifact.artifact_id
            )
            answer_text = str(
                aggregation_payload.get("output", {}).get(
                    "text",
                    aggregation_payload.get("output", ""),
                )
            )
        result_artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.DONE_RECALL_RESULT.value,
            payload={
                "schema_version": "done_recall_result.v1",
                "directive": directive,
                "query": query,
                "ranked_chunk_ids": [hit.chunk_id for hit in ranked],
                "snippets": [
                    {"chunk_id": hit.chunk_id, "score": hit.score, "text_preview": hit.text[:400]}
                    for hit in ranked
                ],
                "aggregated_answer": answer_text,
                "fallback_active": self.runtime.model_manager.is_default_fallback_active(),
            },
            workflow_id=workflow_id,
            schema_version="done_recall_result.v1",
        )
        artifacts = [prompt_artifact, result_artifact]
        if aggregation_artifact is not None:
            artifacts.insert(1, aggregation_artifact)
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.COMPLETED,
            stage=Stage.COMPLETED,
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.DONE_RECALL,
            WorkflowStatus.COMPLETED,
            Stage.COMPLETED,
            artifacts,
        )

    def propose_workflowy_destination(
        self,
        workflow_id: str,
        workspace_id: str,
        text: str,
        input_artifacts: list[str],
    ) -> Any:
        policy = self.runtime.policy_store.get(workspace_id)
        task = PiTask(
            workflow_id=workflow_id,
            workspace_id=workspace_id,
            task_type="choose_workflowy_destination",
            allowed_tools=policy.allowed_tools,
            forbidden_tools=policy.forbidden_tools,
            input_artifacts=input_artifacts,
            output_schema="workflowy_destination_decision.v1",
            prompt=text,
        )
        return self.runtime.pi.run_decision(task)
