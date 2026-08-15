# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""ModelWorkflow methods split from the workflow facade."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator
from typing import Any

from ..agent_query import (
    agent_query_request,
    build_agent_query_record,
    resolve_transcript_pointer,
    run_agent_query,
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
)
from ..directives import DirectiveParser
from ..utils import estimate_tokens, format_turns, parse_turns
from .base import WorkflowMixinBase
from .compaction import (
    HIGH_WATER_RATIO,
    KEEP_LAST_N_EXCHANGES,
    QWEN_CONTEXT_WINDOW,
    TARGET_RATIO,
    build_compacted_context,
    build_context_compaction_payload,
    parse_model_json_object,
    prune_memory_to_token_target,
)
from .core import (
    build_completed_workflow_result,
)

logger = logging.getLogger(__name__)


class ModelWorkflowMixin(WorkflowMixinBase):
    def context_compaction(self, event: IngressEvent) -> WorkflowResult:
        workflow_id = self._start(WorkflowType.CONTEXT_COMPACTION, event)
        parser = DirectiveParser(self.runtime.settings)
        result = self._compact_context(
            workflow_id=workflow_id,
            directive=str(event.payload.get("directive", "/compact")),
            context=str(event.payload.get("context") or ""),
            max_window_tokens=int(
                event.payload.get("max_window_tokens") or parser.default_max_window_tokens
            ),
            threshold_ratio=float(
                event.payload.get("threshold_ratio") or parser.compaction_threshold_ratio
            ),
            target_ratio=float(event.payload.get("target_ratio") or parser.compaction_target_ratio),
        )
        artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.CONTEXT_COMPACTION.value,
            payload=result,
            workflow_id=workflow_id,
            schema_version="context_compaction.v2",
        )
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.COMPLETED,
            stage=Stage.COMPLETED,
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.CONTEXT_COMPACTION,
            WorkflowStatus.COMPLETED,
            Stage.COMPLETED,
            [artifact],
        )

    def _call_compactor_json(
        self,
        *,
        workflow_id: str,
        prompt: str,
        prompt_schema_version: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        prompt_artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.PROMPT.value,
            payload={
                "schema_version": prompt_schema_version,
                "prompt": prompt,
            },
            workflow_id=workflow_id,
            schema_version=prompt_schema_version,
        )
        model_result = self.runtime.model_manager.call_model(
            ModelCallRequest(
                workflow_id=workflow_id,
                model_role=ModelRole.COMPACTOR,
                input_artifact_id=prompt_artifact.artifact_id,
                payload={"prompt": prompt},
                params={"temperature": 0, "max_tokens": max_tokens},
                timeout_seconds=DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
            )
        )
        output_payload = self.runtime.artifact_store.read_json(
            model_result.output_artifact.artifact_id
        )
        output = output_payload.get("output")
        if not isinstance(output, dict):
            raise ValueError("Compactor model output artifact is malformed.")
        return parse_model_json_object(output)

    def _compact_context(
        self,
        *,
        workflow_id: str,
        directive: str,
        context: str,
        max_window_tokens: int,
        threshold_ratio: float,
        target_ratio: float,
    ) -> dict[str, Any]:
        max_window_tokens = max_window_tokens or QWEN_CONTEXT_WINDOW
        threshold_ratio = threshold_ratio or HIGH_WATER_RATIO
        target_ratio = target_ratio or TARGET_RATIO
        threshold_tokens = int(max_window_tokens * threshold_ratio)
        target_tokens = int(max_window_tokens * target_ratio)
        compactor = self.runtime.model_registry.resolve_model(ModelRole.COMPACTOR)
        original_token_count = estimate_tokens(context)

        turns = parse_turns(context)
        if turns:
            split_at = max(0, len(turns) - KEEP_LAST_N_EXCHANGES * 2)
            old_turns = turns[:split_at]
            raw_tail_turns = turns[split_at:]
            old_assistant_turns = [(r, c) for r, c in old_turns if r == "assistant"]
            old_user_turns = [(r, c) for r, c in old_turns if r == "user"]
            old_assistant_text = format_turns(old_assistant_turns)
            old_user_text = format_turns(old_user_turns)
            raw_tail = format_turns(raw_tail_turns)
            total_exchanges = len(turns) // 2
            compacted_exchanges = len(old_turns) // 2
        else:
            old_turns = []
            old_assistant_text = context
            old_user_text = ""
            raw_tail = ""
            total_exchanges = 0
            compacted_exchanges = 0

        if original_token_count < threshold_tokens or not old_assistant_text.strip():
            return build_context_compaction_payload(
                directive=directive,
                status="not_needed",
                compactor_model_id=compactor.model_id,
                original_token_count=original_token_count,
                compacted_token_count=0,
                max_window_tokens=max_window_tokens,
                threshold_ratio=threshold_ratio,
                target_ratio=target_ratio,
                raw_tail_token_count=0,
                total_exchanges=total_exchanges,
                compacted_exchanges=0,
                structured_memory={},
                compacted_context="",
            )

        raw_tail_token_count = estimate_tokens(raw_tail)

        compaction_prompt = self.runtime.pi_prompts.get("compaction")
        with self.runtime.model_manager.loaded_session(ModelRole.COMPACTOR):
            merged = self._call_compactor_json(
                workflow_id=workflow_id,
                prompt=compaction_prompt.render(old_turns=old_assistant_text),
                prompt_schema_version=compaction_prompt.version,
                max_tokens=8192,
            )
            compacted_context = build_compacted_context(merged, raw_tail, old_user_text)

        if estimate_tokens(compacted_context) > target_tokens:
            merged = prune_memory_to_token_target(merged, raw_tail, target_tokens, old_user_text)
            compacted_context = build_compacted_context(merged, raw_tail, old_user_text)

        return build_context_compaction_payload(
            directive=directive,
            status="compacted",
            compactor_model_id=compactor.model_id,
            original_token_count=original_token_count,
            compacted_token_count=estimate_tokens(compacted_context),
            max_window_tokens=max_window_tokens,
            threshold_ratio=threshold_ratio,
            target_ratio=target_ratio,
            raw_tail_token_count=raw_tail_token_count,
            total_exchanges=total_exchanges,
            compacted_exchanges=compacted_exchanges,
            structured_memory=merged,
            compacted_context=compacted_context,
        )

    def _start_default_fallback(self) -> dict[str, Any]:
        fallback_role = ModelRole.GENERAL_FALLBACK
        if not self.runtime.repository.list_embedding_chunks(None):
            self.runtime.model_manager.ensure_loaded(fallback_role, allow_autoload=True)
            self.runtime.model_manager.unload(fallback_role)
            raise RuntimeError("Default model failed and no embedding store exists for fallback.")
        self.runtime.model_manager.ensure_loaded(fallback_role, allow_autoload=True)
        reason = (
            "Default model could not load; general fallback/vector-store fallback is active. "
            "Unload another model manually if memory pressure persists."
        )
        self.runtime.model_manager.activate_default_fallback(fallback_role, reason)
        return {
            "fallback_role": fallback_role.value,
            "warning": reason,
        }

    def agent_query(self, event: IngressEvent) -> WorkflowResult:
        """Ask a frontier CLI directly and record the question plus a transcript pointer.

        No worktree, no lease, no dispatch intent: a question is not a code
        change. The answer is returned to the terminal and left in the CLI's own
        transcript rather than copied into the artifact store.
        """

        workflow_id = self._start(WorkflowType.AGENT_QUERY, event)
        parser = DirectiveParser(self.runtime.settings)
        directive = str(event.payload.get("directive", ""))
        try:
            spec = parser.parse(directive)
        except Exception as exc:
            return self._fail_agent_query(workflow_id, directive, str(exc))
        if spec.action != "agent_query" or spec.agent_harness is None or not spec.query:
            return self._fail_agent_query(
                workflow_id,
                directive,
                "/claude and /codex each require a query.",
            )

        request = agent_query_request(
            workflow_id=workflow_id,
            harness=spec.agent_harness,
            alias=spec.alias,
            query=spec.query,
        )
        run = run_agent_query(request)
        transcript = resolve_transcript_pointer({**request, "session_id": run.get("session_id")})
        record = build_agent_query_record({**request, **run, "transcript": transcript})
        artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.AGENT_QUERY_RECORD.value,
            payload=record,
            workflow_id=workflow_id,
            schema_version=record["schema_version"],
        )
        status = WorkflowStatus.COMPLETED if run["succeeded"] else WorkflowStatus.FAILED_PERMANENT
        self.runtime.repository.update_workflow(
            workflow_id,
            status=status,
            stage=Stage.COMPLETED,
            error=run.get("error"),
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.AGENT_QUERY,
            status,
            Stage.COMPLETED,
            [artifact],
            manual_review_reason=run.get("error") if not run["succeeded"] else None,
        )

    def _fail_agent_query(self, workflow_id: str, directive: str, error: str) -> WorkflowResult:
        artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.AGENT_QUERY_RECORD.value,
            payload={
                "schema_version": "agent_query_record.v1",
                "directive": directive,
                "status": "failed",
                "error": error,
            },
            workflow_id=workflow_id,
            schema_version="agent_query_record.v1",
        )
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.FAILED_PERMANENT,
            stage=Stage.COMPLETED,
            error=error,
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.AGENT_QUERY,
            WorkflowStatus.FAILED_PERMANENT,
            Stage.COMPLETED,
            [artifact],
            manual_review_reason=error,
        )

    def general_questions(
        self,
        event: IngressEvent,
        use_retrieval: bool = True,
    ) -> WorkflowResult:
        result: WorkflowResult | None = None
        for item in self.stream_general_questions(event, use_retrieval=use_retrieval):
            if isinstance(item, WorkflowResult):
                result = item
        assert result is not None
        return result

    def stream_general_questions(
        self,
        event: IngressEvent,
        use_retrieval: bool = True,
    ) -> Iterator[str | WorkflowResult]:
        workflow_id = self._start(WorkflowType.GENERAL_QUESTIONS, event)
        prompt = str(event.payload.get("prompt", ""))
        explicit_role = event.payload.get("model_role")
        default_role = self.runtime.model_manager.effective_general_role()
        requested_role = ModelRole(str(explicit_role or default_role.value))
        requested_selector = event.payload.get("model_selector")
        requested_selector = str(requested_selector) if requested_selector else None
        prompt_artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.PROMPT.value,
            payload={
                "schema_version": "prompt.v1",
                "prompt": prompt,
                "model_selector": requested_selector,
                "model_role": requested_role.value,
            },
            workflow_id=workflow_id,
            schema_version="prompt.v1",
        )
        artifacts = [prompt_artifact]
        embedding_degraded = False
        try:
            if requested_role != ModelRole.GENERAL:
                self.runtime.model_manager.require_loaded(requested_role)
            elif self.runtime.model_manager.is_default_fallback_active():
                yield self._answer_via_fallback(
                    workflow_id=workflow_id,
                    prompt=prompt,
                    prompt_artifact=prompt_artifact,
                    artifacts=artifacts,
                )
                return
            use_retrieval = bool(event.payload.get("use_retrieval", use_retrieval)) and (
                explicit_role is None or requested_role == ModelRole.GENERAL
            )
            if use_retrieval:
                try:
                    hits = self.runtime.retrieval.search(prompt, workspace_id=None, top_k=50)
                except Exception as exc:
                    embedding_degraded = True
                    hits = []
                    candidate_artifact = self.runtime.artifact_store.write_json(
                        role=ArtifactRole.CANDIDATE_SET.value,
                        payload={
                            "schema_version": "candidate_set.v1",
                            "status": "degraded",
                            "error": str(exc),
                            "hits": [],
                        },
                        workflow_id=workflow_id,
                        schema_version="candidate_set.v1",
                    )
                    artifacts.append(candidate_artifact)
                else:
                    candidate_artifact = self.runtime.artifact_store.write_json(
                        role=ArtifactRole.CANDIDATE_SET.value,
                        payload={
                            "schema_version": "candidate_set.v1",
                            "hits": [hit.__dict__ for hit in hits[:50]],
                        },
                        workflow_id=workflow_id,
                        schema_version="candidate_set.v1",
                    )
                    artifacts.append(candidate_artifact)
            self.runtime.repository.update_workflow(workflow_id, stage=Stage.MODEL_LOADING)
            model_request = ModelCallRequest(
                workflow_id=workflow_id,
                model_role=requested_role,
                input_artifact_id=prompt_artifact.artifact_id,
                payload={"prompt": prompt},
                params=(
                    {"max_tokens": 2048}
                    if requested_role == ModelRole.GENERAL
                    else {"temperature": 0.2, "max_tokens": 2048}
                ),
                timeout_seconds=DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS,
            )
            session = (
                self.runtime.model_manager.preloaded_session(requested_role)
                if requested_role != ModelRole.GENERAL
                else contextlib.nullcontext()
            )
            with session:
                yield from self.runtime.model_manager.stream_deltas(model_request)
                model_result = self.runtime.model_manager.write_completed_stream_result(
                    model_request
                )
            output_payload = self.runtime.artifact_store.read_json(
                model_result.output_artifact.artifact_id
            )
            answer_artifact = self.runtime.artifact_store.write_json(
                role=ArtifactRole.ANSWER.value,
                payload={
                    "schema_version": "answer.v1",
                    "prompt_artifact_id": prompt_artifact.artifact_id,
                    "model_output_artifact_id": model_result.output_artifact.artifact_id,
                    "model_selector": requested_selector,
                    "model_role": requested_role.value,
                    "answer": output_payload["output"].get("text", output_payload["output"]),
                },
                workflow_id=workflow_id,
                schema_version="answer.v1",
            )
            artifacts.extend([model_result.output_artifact, answer_artifact])
            self.runtime.repository.update_workflow(
                workflow_id,
                status=WorkflowStatus.COMPLETED,
                stage=Stage.COMPLETED,
            )
            yield build_completed_workflow_result(
                workflow_id,
                WorkflowType.GENERAL_QUESTIONS,
                WorkflowStatus.COMPLETED,
                Stage.COMPLETED,
                artifacts,
                embedding_degraded=embedding_degraded,
            )
        except Exception as exc:
            self.runtime.repository.update_workflow(
                workflow_id,
                status=WorkflowStatus.FAILED_PERMANENT,
                stage=Stage.COMPLETED,
                error=str(exc),
            )
            raise

    def _answer_via_fallback(
        self,
        *,
        workflow_id: str,
        prompt: str,
        prompt_artifact: ArtifactRef,
        artifacts: list[ArtifactRef],
    ) -> WorkflowResult:
        hits = self.runtime.retrieval.search(prompt, workspace_id=None, top_k=8)
        ranked = list(hits[:8])
        snippets = [hit.text[:600] for hit in ranked[:5]]
        fallback_role = (
            self.runtime.model_manager.default_role_fallback or ModelRole.GENERAL_FALLBACK
        )
        body = (
            "Default model unavailable. General fallback is using the top "
            f"{len(snippets)} pgvector hits."
            if snippets
            else "Default model unavailable. No matching embeddings were found."
        )
        joined = "\n\n".join(f"- {snippet}" for snippet in snippets) if snippets else "(no hits)"
        answer_artifact = self.runtime.artifact_store.write_json(
            role=ArtifactRole.ANSWER.value,
            payload={
                "schema_version": "answer.v1",
                "prompt_artifact_id": prompt_artifact.artifact_id,
                "answer": f"{body}\n\n{joined}",
                "fallback_role": fallback_role.value,
                "ranked_chunk_ids": [hit.chunk_id for hit in ranked[:5]],
                "fallback_reason": (
                    self.runtime.model_manager.default_fallback_reason
                    or "default_model_unavailable"
                ),
            },
            workflow_id=workflow_id,
            schema_version="answer.v1",
        )
        artifacts.append(answer_artifact)
        self.runtime.repository.update_workflow(
            workflow_id,
            status=WorkflowStatus.COMPLETED,
            stage=Stage.COMPLETED,
        )
        return build_completed_workflow_result(
            workflow_id,
            WorkflowType.GENERAL_QUESTIONS,
            WorkflowStatus.COMPLETED,
            Stage.COMPLETED,
            artifacts,
            manual_review_reason=self.runtime.model_manager.default_fallback_reason,
        )
