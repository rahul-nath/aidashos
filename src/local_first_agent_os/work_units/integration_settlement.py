# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The landed commit settles its milestone.

The promotion chain does not end at ``MERGED``; it ends at
``MILESTONE_COMPLETED``, and until this module nothing performed that: a landed
request was an ``Integrated`` row an operator noticed by hand
(docs/landed_commit_settles_its_milestone_gawd.md). The trigger is the durable
``integration_landed`` ledger event the landing transaction itself writes;
this consumer claims those events under SKIP LOCKED, so there is exactly one
trigger and no second reader racing the first.

The settle core completes the milestone through
``dispatch_adoption.record_integrated_completion``, the same function the
manual adopt verb calls, so the automatic and operator paths cannot drift. The
``MILESTONE_COMPLETED_BEFORE_EXACT_MERGE`` invariant is enforced by re-reading
the ``Integrated`` row for the intent rather than trusting the event payload:
no milestone completes from this path without a row naming its intent.

Settlement reads integration state and never transitions it, never advances a
branch, and never resolves an approval. A live milestone execution is left to
its own workflow, which already adopts settlements through the dispatch wait;
this path exists for the parked shapes, where the epoch has ended and nothing
is listening.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..coordination.execution import claim_next_ledger_event, complete_ledger_event
from ..coordination.integration_queue import (
    INTEGRATION_LANDED_EVENT,
    integrated_request_for_intent,
)
from . import repository as repo
from .dispatch_adoption import (
    WORK_UNIT_DISPATCH_SOURCE,
    record_integrated_completion,
)
from .events import ArtifactKind, RequirableArtifact
from .execution import MilestoneContext, evidence_artifact
from .lifecycle import (
    TERMINAL_WORK_UNIT_STATUSES,
    MilestoneExecutionStatus,
    WorkUnitStatus,
)

logger = logging.getLogger(__name__)

SETTLEMENT_KIND = "integration_landed.v1"

SETTLEMENT_CONSUMER = "integration-settlement"


@dataclass(frozen=True)
class MilestoneSettled:
    """One landed commit completed its milestone, with the evidence recorded."""

    work_unit_id: str
    milestone_key: str
    attempt: int
    integration_request_id: str
    approved_commit_sha: str
    integration_commit_sha: str
    landing: str
    """``fast_forward`` when the integrated tip equals the approved sha,
    ``merge_commit`` when the refinery landed a commit that differs from it.
    Both shas appear in the evidence either way."""

    applied: bool
    resume_enqueued: bool


@dataclass(frozen=True)
class SettlementSkipped:
    """This landing settles nothing here, and that is not an error.

    A request that did not come from a WorkUnit milestone, a milestone whose
    execution is still live inside its own root workflow, or one already
    completed by another path. The event is consumed; the row stays history.
    """

    intent_id: str | None
    reason: str


@dataclass(frozen=True)
class SettlementRefused:
    """This landing named a milestone that must not be completed.

    A stale plan: the WorkUnit was cancelled or superseded after the commit was
    approved. The refusal names why, the event resolves FAILED so the refusal
    is durable, and the ``Integrated`` row remains as history.
    """

    intent_id: str
    code: str
    reason: str


type SettlementOutcome = MilestoneSettled | SettlementSkipped | SettlementRefused


def settle_landed_integration(payload: dict[str, Any]) -> SettlementOutcome:
    """Settle one ``integration_landed`` event's milestone, or say why not."""

    intent_id = str(payload.get("intent_id") or "") or None
    if intent_id is None or not payload.get("milestone_key"):
        return SettlementSkipped(
            intent_id=intent_id,
            reason="the integration request did not come from a milestone",
        )
    integrated = integrated_request_for_intent(intent_id)
    if integrated is None:
        return SettlementRefused(
            intent_id=intent_id,
            code="integrated_row_missing",
            reason=(
                "no Integrated row names this intent; "
                "MILESTONE_COMPLETED_BEFORE_EXACT_MERGE forbids settling without one"
            ),
        )
    with_source = _work_unit_source(intent_id)
    if with_source is None:
        return SettlementSkipped(
            intent_id=intent_id,
            reason="the dispatch source does not name a WorkUnit milestone",
        )
    work_unit_id, milestone_key = with_source
    unit = repo.get_work_unit(work_unit_id)
    if unit.status in TERMINAL_WORK_UNIT_STATUSES:
        return SettlementRefused(
            intent_id=intent_id,
            code="stale_plan",
            reason=(
                f"work unit {work_unit_id} is {unit.status.value}; the landed commit "
                "stays history and settles nothing"
            ),
        )
    milestone = next(
        (
            item
            for item in repo.list_milestone_executions(work_unit_id)
            if item.stable_key == milestone_key
        ),
        None,
    )
    if milestone is None:
        return SettlementRefused(
            intent_id=intent_id,
            code="stale_plan",
            reason=f"work unit {work_unit_id} has no milestone {milestone_key}",
        )
    if milestone.status is MilestoneExecutionStatus.SUCCEEDED:
        return SettlementSkipped(
            intent_id=intent_id,
            reason=f"milestone {milestone_key} is already SUCCEEDED; settling twice writes nothing",
        )
    if milestone.status is MilestoneExecutionStatus.READY:
        return SettlementSkipped(
            intent_id=intent_id,
            reason=(
                f"milestone {milestone_key} is {milestone.status.value}; it has not "
                "started the source-patch attempt the landing claims to settle"
            ),
        )
    plan_revision = repo.get_compiled_plan_revision(unit.compiled_plan_revision_id)
    compiled_milestone = plan_revision.plan.milestone(milestone_key)
    if tuple(compiled_milestone.required_artifacts) != (ArtifactKind.SOURCE_PATCH.value,):
        # Same gate as the manual adopt verb: a landed commit proves exactly one
        # required source_patch. A milestone requiring anything else has
        # evidence this path cannot supply, and `record_fact` would refuse the
        # completion anyway; skipping says so before writing half a triple.
        return SettlementSkipped(
            intent_id=intent_id,
            reason=(
                f"milestone {milestone_key} requires "
                f"{list(compiled_milestone.required_artifacts)}; a landed commit "
                "proves exactly one source_patch"
            ),
        )
    subject = integrated.subject
    landing = (
        "fast_forward"
        if integrated.integration_commit_sha == subject.commit_sha
        else "merge_commit"
    )
    live_execution = milestone.status is MilestoneExecutionStatus.RUNNING
    attempt = milestone.attempt if live_execution else milestone.attempt + 1
    child_workflow_id = (
        str(milestone.child_workflow_id)
        if live_execution and milestone.child_workflow_id
        else f"integration-settlement:{subject.request_id}"
    )
    context = MilestoneContext(
        work_unit_id=work_unit_id,
        root_workflow_id=unit.root_workflow_id,
        child_workflow_id=child_workflow_id,
        milestone=compiled_milestone,
        attempt=attempt,
        design_doc_revision_id=unit.design_doc_revision_id,
        compiled_plan_hash=unit.compiled_plan_hash,
        document_context=plan_revision.plan.document_context,
        target_project_id=plan_revision.plan.target_project_id,
    )
    artifact = evidence_artifact(
        context,
        RequirableArtifact(ArtifactKind.SOURCE_PATCH),
        content=(
            f"integration request: {subject.request_id}\n"
            f"approved commit: {subject.commit_sha}\n"
            f"integration commit: {integrated.integration_commit_sha}\n"
            f"landing: {landing}\n"
        ),
        step_name=child_workflow_id,
        metadata={
            "settlement_kind": SETTLEMENT_KIND,
            "integration_request_id": subject.request_id,
            "approved_commit_sha": subject.commit_sha,
            "integration_commit_sha": integrated.integration_commit_sha,
            "landing": landing,
            "settled_dispatch_intent_id": intent_id,
        },
    )
    shared_payload = {
        "settlement_kind": SETTLEMENT_KIND,
        "integration_request_id": subject.request_id,
        "approved_commit_sha": subject.commit_sha,
        "integration_commit_sha": integrated.integration_commit_sha,
        "landing": landing,
        "settled_dispatch_intent_id": intent_id,
    }
    outcome = record_integrated_completion(
        work_unit_id,
        phase=milestone.phase,
        milestone_key=milestone_key,
        attempt=attempt,
        child_workflow_id=child_workflow_id,
        dispatch_intent_id=intent_id,
        artifact=artifact,
        shared_payload=shared_payload,
        result_summary=(
            f"the landed commit {integrated.integration_commit_sha} settled this "
            f"milestone ({landing})"
        ),
    )
    if live_execution:
        from .root_workflow import notify_integration_settlement

        notify_integration_settlement(child_workflow_id, intent_id)
    resume_enqueued = False
    status_after = repo.get_work_unit(work_unit_id).status
    if (
        status_after not in TERMINAL_WORK_UNIT_STATUSES
        and status_after is not WorkUnitStatus.WAITING_FOR_OPERATOR
        and not live_execution
    ):
        # The milestone is complete, and a unit this path settled had no live
        # milestone execution, so nothing schedules what the completion
        # unblocked. The RESUME row is the same delivery every other unattended
        # wake uses. Re-read after the completion because recording the triple
        # itself moves the unit's projected status, and gating on the stale
        # BLOCKED reading skipped the delivery every time. A WAITING unit keeps
        # its own durable wake; everything else gets the row, and a root that
        # somehow still runs is safe because the continuation ID coalesces.
        resume_enqueued = repo.enqueue_resume(work_unit_id)
    return MilestoneSettled(
        work_unit_id=work_unit_id,
        milestone_key=milestone_key,
        attempt=attempt,
        integration_request_id=subject.request_id,
        approved_commit_sha=subject.commit_sha,
        integration_commit_sha=integrated.integration_commit_sha,
        landing=landing,
        applied=outcome.applied,
        resume_enqueued=resume_enqueued,
    )


def settle_landed_integrations(
    limit: int = 20,
    *,
    claimed_by: str = SETTLEMENT_CONSUMER,
) -> tuple[SettlementOutcome, ...]:
    """Claim and settle pending ``integration_landed`` events, oldest first.

    A settled or skipped event resolves ``PROCESSED``; a refusal resolves
    ``FAILED`` with the reason, because a stale plan does not become fresh by
    retrying. An event whose settle *raised* resolves ``FAILED`` with the
    exception, so one poisoned event cannot stall the queue behind it.
    """

    outcomes: list[SettlementOutcome] = []
    for _ in range(limit):
        claimed = claim_next_ledger_event(claimed_by, event_type=INTEGRATION_LANDED_EVENT)
        event = claimed.get("event")
        if not event:
            break
        event_id = str(event["event_id"])
        payload = dict(event.get("payload") or {})
        try:
            outcome = settle_landed_integration(payload)
        except Exception as exc:  # noqa: BLE001 - one bad event must not stop the drain
            complete_ledger_event(event_id, "FAILED", error=f"{type(exc).__name__}: {exc}")
            logger.error(
                "integration settlement raised for event %s: %s",
                event_id,
                exc,
                exc_info=exc,
            )
            continue
        match outcome:
            case SettlementRefused():
                complete_ledger_event(event_id, "FAILED", error=f"{outcome.code}: {outcome.reason}")
            case MilestoneSettled() | SettlementSkipped():
                complete_ledger_event(event_id, "PROCESSED")
        if isinstance(outcome, MilestoneSettled):
            logger.info(
                "the landed commit %s settled milestone %s of work unit %s (%s)",
                outcome.integration_commit_sha,
                outcome.milestone_key,
                outcome.work_unit_id,
                outcome.landing,
            )
        outcomes.append(outcome)
    return tuple(outcomes)


def _work_unit_source(intent_id: str) -> tuple[str, str] | None:
    from .dispatch_adoption import _dispatch_intent

    intent = _dispatch_intent(intent_id)
    match = WORK_UNIT_DISPATCH_SOURCE.fullmatch(str(intent.get("source") or ""))
    if match is None:
        return None
    return match.group("work_unit_id"), match.group("milestone_key")


__all__ = [
    "SETTLEMENT_CONSUMER",
    "SETTLEMENT_KIND",
    "MilestoneSettled",
    "SettlementOutcome",
    "SettlementRefused",
    "SettlementSkipped",
    "settle_landed_integration",
    "settle_landed_integrations",
]
