# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Whiteboard snapshot ingestion and the create-tomorrow named workflow.

``whiteboard_intent`` turns one board snapshot into durable evidence: a typed
intent graph, corpus match evidence, and a diff against the previous snapshot
with disappearance evidence. It never labels the corpus.

``create_tomorrow`` is user-invoked with a specific instruction. It interprets
the current evidence in the context of that instruction and emits a
``DailyViewPatch`` for one dated top-level Workflowy node, terminating in
MANUAL_REVIEW. Writeback remains the operator-approved ``workflowy_write``
path.
"""

from __future__ import annotations

import logging

from ..constants import DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS
from ..contracts import (
    ArtifactRole,
    IngressEvent,
    ModelCallRequest,
    ModelRole,
    Stage,
    WhiteboardCorpusEvidence,
    WhiteboardIntentGraph,
    WorkflowResult,
    WorkflowStatus,
    WorkflowType,
    parse_file_uri,
)
from ..create_tomorrow import (
    build_daily_view_patch,
    build_interpretation_prompt,
    default_target_top_level,
    load_regimen,
    select_primary_candidates,
)
from ..whiteboard_intent import (
    WHITEBOARD_EXTRACTION_PROMPT,
    build_corpus_evidence,
    diff_graphs,
    parse_extraction_output,
)
from .base import WorkflowMixinBase
from .core import build_completed_workflow_result

logger = logging.getLogger(__name__)

CREATE_TOMORROW_REVIEW_REASON = (
    "daily view patch requires operator approval before any Workflowy write"
)


class WhiteboardIntentWorkflowMixin(WorkflowMixinBase):
    def whiteboard_intent(self, event: IngressEvent) -> WorkflowResult:
        workflow_id = self._start(WorkflowType.WHITEBOARD_INTENT, event)
        source_path = parse_file_uri(event.source_uri)
        self.runtime.policy_store.ensure_path_in_workspace(event.workspace_id, source_path)
        source_artifact = self.runtime.artifact_store.import_file(
            role=ArtifactRole.SOURCE_IMAGE.value,
            source_path=source_path,
            workflow_id=workflow_id,
            schema_version="source_file.v1",
        )
        # The previous snapshot must be resolved before this run's graph is
        # persisted, or the diff would compare the new graph to itself.
        previous_ref = self.runtime.repository.latest_artifact_by_role(
            ArtifactRole.WHITEBOARD_INTENT_GRAPH.value
        )
        self.runtime.repository.update_workflow(workflow_id, stage=Stage.MODEL_LOADING)
        model_result = self.runtime.model_manager.call_model(
            ModelCallRequest(
                workflow_id=workflow_id,
                model_role=ModelRole.OCR,
                input_artifact_id=source_artifact.artifact_id,
                payload={"prompt": WHITEBOARD_EXTRACTION_PROMPT},
                params={"temperature": 0, "max_tokens": 4096},
                timeout_seconds=DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
            )
        )
        raw = self.runtime.artifact_store.read_json(model_result.output_artifact.artifact_id)
        raw_text = str(raw["output"].get("text", ""))
        graph = parse_extraction_output(raw_text, source_artifact.artifact_id)
        graph_artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.WHITEBOARD_INTENT_GRAPH.value,
            payload=graph.model_dump(mode="json"),
            workflow_id=workflow_id,
            schema_version=graph.schema_version,
        )
        flattened = "\n".join(item.text for _, _, item in graph.flattened_items()).strip()
        artifacts = [source_artifact, model_result.output_artifact, graph_artifact]
        if flattened:
            normalized = self.runtime.artifact_store.write_text(
                role=ArtifactRole.NORMALIZED_TEXT.value,
                text=flattened,
                workflow_id=workflow_id,
                schema_version="normalized_text.v1",
            )
            artifacts.append(normalized)
            self.runtime.repository.update_workflow(workflow_id, stage=Stage.EMBEDDING_PENDING)
            self.runtime.retrieval.embed_artifact(normalized, event.workspace_id, workflow_id)
        evidence = build_corpus_evidence(
            graph,
            graph_artifact.artifact_id,
            searcher=self.runtime.retrieval.fetch_workflowy,
        )
        evidence_artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.WHITEBOARD_CORPUS_EVIDENCE.value,
            payload=evidence.model_dump(mode="json"),
            workflow_id=workflow_id,
            schema_version=evidence.schema_version,
        )
        artifacts.append(evidence_artifact)
        if previous_ref is not None:
            previous_graph = WhiteboardIntentGraph.model_validate(
                self.runtime.artifact_store.read_json(previous_ref.artifact_id)
            )
            regimen = load_regimen(self.runtime.settings)
            diff = diff_graphs(
                previous_graph,
                previous_ref.artifact_id,
                graph,
                graph_artifact.artifact_id,
                searcher=self.runtime.retrieval.fetch_workflowy,
                done_top_level=regimen.done_top_level,
            )
            diff_artifact = self.runtime.artifact_store.write_json(
                role=ArtifactRole.WHITEBOARD_DIFF.value,
                payload=diff.model_dump(mode="json"),
                workflow_id=workflow_id,
                schema_version=diff.schema_version,
            )
            artifacts.append(diff_artifact)
        self.runtime.repository.update_workflow(
            workflow_id, status=WorkflowStatus.COMPLETED, stage=Stage.COMPLETED
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.WHITEBOARD_INTENT,
            WorkflowStatus.COMPLETED,
            Stage.COMPLETED,
            artifacts,
        )

    def create_tomorrow(self, event: IngressEvent) -> WorkflowResult:
        workflow_id = self._start(WorkflowType.CREATE_TOMORROW, event)
        instruction = str(event.payload.get("instruction", "")).strip()
        if not instruction:
            raise ValueError(
                "create_tomorrow requires a non-empty 'instruction' payload; "
                "this workflow interprets a specific request, it does not plan "
                "autonomously"
            )
        regimen = load_regimen(self.runtime.settings)
        target_top_level = str(event.payload.get("target_top_level") or default_target_top_level())
        evidence_ref = self.runtime.repository.latest_artifact_by_role(
            ArtifactRole.WHITEBOARD_CORPUS_EVIDENCE.value
        )
        evidence: WhiteboardCorpusEvidence | None = None
        if evidence_ref is not None:
            evidence = WhiteboardCorpusEvidence.model_validate(
                self.runtime.artifact_store.read_json(evidence_ref.artifact_id)
            )
        diff_ref = self.runtime.repository.latest_artifact_by_role(
            ArtifactRole.WHITEBOARD_DIFF.value
        )
        candidates = select_primary_candidates(evidence, regimen)
        prompt = build_interpretation_prompt(instruction, candidates, regimen)
        prompt_artifact = self.runtime.artifact_store.write_text(
            role=ArtifactRole.PROMPT.value,
            text=prompt,
            workflow_id=workflow_id,
            schema_version="prompt.v1",
        )
        self.runtime.repository.update_workflow(workflow_id, stage=Stage.MODEL_LOADING)
        model_result = self.runtime.model_manager.call_model(
            ModelCallRequest(
                workflow_id=workflow_id,
                model_role=ModelRole.GENERAL,
                input_artifact_id=prompt_artifact.artifact_id,
                payload={"prompt": prompt},
                params={"temperature": 0, "max_tokens": 2048},
                timeout_seconds=DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
            )
        )
        raw = self.runtime.artifact_store.read_json(model_result.output_artifact.artifact_id)
        model_text = str(raw["output"].get("text", ""))
        patch = build_daily_view_patch(
            instruction=instruction,
            model_output_text=model_text,
            candidates=candidates,
            regimen=regimen,
            target_top_level=target_top_level,
            evidence_artifact_id=(evidence_ref.artifact_id if evidence_ref is not None else None),
            diff_artifact_id=diff_ref.artifact_id if diff_ref is not None else None,
        )
        patch_artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.DAILY_VIEW_PATCH.value,
            payload=patch.model_dump(mode="json"),
            workflow_id=workflow_id,
            schema_version=patch.schema_version,
        )
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.MANUAL_REVIEW,
            stage=Stage.MANUAL_REVIEW,
            error=CREATE_TOMORROW_REVIEW_REASON,
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.CREATE_TOMORROW,
            WorkflowStatus.MANUAL_REVIEW,
            Stage.MANUAL_REVIEW,
            [prompt_artifact, model_result.output_artifact, patch_artifact],
            manual_review_reason=CREATE_TOMORROW_REVIEW_REASON,
        )
