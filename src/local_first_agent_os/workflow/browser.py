# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""BrowserWorkflow methods split from the workflow facade."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..capability_gate import SystemWorkflow
from ..chrome_devtools import (
    CHROME_CONTROL_RESULT_V1,
    CHROME_CONTROL_RESULT_V2,
    ChromeControlFailure,
)
from ..constants import DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS
from ..contracts import (
    ArtifactRef,
    ArtifactRole,
    IngressEvent,
    ModelCallRequest,
    ModelRole,
    Stage,
    WorkflowResult,
    WorkflowStatus,
    WorkflowType,
    WorkspaceId,
)
from ..directives import DirectiveParser
from ..directives_help import help_payload
from .base import WorkflowMixinBase
from .core import (
    build_completed_workflow_result,
)

logger = logging.getLogger(__name__)


class BrowserWorkflowMixin(WorkflowMixinBase):
    def chrome_control(self, event: IngressEvent) -> WorkflowResult:
        workflow_id = self._start(WorkflowType.CHROME_CONTROL, event)
        parser = DirectiveParser(self.runtime.settings)
        directive = str(event.payload.get("directive", ""))
        artifacts: list[ArtifactRef] = []
        try:
            spec = parser.parse(directive)
            action = spec.chrome_action or "list"
            output = self.runtime.tool_registry.run(
                workflow_id=workflow_id,
                workspace_id=WorkspaceId.CHROME.value,
                caller=SystemWorkflow(source=event.source_type.value),
                tool_name="chrome_devtools",
                payload={
                    "schema_version": "chrome_control_request.v1",
                    "directive": directive,
                    "action": action,
                    "args": list(spec.chrome_args),
                },
            )
            if action in {"read", "summarize", "decide"}:
                ocr_texts, ocr_artifacts = self._ocr_chrome_screenshots(
                    workflow_id=workflow_id,
                    chrome_output=output,
                )
                if ocr_texts:
                    output["ocr_texts"] = ocr_texts
                    artifacts.extend(ocr_artifacts)
                read_artifact = self._write_chrome_tab_text(
                    workflow_id=workflow_id,
                    directive=directive,
                    chrome_output=output,
                )
                output["read_artifact_id"] = read_artifact.artifact_id
                artifacts.append(read_artifact)
            if action in {"summarize", "decide"}:
                summary, summary_artifacts = self._summarize_chrome_tabs(
                    workflow_id=workflow_id,
                    directive=directive,
                    chrome_output=output,
                    action=action,
                )
                output["summary"] = summary
                artifacts.extend(summary_artifacts)
        except ChromeControlFailure as exc:
            help_block = help_payload(parser, directive, str(exc))
            failure = dict(exc.result)
            failure["directive"] = directive
            failure["help"] = help_block
            artifact = self.runtime.artifact_store.write_json(
                role=ArtifactRole.CHROME_CONTROL_RESULT.value,
                payload=failure,
                workflow_id=workflow_id,
                schema_version=CHROME_CONTROL_RESULT_V2,
            )
            self.runtime.repository.update_workflow(
                workflow_id,
                status=WorkflowStatus.FAILED_PERMANENT,
                stage=Stage.COMPLETED,
                error=str(exc),
            )
            return build_completed_workflow_result(
                workflow_id,
                WorkflowType.CHROME_CONTROL,
                WorkflowStatus.FAILED_PERMANENT,
                Stage.COMPLETED,
                [artifact],
                manual_review_reason=help_block.get("summary"),
                help=help_block,
            )
        except Exception as exc:
            help_block = help_payload(parser, directive, str(exc))
            artifact = self.runtime.artifact_store.write_json(
                role=ArtifactRole.CHROME_CONTROL_RESULT.value,
                payload={
                    "schema_version": CHROME_CONTROL_RESULT_V1,
                    "directive": directive,
                    "status": "failed",
                    "error": str(exc),
                    "help": help_block,
                },
                workflow_id=workflow_id,
                schema_version=CHROME_CONTROL_RESULT_V1,
            )
            self.runtime.repository.update_workflow(
                workflow_id,
                status=WorkflowStatus.FAILED_PERMANENT,
                stage=Stage.COMPLETED,
                error=str(exc),
            )
            return build_completed_workflow_result(
                workflow_id,
                WorkflowType.CHROME_CONTROL,
                WorkflowStatus.FAILED_PERMANENT,
                Stage.COMPLETED,
                [artifact],
                manual_review_reason=help_block.get("summary"),
                help=help_block,
            )
        artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.CHROME_CONTROL_RESULT.value,
            payload={
                "schema_version": CHROME_CONTROL_RESULT_V1,
                "directive": directive,
                "status": "completed",
                "chrome": output,
            },
            workflow_id=workflow_id,
            schema_version=CHROME_CONTROL_RESULT_V1,
        )
        artifacts.append(artifact)
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.COMPLETED,
            stage=Stage.COMPLETED,
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.CHROME_CONTROL,
            WorkflowStatus.COMPLETED,
            Stage.COMPLETED,
            artifacts,
        )

    def _summarize_chrome_tabs(
        self,
        *,
        workflow_id: str,
        directive: str,
        chrome_output: dict[str, Any],
        action: str,
    ) -> tuple[dict[str, Any], list[ArtifactRef]]:
        pages = list(chrome_output.get("matched_pages") or [])
        content_blocks = self._chrome_content_blocks(chrome_output)
        schema_version = "chrome_tab_decision.v1" if action == "decide" else "chrome_tab_summary.v1"
        if not pages:
            return {
                "schema_version": schema_version,
                "summary": "No matching Chrome tabs were found.",
                "matched_page_count": 0,
            }, []
        page_lines = [
            f"- [{page.get('page_id')}] {page.get('title') or page.get('label')} "
            f"{page.get('url') or ''}".strip()
            for page in pages
        ]
        decision_prompt = str(chrome_output.get("decision_prompt") or "").strip()
        if action == "decide":
            chrome_decide = self.runtime.pi_prompts.get("chrome_decide")
            instruction = decision_prompt or chrome_decide.defaults["default_instruction"]
            prompt = chrome_decide.render(instruction=instruction)
        else:
            prompt = self.runtime.pi_prompts.get("chrome_summary").text
        prompt += (
            f"Directive: {directive}\n\n"
            "Matched tabs:\n"
            + "\n".join(page_lines)
            + "\n\nCaptured content:\n"
            + ("\n\n".join(content_blocks) if content_blocks else "(no captured page text)")
        )
        prompt_artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.PROMPT.value,
            payload={"schema_version": "prompt.v1", "prompt": prompt},
            workflow_id=workflow_id,
            schema_version="prompt.v1",
        )
        artifacts: list[ArtifactRef] = [prompt_artifact]
        if self.runtime.model_manager.is_default_fallback_active():
            return {
                "schema_version": schema_version,
                "summary": "\n".join(page_lines),
                "matched_page_count": len(pages),
                "fallback_active": True,
            }, artifacts
        model_result = self.runtime.model_manager.call_model(
            ModelCallRequest(
                workflow_id=workflow_id,
                model_role=ModelRole.GENERAL,
                input_artifact_id=prompt_artifact.artifact_id,
                payload={"prompt": prompt},
                params={"temperature": 0.2, "max_tokens": 1200},
                timeout_seconds=DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
            )
        )
        artifacts.append(model_result.output_artifact)
        model_payload = self.runtime.artifact_store.read_json(
            model_result.output_artifact.artifact_id
        )
        summary_text = str(
            model_payload.get("output", {}).get("text", model_payload.get("output", ""))
        )
        text_key = "decision" if action == "decide" else "summary"
        return {
            "schema_version": schema_version,
            text_key: summary_text,
            "matched_page_count": len(pages),
            "snapshot_count": len(chrome_output.get("page_snapshots") or []),
            "ocr_count": len(chrome_output.get("ocr_texts") or []),
            "model_output_artifact_id": model_result.output_artifact.artifact_id,
        }, artifacts

    def _write_chrome_tab_text(
        self,
        *,
        workflow_id: str,
        directive: str,
        chrome_output: dict[str, Any],
    ) -> ArtifactRef:
        blocks = self._chrome_content_blocks(chrome_output)
        text = (
            f"Directive: {directive}\n"
            f"Category: {chrome_output.get('category') or ''}\n"
            f"Matched pages: {chrome_output.get('match_count') or 0}\n\n"
            + ("\n\n".join(blocks) if blocks else "(no captured page text)")
        )
        return self.runtime.artifact_store.write_text(
            role=ArtifactRole.NORMALIZED_TEXT.value,
            text=text,
            workflow_id=workflow_id,
            schema_version="chrome_tab_text.v1",
            mime_type="text/plain",
        )

    def _chrome_content_blocks(self, chrome_output: dict[str, Any]) -> list[str]:
        pages = {
            str(page.get("page_id")): page for page in chrome_output.get("matched_pages") or []
        }
        blocks: list[str] = []
        for item in chrome_output.get("page_snapshots") or []:
            page_id = str(item.get("page_id") or "")
            page = pages.get(page_id, {})
            title = item.get("title") or page.get("title") or item.get("url") or "untitled"
            blocks.append(f"Page {page_id} ({title}):\n{str(item.get('snapshot') or '')[:6000]}")
        for item in chrome_output.get("ocr_texts") or []:
            page_id = str(item.get("page_id") or "")
            page = pages.get(page_id, {})
            title = item.get("title") or page.get("title") or item.get("url") or "untitled"
            blocks.append(f"OCR page {page_id} ({title}):\n{str(item.get('text') or '')[:6000]}")
        return blocks

    def _ocr_chrome_screenshots(
        self,
        *,
        workflow_id: str,
        chrome_output: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[ArtifactRef]]:
        results: list[dict[str, Any]] = []
        artifacts: list[ArtifactRef] = []
        for item in chrome_output.get("page_screenshots") or []:
            path = Path(str(item.get("path") or "")).expanduser()
            if not path.exists():
                continue
            source_artifact = self.runtime.artifact_store.import_file(
                role=ArtifactRole.SOURCE_IMAGE.value,
                source_path=path,
                workflow_id=workflow_id,
                schema_version="chrome_page_screenshot.v1",
            )
            artifacts.append(source_artifact)
            ocr_result = self.runtime.model_manager.call_model(
                ModelCallRequest(
                    workflow_id=workflow_id,
                    model_role=ModelRole.OCR,
                    input_artifact_id=source_artifact.artifact_id,
                    payload={
                        "prompt": (
                            "OCR this browser page screenshot. Preserve visible text faithfully "
                            "and omit commentary."
                        )
                    },
                    params={"temperature": 0, "max_tokens": 4096},
                    timeout_seconds=DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
                )
            )
            artifacts.append(ocr_result.output_artifact)
            payload = self.runtime.artifact_store.read_json(ocr_result.output_artifact.artifact_id)
            text = str(payload.get("output", {}).get("text", "")).strip()
            results.append(
                {
                    "page_id": item.get("page_id"),
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "screenshot_artifact_id": source_artifact.artifact_id,
                    "ocr_artifact_id": ocr_result.output_artifact.artifact_id,
                    "text": text,
                }
            )
        return results, artifacts

    def _fail_directory_embedding(
        self,
        workflow_id: str,
        directive: str,
        error: str,
        parser: DirectiveParser,
    ) -> WorkflowResult:
        help_block = help_payload(parser, directive, error)
        artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.STORE_MANIFEST.value,
            payload={
                "schema_version": "store_manifest.v1",
                "directive": directive,
                "status": "failed",
                "error": error,
                "help": help_block,
            },
            workflow_id=workflow_id,
            schema_version="store_manifest.v1",
        )
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.FAILED_PERMANENT,
            stage=Stage.COMPLETED,
            error=error,
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.DIRECTORY_EMBEDDING,
            WorkflowStatus.FAILED_PERMANENT,
            Stage.COMPLETED,
            [artifact],
            manual_review_reason=help_block.get("summary"),
            help=help_block,
        )
