# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One typed application boundary for every operator mutation.

Terminal text and HTTP payloads are transport formats.  Their adapters end by
constructing one of these commands.  Authorization may reject a command before
this boundary, but no adapter owns a second implementation of the mutation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, assert_never

from .coordination import (
    ApprovalDecision,
    ListSagas,
    ResolveApprovalRequest,
)
from .coordination.outcomes import (
    DispatchPromotionState,
    next_dispatch_promotion_states,
    require_dispatch_promotion_transition,
)
from .lifecycle_failure_harness import (
    LifecycleTransitionPoint,
    reach_lifecycle_transition,
)
from .merge_review import pending_code_merge_approval, require_staff_review_provenance
from .operator_identity import OperatorActor, require_verified_operator
from .pow_wow.ledger import run_coordination_command
from .project_center import load_project_center
from .refinery.trigger import (
    IntegrationAccepted,
    IntegrationTriggerResult,
    plan_integration_trigger,
)
from .settings import Settings
from .work_units import service as work_units
from .work_units.root_workflow import EnqueueDelivery


@dataclass(frozen=True)
class ResolveWorkUnitDecision:
    work_unit_id: str
    request_id: str
    decision: str
    idempotency_key: str
    actor: OperatorActor
    payload: Mapping[str, Any] | None = None
    resume_refusal: Callable[[], str | None] | None = None


@dataclass(frozen=True)
class CancelWorkUnit:
    work_unit_id: str
    reason: str
    actor: OperatorActor


@dataclass(frozen=True)
class ResumeWorkUnit:
    work_unit_id: str
    delivery: EnqueueDelivery
    actor: OperatorActor


@dataclass(frozen=True)
class ApproveCodeMerge:
    approval_id: str
    actor: OperatorActor


@dataclass(frozen=True)
class TriggerIntegration:
    approval_id: str
    actor: OperatorActor


OperatorCommand = (
    ResolveWorkUnitDecision
    | CancelWorkUnit
    | ResumeWorkUnit
    | ApproveCodeMerge
    | TriggerIntegration
)


@dataclass(frozen=True)
class WorkUnitDecisionExecuted:
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class WorkUnitCancelled:
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class WorkUnitResumed:
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class CodeMergeApproved:
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class IntegrationTriggered:
    result: IntegrationTriggerResult


OperatorCommandResult = (
    WorkUnitDecisionExecuted
    | WorkUnitCancelled
    | WorkUnitResumed
    | CodeMergeApproved
    | IntegrationTriggered
)


@dataclass(frozen=True)
class OperatorExecutionContext:
    settings: Settings
    submit_integration: Callable[[str], None] | None = None
    plan_integration: Callable[[str], IntegrationTriggerResult] = plan_integration_trigger


def execute_operator_command(
    command: OperatorCommand,
    *,
    context: OperatorExecutionContext,
) -> OperatorCommandResult:
    """Execute the command after all surface-specific parsing has ended."""

    require_verified_operator(command.actor)
    match command:
        case ResolveWorkUnitDecision():
            return WorkUnitDecisionExecuted(
                work_units.submit_work_unit_decision(
                    command.work_unit_id,
                    command.request_id,
                    command.decision,
                    command.idempotency_key,
                    decided_by=command.actor.principal,
                    payload=(dict(command.payload) if command.payload is not None else None),
                    resume_refusal=command.resume_refusal,
                )
            )
        case CancelWorkUnit():
            return WorkUnitCancelled(
                work_units.cancel_work_unit(
                    command.work_unit_id,
                    reason=command.reason,
                )
            )
        case ResumeWorkUnit():
            return WorkUnitResumed(
                work_units.resume_work_unit(
                    command.work_unit_id,
                    delivery=command.delivery,
                )
            )
        case ApproveCodeMerge():
            return CodeMergeApproved(_approve_code_merge(command, settings=context.settings))
        case TriggerIntegration():
            result = context.plan_integration(command.approval_id)
            if isinstance(result, IntegrationAccepted):
                if context.submit_integration is None:
                    raise RuntimeError("TriggerIntegration requires an integration submitter")
                context.submit_integration(result.target_project_id)
            return IntegrationTriggered(result)
    assert_never(command)


def _approve_code_merge(
    command: ApproveCodeMerge,
    *,
    settings: Settings,
) -> dict[str, Any]:
    approval_id = command.approval_id
    approval = pending_code_merge_approval(
        settings=settings,
        approval_id=approval_id,
    )
    require_staff_review_provenance(approval, settings=settings)
    resolution = run_coordination_command(
        ResolveApprovalRequest(
            approval_id=approval_id,
            decision=ApprovalDecision.APPROVE,
            resolved_by=command.actor.principal,
        ),
        timeout=15,
        settings=settings,
    )
    reach_lifecycle_transition(
        LifecycleTransitionPoint.AFTER_MERGE_APPROVAL_RESOLVED,
        approval_id=approval_id,
        decision="APPROVED",
        saga_id=str(approval.get("saga_id") or "") or None,
    )
    require_dispatch_promotion_transition(
        DispatchPromotionState.MERGE_PENDING,
        DispatchPromotionState.MERGE_APPROVED,
    )
    next_required_state = next(
        iter(next_dispatch_promotion_states(DispatchPromotionState.MERGE_APPROVED))
    )
    payload = approval.get("payload")
    merge_payload = payload if isinstance(payload, Mapping) else {}
    target_project_id = str(merge_payload.get("target_project_id") or "").strip()
    branch = str(merge_payload.get("branch") or "").strip()
    base_sha = str(merge_payload.get("base_sha") or "").strip()
    commit_sha = str(merge_payload.get("commit_sha") or "").strip()
    milestone_id = str(merge_payload.get("milestone_id") or "").strip()
    target_path = ""
    if target_project_id:
        try:
            target_path = str(
                load_project_center(settings).project_by_id(target_project_id).expanded_path
            )
        except (FileNotFoundError, KeyError, ValueError):
            target_path = ""
    saga_id = str(approval.get("saga_id") or "").strip()
    saga_listing = run_coordination_command(
        ListSagas(),
        timeout=15,
        settings=settings,
    )
    saga = next(
        (
            row
            for row in saga_listing.get("sagas", [])
            if isinstance(row, Mapping) and row.get("saga_id") == saga_id
        ),
        {},
    )
    gawd_doc_id = str(saga.get("gawd_doc_id") or "").strip()
    next_actions: list[dict[str, Any]] = [
        {
            "state": DispatchPromotionState.MERGED.value,
            "action": "merge_exact_approved_commit",
            "target_project_id": target_project_id or None,
            "target_project_path": target_path or None,
            "branch": branch or None,
            "base_sha": base_sha or None,
            "commit_sha": commit_sha or None,
            "instruction": (
                "Confirm target main is clean and still contains the approved base, "
                "then merge the exact approved commit."
            ),
        }
    ]
    if milestone_id:
        next_actions.extend(
            [
                {
                    "state": DispatchPromotionState.MILESTONE_COMPLETED.value,
                    "action": "complete_milestone_after_merge",
                    "milestone_id": milestone_id,
                    "instruction": (
                        "Record milestone completion with the resulting merge commit as evidence."
                    ),
                },
                {
                    "action": "dispatch_next_ready_milestone",
                    "gawd_doc_id": gawd_doc_id or None,
                    "target_project_id": target_project_id or None,
                    "instruction": (
                        "Run the approved-GAWD path again only after the merge and "
                        "milestone completion are durable."
                    ),
                },
            ]
        )
    integration_request_id = str(resolution.get("integration_request_id") or "").strip()
    report_lines = [
        f"approved CODE_MERGE request {approval_id}; no code was merged by this command.",
        f"Next required transition: {DispatchPromotionState.MERGE_APPROVED.value} -> "
        f"{next_required_state.value}.",
    ]
    if integration_request_id:
        report_lines.append(
            f"Queued for integration as {integration_request_id}. The refinery owns "
            "the integration request from this point."
        )
    if target_project_id and commit_sha:
        location = f" in {target_path}" if target_path else ""
        report_lines.append(
            f"1. Confirm {target_project_id} main is clean and still contains base "
            f"{base_sha or 'recorded in the approval'}, then merge exact commit "
            f"{commit_sha} from {branch or 'the approved branch'}{location}."
        )
    else:
        report_lines.append(
            "1. Inspect the approval payload and merge its exact approved commit into "
            "the target main branch."
        )
    if milestone_id:
        report_lines.append(
            f"2. After the merge, complete milestone {milestone_id} with the merge "
            "commit recorded as evidence."
        )
    else:
        report_lines.append(
            "2. This approval has no milestone_id; do not invent a milestone "
            "transition after integration."
        )
    if milestone_id and gawd_doc_id and target_project_id:
        report_lines.append(
            "3. Dispatch the next dependency-ready milestone with: "
            f"pi /start /approved-gawd {gawd_doc_id} --target-project "
            f"{target_project_id}"
        )
    elif milestone_id:
        report_lines.append(
            "3. Re-run the approved-GAWD path to select the next dependency-ready milestone."
        )
    return {
        "schema_version": "directive_result.v1",
        "action": "approve_merge",
        "status": "approved",
        "approval_id": approval_id,
        "saga_id": saga_id or None,
        "resolution": resolution,
        "promotion_state": DispatchPromotionState.MERGE_APPROVED.value,
        "next_required_state": next_required_state.value,
        "next_actions": next_actions,
        "report": "\n".join(report_lines),
        "code_merged": False,
        "integration_request_id": integration_request_id or None,
    }


__all__ = [
    "ApproveCodeMerge",
    "CancelWorkUnit",
    "CodeMergeApproved",
    "IntegrationTriggered",
    "OperatorActor",
    "OperatorCommand",
    "OperatorCommandResult",
    "OperatorExecutionContext",
    "ResolveWorkUnitDecision",
    "ResumeWorkUnit",
    "TriggerIntegration",
    "WorkUnitCancelled",
    "WorkUnitDecisionExecuted",
    "WorkUnitResumed",
    "execute_operator_command",
]
