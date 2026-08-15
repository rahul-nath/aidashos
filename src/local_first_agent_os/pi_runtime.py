# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from pydantic import ValidationError

from .artifacts import ArtifactStore
from .contracts import ArtifactRole, PiTask, WorkflowyDestinationDecision, WorkspaceId
from .ids import build_pi_turn_id, sha256_text
from .policies import PolicyStore
from .repository import Repository
from .retrieval import RetrievalService


class PiRuntime:
    def __init__(
        self,
        policy_store: PolicyStore,
        repository: Repository,
        artifact_store: ArtifactStore,
        retrieval: RetrievalService,
    ):
        self.policy_store = policy_store
        self.repository = repository
        self.artifact_store = artifact_store
        self.retrieval = retrieval

    def run_decision(self, task: PiTask) -> WorkflowyDestinationDecision:
        policy = self.policy_store.get(task.workspace_id)
        prompt_artifact = self.artifact_store.write_json(
            role=ArtifactRole.PROMPT.value,
            payload=task.model_dump(),
            workflow_id=task.workflow_id,
            schema_version=task.schema_version,
        )
        pi_turn_id = build_pi_turn_id(task.workflow_id, task.task_type, prompt_artifact.sha256)
        try:
            decision = self._deterministic_decision(task, policy.approved_workflowy_parent_ids)
            status = "valid_output"
            if decision.confidence < policy.decision_confidence_threshold:
                decision.requires_manual_review = True
            output = self.artifact_store.write_json(
                role=ArtifactRole.PI_DECISION.value,
                payload=decision.model_dump(),
                workflow_id=task.workflow_id,
                schema_version=decision.schema_version,
            )
            self.repository.record_pi_turn(
                pi_turn_id=pi_turn_id,
                workflow_id=task.workflow_id,
                workspace_id=task.workspace_id,
                prompt_artifact_id=prompt_artifact.artifact_id,
                allowed_tools=task.allowed_tools,
                decision_schema=task.output_schema,
                output_artifact_id=output.artifact_id,
                status=status,
            )
            return decision
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            self.repository.record_pi_turn(
                pi_turn_id=pi_turn_id,
                workflow_id=task.workflow_id,
                workspace_id=task.workspace_id,
                prompt_artifact_id=prompt_artifact.artifact_id,
                allowed_tools=task.allowed_tools,
                decision_schema=task.output_schema,
                output_artifact_id=None,
                status="invalid_schema",
            )
            raise ValueError(f"Pi output invalid for {task.output_schema}: {exc}") from exc

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        """Stream a free-form Pi query through the resident daemon."""
        from .pi_daemon import PiDaemonClient, ensure_pi_daemon

        ensure_pi_daemon()
        client = PiDaemonClient()
        streamed_chunks: list[str] = []
        for event in client.stream_query(
            text=prompt,
            workspace_id=WorkspaceId.GENERAL.value,
            session_id="saga-coordinator",
            context=None,
            max_window_tokens=None,
            streaming=True,
        ):
            event_type = event.get("type")
            if event_type == "delta":
                text = event.get("text")
                if isinstance(text, str):
                    streamed_chunks.append(text)
                    yield text
                continue
            if event_type == "result":
                rendered = event.get("rendered")
                if isinstance(rendered, str):
                    streamed_text = "".join(streamed_chunks)
                    if streamed_text and rendered.strip() == streamed_text.strip():
                        continue
                    yield rendered
                continue
            if event_type == "error":
                error = event.get("error")
                raise RuntimeError(str(error or "Pi daemon query failed"))

    def _deterministic_decision(
        self,
        task: PiTask,
        approved_parent_ids: list[str],
    ) -> WorkflowyDestinationDecision:
        if task.output_schema != "workflowy_destination_decision.v1":
            raise ValueError(f"Unsupported Pi output schema: {task.output_schema}")
        if not approved_parent_ids:
            return WorkflowyDestinationDecision(
                action="manual_review",
                target_reason="No approved Workflowy parent IDs are configured for this workspace.",
                confidence=0.0,
                requires_manual_review=True,
            )
        hits = self.retrieval.search(task.prompt, workspace_id=None, top_k=5)
        confidence = 0.86 if hits else 0.55
        return WorkflowyDestinationDecision(
            action="propose_insert" if confidence >= 0.85 else "manual_review",
            target_node_id=approved_parent_ids[0],
            target_reason=(
                "Deterministic Pi harness selected the first approved parent after retrieval "
                f"evidence hash {sha256_text(task.prompt)[:12]}."
            ),
            confidence=confidence,
            requires_manual_review=confidence < 0.85,
        )
