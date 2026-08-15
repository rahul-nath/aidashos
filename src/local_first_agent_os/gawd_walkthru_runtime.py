# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The shared model-backed summarizer for Pi and HTTP walkthroughs."""

from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import ArtifactRef, ArtifactRole, ModelCallRequest
from .gawd_walkthru import (
    SummaryProposal,
    WalkthruSection,
    parse_summary_proposal,
    summary_prompt,
)
from .runtime import AppRuntime


@dataclass
class GawdWalkthruSummarizer:
    """Summarize one verbatim answer while retaining the model-call evidence."""

    runtime: AppRuntime
    workflow_id: str
    artifacts: list[ArtifactRef] = field(default_factory=list)

    def __call__(self, section: WalkthruSection, verbatim: str) -> SummaryProposal:
        prompt = summary_prompt(section, verbatim)
        prompt_artifact = self.runtime.artifact_store.write_text(
            role=ArtifactRole.PROMPT.value,
            text=prompt,
            workflow_id=self.workflow_id,
            schema_version="gawd_walkthru_summary_prompt.v1",
        )
        self.artifacts.append(prompt_artifact)
        model_result = self.runtime.model_manager.call_model(
            ModelCallRequest(
                workflow_id=self.workflow_id,
                model_role=self.runtime.model_manager.effective_general_role(),
                input_artifact_id=prompt_artifact.artifact_id,
                payload={"prompt": prompt},
                params={"temperature": 0.1, "max_tokens": 1200},
            )
        )
        self.artifacts.append(model_result.output_artifact)
        output_payload = self.runtime.artifact_store.read_json(
            model_result.output_artifact.artifact_id
        )
        return parse_summary_proposal(
            output_payload.get("output"),
            verbatim=verbatim,
        )


__all__ = ["GawdWalkthruSummarizer"]
