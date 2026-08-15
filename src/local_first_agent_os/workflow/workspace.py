# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""WorkspaceWorkflow methods split from the workflow facade."""

from __future__ import annotations

import json
import logging

from ..capability_gate import SystemWorkflow
from ..constants import DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS
from ..contracts import (
    ArtifactRole,
    EgressStatus,
    IngressEvent,
    MedicalReport,
    ModelCallRequest,
    ModelRole,
    Stage,
    WorkflowResult,
    WorkflowStatus,
    WorkflowType,
    parse_file_uri,
)
from ..ids import sha256_text
from .base import WorkflowMixinBase
from .core import (
    build_completed_workflow_result,
)

logger = logging.getLogger(__name__)


class WorkspaceWorkflowMixin(WorkflowMixinBase):
    def whiteboard_ocr(self, event: IngressEvent) -> WorkflowResult:
        return self._ocr_workflow(
            event=event,
            workflow_type=WorkflowType.WHITEBOARD_OCR,
            source_role=ArtifactRole.SOURCE_IMAGE,
            manual_review_bias=False,
        )

    def paper_notes_ocr(self, event: IngressEvent) -> WorkflowResult:
        return self._ocr_workflow(
            event=event,
            workflow_type=WorkflowType.PAPER_NOTES_OCR,
            source_role=ArtifactRole.SOURCE_FILE,
            manual_review_bias=True,
        )

    def _ocr_workflow(
        self,
        *,
        event: IngressEvent,
        workflow_type: WorkflowType,
        source_role: ArtifactRole,
        manual_review_bias: bool,
    ) -> WorkflowResult:
        workflow_id = self._start(workflow_type, event)
        source_path = parse_file_uri(event.source_uri)
        self.runtime.policy_store.ensure_path_in_workspace(event.workspace_id, source_path)
        source_artifact = self.runtime.artifact_store.import_file(
            role=source_role.value,
            source_path=source_path,
            workflow_id=workflow_id,
            schema_version="source_file.v1",
        )
        self.runtime.repository.update_workflow(workflow_id, stage=Stage.MODEL_LOADING)
        model_result = self.runtime.model_manager.call_model(
            ModelCallRequest(
                workflow_id=workflow_id,
                model_role=ModelRole.OCR,
                input_artifact_id=source_artifact.artifact_id,
                payload={"prompt": "OCR this source artifact into faithful plain text."},
                params={"temperature": 0, "max_tokens": 4096},
                timeout_seconds=DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
            )
        )
        raw = self.runtime.artifact_store.read_json(model_result.output_artifact.artifact_id)
        text = str(raw["output"].get("text", ""))
        ocr_artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.OCR_TEXT.value,
            payload={
                "schema_version": "ocr_text.v1",
                "source_artifact_id": source_artifact.artifact_id,
                "text": text,
                "confidence": raw["output"].get("confidence"),
            },
            workflow_id=workflow_id,
            schema_version="ocr_text.v1",
        )
        normalized = self.runtime.artifact_store.write_text(
            role=ArtifactRole.NORMALIZED_TEXT.value,
            text=text.strip(),
            workflow_id=workflow_id,
            schema_version="normalized_text.v1",
        )
        self.runtime.repository.update_workflow(workflow_id, stage=Stage.EMBEDDING_PENDING)
        self.runtime.retrieval.embed_artifact(normalized, event.workspace_id, workflow_id)
        stage = Stage.MANUAL_REVIEW if manual_review_bias else Stage.COMPLETED
        status = WorkflowStatus.MANUAL_REVIEW if manual_review_bias else WorkflowStatus.COMPLETED
        reason = (
            "paper_notes_ocr defaults to manual review before any egress"
            if manual_review_bias
            else None
        )
        self.runtime.repository.update_workflow(
            workflow_id,
            status=status,
            stage=stage,
            error=reason,
        )
        return build_completed_workflow_result(
            workflow_id,
            workflow_type,
            status,
            stage,
            [source_artifact, model_result.output_artifact, ocr_artifact, normalized],
            manual_review_reason=reason,
        )

    def apple_notes_sync(self, event: IngressEvent) -> WorkflowResult:
        workflow_id = self._start(WorkflowType.APPLE_NOTES_SYNC, event)
        output = self.runtime.tool_registry.run(
            workflow_id=workflow_id,
            workspace_id=event.workspace_id,
            caller=SystemWorkflow(source=event.source_type.value),
            tool_name="apple_notes_fetch",
            payload=event.payload,
        )
        artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.NOTES_SNAPSHOT.value,
            payload=output,
            workflow_id=workflow_id,
            schema_version="apple_notes_snapshot.v1",
        )
        self.runtime.retrieval.embed_artifact(artifact, event.workspace_id, workflow_id)
        self.runtime.repository.update_workflow(
            workflow_id, status=WorkflowStatus.COMPLETED, stage=Stage.COMPLETED
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.APPLE_NOTES_SYNC,
            WorkflowStatus.COMPLETED,
            Stage.COMPLETED,
            [artifact],
        )

    def workflowy_sync(self, event: IngressEvent) -> WorkflowResult:
        workflow_id = self._start(WorkflowType.WORKFLOWY_SYNC, event)
        output = self.runtime.tool_registry.run(
            workflow_id=workflow_id,
            workspace_id=event.workspace_id,
            caller=SystemWorkflow(source=event.source_type.value),
            tool_name="workflowy_fetch_nodes",
            payload=event.payload,
        )
        artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.WORKFLOWY_NODE_SNAPSHOT.value,
            payload=output,
            workflow_id=workflow_id,
            schema_version="workflowy_node_snapshot.v1",
        )
        self.runtime.retrieval.embed_artifact(artifact, event.workspace_id, workflow_id)
        self.runtime.repository.update_workflow(
            workflow_id, status=WorkflowStatus.COMPLETED, stage=Stage.COMPLETED
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.WORKFLOWY_SYNC,
            WorkflowStatus.COMPLETED,
            Stage.COMPLETED,
            [artifact],
        )

    def workflowy_write(self, event: IngressEvent) -> WorkflowResult:
        workflow_id = self._start(WorkflowType.WORKFLOWY_WRITE, event)
        parent_node_id = str(event.payload["parent_node_id"])
        content = str(event.payload["content"])
        self.runtime.policy_store.ensure_workflowy_parent_allowed(
            event.workspace_id,
            parent_node_id,
        )
        content_hash = sha256_text(content)
        egress_id, created = self.runtime.repository.create_or_get_egress(
            workflow_id=workflow_id,
            egress_type="workflowy_insert",
            destination_uri=f"workflowy://node/{parent_node_id}",
            content_sha256=content_hash,
            request_json=event.payload,
        )
        if not created:
            self.runtime.repository.update_workflow(
                workflow_id, status=WorkflowStatus.COMPLETED, stage=Stage.COMPLETED
            )
            return build_completed_workflow_result(
                workflow_id,
                WorkflowType.WORKFLOWY_WRITE,
                WorkflowStatus.COMPLETED,
                Stage.COMPLETED,
                [],
                [egress_id],
            )
        self.runtime.repository.update_workflow(workflow_id, stage=Stage.EGRESS_PENDING)
        response = self.runtime.tool_registry.run(
            workflow_id=workflow_id,
            workspace_id=event.workspace_id,
            caller=SystemWorkflow(source=event.source_type.value),
            tool_name="workflowy_insert_node",
            payload={
                "parent_node_id": parent_node_id,
                "content": content,
                "content_sha256": content_hash,
                "idempotency_key": egress_id,
            },
        )
        self.runtime.repository.update_egress(
            egress_id,
            status=EgressStatus.COMPLETED,
            response_json=response,
        )
        artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.MODEL_OUTPUT.value,
            payload=response,
            workflow_id=workflow_id,
            schema_version="workflowy_insert_response.v1",
        )
        self.runtime.repository.update_workflow(
            workflow_id, status=WorkflowStatus.COMPLETED, stage=Stage.COMPLETED
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.WORKFLOWY_WRITE,
            WorkflowStatus.COMPLETED,
            Stage.COMPLETED,
            [artifact],
            [egress_id],
        )

    def audio_transcription(self, event: IngressEvent) -> WorkflowResult:
        workflow_id = self._start(WorkflowType.AUDIO_TRANSCRIPTION, event)
        source_path = parse_file_uri(event.source_uri)
        if not source_path.exists() or not source_path.is_file():
            artifact = self.runtime.artifact_store.write_json(
                role=ArtifactRole.UNSUPPORTED_STUB.value,
                payload={
                    "schema_version": "transcript.v0_stub",
                    "source_uri": event.source_uri,
                    "reason": f"audio source not found: {source_path}",
                },
                workflow_id=workflow_id,
                schema_version="transcript.v0_stub",
            )
            self.runtime.repository.update_workflow(
                workflow_id,
                status=WorkflowStatus.FAILED_PERMANENT,
                stage=Stage.COMPLETED,
                error=f"audio source not found: {source_path}",
            )
            return build_completed_workflow_result(
                workflow_id,
                WorkflowType.AUDIO_TRANSCRIPTION,
                WorkflowStatus.FAILED_PERMANENT,
                Stage.COMPLETED,
                [artifact],
                manual_review_reason=f"audio source not found: {source_path}",
            )
        source_artifact = self.runtime.artifact_store.import_file(
            role=ArtifactRole.SOURCE_FILE.value,
            source_path=source_path,
            workflow_id=workflow_id,
            schema_version="source_audio.v1",
        )
        self.runtime.repository.update_workflow(workflow_id, stage=Stage.MODEL_LOADING)
        transcription = self.runtime.audio_transcriber.transcribe(source_path)
        if transcription.status == "stub":
            artifact = self.runtime.artifact_store.write_json(
                role=ArtifactRole.UNSUPPORTED_STUB.value,
                payload={
                    "schema_version": "transcript.v0_stub",
                    "source_uri": event.source_uri,
                    "reason": transcription.reason,
                },
                workflow_id=workflow_id,
                schema_version="transcript.v0_stub",
            )
            self.runtime.repository.update_workflow(
                workflow_id,
                status=WorkflowStatus.UNSUPPORTED_STUB,
                stage=Stage.UNSUPPORTED_STUB,
                error="UNSUPPORTED_STUB",
            )
            return build_completed_workflow_result(
                workflow_id,
                WorkflowType.AUDIO_TRANSCRIPTION,
                WorkflowStatus.UNSUPPORTED_STUB,
                Stage.UNSUPPORTED_STUB,
                [source_artifact, artifact],
                manual_review_reason=transcription.reason,
            )
        transcript_text = transcription.text
        transcript_artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.TRANSCRIPT.value,
            payload={
                "schema_version": "transcript.v1",
                "source_artifact_id": source_artifact.artifact_id,
                "text": transcript_text,
                "language": transcription.language,
                "confidence": transcription.confidence,
            },
            workflow_id=workflow_id,
            schema_version="transcript.v1",
        )
        normalized = self.runtime.artifact_store.write_text(
            role=ArtifactRole.NORMALIZED_TEXT.value,
            text=transcript_text.strip(),
            workflow_id=workflow_id,
            schema_version="audio_transcript.v1",
        )
        self.runtime.retrieval.embed_artifact(normalized, event.workspace_id, workflow_id)
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.COMPLETED,
            stage=Stage.COMPLETED,
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.AUDIO_TRANSCRIPTION,
            WorkflowStatus.COMPLETED,
            Stage.COMPLETED,
            [source_artifact, transcript_artifact, normalized],
        )

    def medical_image_analyzer(self, event: IngressEvent) -> WorkflowResult:
        workflow_id = self._start(WorkflowType.MEDICAL_IMAGE_ANALYZER, event)
        source_path = parse_file_uri(event.source_uri)
        source_artifact = self.runtime.artifact_store.import_file(
            role=ArtifactRole.SOURCE_IMAGE.value,
            source_path=source_path,
            workflow_id=workflow_id,
            schema_version="source_image.v1",
        )
        model_result = self.runtime.model_manager.call_model(
            ModelCallRequest(
                workflow_id=workflow_id,
                model_role=ModelRole.MEDICAL,
                input_artifact_id=source_artifact.artifact_id,
                payload={
                    "prompt": (
                        "Describe visible content without diagnosis. Return JSON matching "
                        "med_report.v1 with review_required=true."
                    )
                },
                params={"temperature": 0, "max_tokens": 2048},
                timeout_seconds=DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
            )
        )
        raw = self.runtime.artifact_store.read_json(model_result.output_artifact.artifact_id)
        report = MedicalReport.model_validate(raw["output"])
        report_artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.MED_REPORT.value,
            payload=report.model_dump(),
            workflow_id=workflow_id,
            schema_version=report.schema_version,
        )
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.MANUAL_REVIEW,
            stage=Stage.MANUAL_REVIEW,
            error="medical_image_analyzer is always review-required",
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.MEDICAL_IMAGE_ANALYZER,
            WorkflowStatus.MANUAL_REVIEW,
            Stage.MANUAL_REVIEW,
            [source_artifact, model_result.output_artifact, report_artifact],
            manual_review_reason="Medical output is non-diagnostic and review-required.",
        )

    def training_export_stub(self, event: IngressEvent) -> WorkflowResult:
        workflow_id = self._start(WorkflowType.TRAINING_EXPORT_STUB, event)
        artifacts = self.runtime.repository.list_artifacts_by_role(
            [
                ArtifactRole.OCR_TEXT.value,
                ArtifactRole.NOTES_SNAPSHOT.value,
                ArtifactRole.WORKFLOWY_NODE_SNAPSHOT.value,
                ArtifactRole.ANSWER.value,
            ],
            limit=50_000,
        )
        lines = [
            json.dumps(
                {
                    "schema_version": "training_manifest_item.v0_stub",
                    "artifact_id": artifact.artifact_id,
                    "role": str(artifact.role),
                    "sha256": artifact.sha256,
                    "uri": artifact.uri,
                },
                sort_keys=True,
            )
            for artifact in artifacts
        ]
        manifest = self.runtime.artifact_store.write_text(
            role=ArtifactRole.TRAINING_MANIFEST.value,
            text="\n".join(lines) + ("\n" if lines else ""),
            workflow_id=workflow_id,
            schema_version="training_manifest.v0_stub",
            mime_type="application/jsonl",
        )
        self.runtime.repository.update_workflow(
            workflow_id, status=WorkflowStatus.COMPLETED, stage=Stage.COMPLETED
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.TRAINING_EXPORT_STUB,
            WorkflowStatus.COMPLETED,
            Stage.COMPLETED,
            [manifest],
        )
