# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Typed HTTP contracts for walkthrough, compile, and start authoring."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .compiler import ValidationStatus
from .plan import PermissionPolicy


class AuthoringContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_serialization_defaults_required=True,
    )


class WalkthruSectionView(AuthoringContract):
    section_id: str
    heading: str | None
    label: str
    question: str
    guidance: str


class WalkthruProposalView(AuthoringContract):
    section_id: str
    verbatim: str
    summary: str
    suggestions: tuple[str, ...] = ()
    summary_method: str


class WalkthruResponseView(AuthoringContract):
    section_id: str
    heading: str | None
    question: str
    verbatim: str | None
    model_summary: str | None
    model_suggestions: tuple[str, ...] = ()
    summary_method: str | None
    accepted_summary: str | None
    status: str


class WalkthruBaseView(AuthoringContract):
    model_config = ConfigDict(
        extra="ignore",
        json_schema_serialization_defaults_required=True,
    )

    schema_version: Literal["gawd_walkthru.v1"]
    status: str
    walkthru_id: str
    draft_id: str
    draft_path: str
    target_project_id: str | None
    create_target_id: str | None
    completed_sections: int
    total_sections: int
    execution_started: Literal[False] = False


class WalkthruAwaitingAnswerView(WalkthruBaseView):
    state: Literal["awaiting_answer"]
    section: WalkthruSectionView


class WalkthruAwaitingSummaryView(WalkthruBaseView):
    state: Literal["awaiting_summary"]
    pending_answer: dict[str, str]


class WalkthruAwaitingReviewView(WalkthruBaseView):
    state: Literal["awaiting_review"]
    proposal: WalkthruProposalView


class WalkthruReadyToFinishView(WalkthruBaseView):
    state: Literal["ready_to_finish"]
    review: tuple[WalkthruResponseView, ...]


class WalkthruFinishedView(WalkthruBaseView):
    state: Literal["finished"]
    responses: tuple[WalkthruResponseView, ...]
    draft_content: str


WalkthruView = Annotated[
    WalkthruAwaitingAnswerView
    | WalkthruAwaitingSummaryView
    | WalkthruAwaitingReviewView
    | WalkthruReadyToFinishView
    | WalkthruFinishedView,
    Field(discriminator="state"),
]


class StartWalkthruRequest(AuthoringContract):
    target_project_id: str | None = None
    create_target_id: str | None = None
    operation_id: str = Field(min_length=1)


class AnswerWalkthru(AuthoringContract):
    action: Literal["answer"]
    operation_id: str = Field(min_length=1)
    verbatim: str = Field(min_length=1)


class AcceptWalkthru(AuthoringContract):
    action: Literal["accept"]
    operation_id: str = Field(min_length=1)


class ReviseWalkthru(AuthoringContract):
    action: Literal["revise"]
    operation_id: str = Field(min_length=1)
    accepted_summary: str = Field(min_length=1)


class SkipWalkthru(AuthoringContract):
    action: Literal["skip"]
    operation_id: str = Field(min_length=1)


class EditWalkthru(AuthoringContract):
    action: Literal["edit"]
    operation_id: str = Field(min_length=1)
    section_id: str = Field(min_length=1)
    accepted_summary: str = Field(min_length=1)


class FinishWalkthru(AuthoringContract):
    action: Literal["finish"]
    operation_id: str = Field(min_length=1)


WalkthruTransition = Annotated[
    AnswerWalkthru | AcceptWalkthru | ReviseWalkthru | SkipWalkthru | EditWalkthru | FinishWalkthru,
    Field(discriminator="action"),
]


class CompileDesignDocRequest(AuthoringContract):
    design_doc_id: str = Field(min_length=1)
    raw_content: str = Field(min_length=1)
    source_path: str | None = None


class SourceSpanView(AuthoringContract):
    start: int
    end: int


class DiagnosticView(AuthoringContract):
    severity: str
    code: str
    message: str
    span: SourceSpanView | None


class PermissionPolicyView(AuthoringContract):
    autonomous_capabilities: tuple[str, ...]
    approval_required_capabilities: tuple[str, ...]
    denied_capabilities: tuple[str, ...]
    capability_ceiling: tuple[str, ...]
    requires_start_approval: bool

    @classmethod
    def from_policy(cls, policy: PermissionPolicy) -> PermissionPolicyView:
        return cls(
            autonomous_capabilities=policy.autonomous_capabilities,
            approval_required_capabilities=policy.approval_required_capabilities,
            denied_capabilities=policy.denied_capabilities,
            capability_ceiling=policy.capability_ceiling,
            requires_start_approval=policy.requires_start_approval,
        )


class DeliveryContractView(AuthoringContract):
    kind: str
    artifact_types: tuple[str, ...] = ()
    reason: str | None = None


class CompileDesignDocResponse(AuthoringContract):
    design_doc_revision_id: str
    compiled_plan_revision_id: str | None
    plan_hash: str | None
    validation_status: ValidationStatus
    diagnostics: tuple[DiagnosticView, ...]
    execution_blockers: tuple[str, ...]
    runnable: bool
    permission_policy: PermissionPolicyView | None
    delivery_contract: DeliveryContractView | None


class StartWorkUnitRequest(AuthoringContract):
    compiled_plan_revision_id: str = Field(min_length=1)
    approved_plan_hash: str | None = None
    title: str | None = None


class StartWorkUnitResponse(AuthoringContract):
    work_unit_id: str
    root_workflow_id: str
    status: str
    created: bool
    dispatch: tuple[dict[str, Any], ...] = ()
    dispatch_failed: tuple[dict[str, Any], ...] = ()


__all__ = [
    "CompileDesignDocRequest",
    "CompileDesignDocResponse",
    "DeliveryContractView",
    "PermissionPolicyView",
    "StartWalkthruRequest",
    "StartWorkUnitRequest",
    "StartWorkUnitResponse",
    "WalkthruTransition",
    "WalkthruView",
]
