# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Adopting legacy sagas into WorkUnits, idempotently and without invention.

The coordination ledger already holds sagas, milestones, dispatch intents,
checkpoints, and evidence. Reconciliation gives that history one owner: a project
saga becomes one WorkUnit whose milestone executions carry the outcomes the legacy
rows already prove, and a milestone-less execution saga stays a dispatch record
rather than being promoted into a project.

Two rules keep this honest. Nothing is fabricated: a legacy milestone is adopted as
``SUCCEEDED`` only when the ledger holds evidence for it, and the adopted artifact
points at that evidence. Nothing is assumed: a phase classification derived from a
milestone's name is recorded as inferred with its confidence, which makes it an
execution blocker until an operator confirms it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..contracts import MilestoneStatus
from ..coordination.store import rowdict, tx
from ..ids import sha256_text
from . import repository as repo
from .design_doc import PhaseInference
from .events import (
    ArtifactRecord,
    LegacySagaReconciled,
    MilestoneTransition,
    WorkUnitTransition,
    parse_artifact_type,
)
from .executors import EXECUTOR_REGISTRY, default_executor_for_phase
from .lifecycle import LifecyclePhase, MilestoneExecutionStatus, WorkUnitStatus, phase_ordinal
from .service import compile_design_doc_revision

CONFIRMED_CONFIDENCE = 1.0


class LegacySagaKind(StrEnum):
    """What a legacy saga actually is, which decides whether it becomes a WorkUnit.

    A saga with milestones represents governed project work. A saga without them is
    the record of one dispatch execution, and promoting it to a project WorkUnit
    would invent a project that never existed.
    """

    PROJECT_SAGA = "PROJECT_SAGA"
    DISPATCH_EXECUTION = "DISPATCH_EXECUTION"


@dataclass(frozen=True)
class LegacyMilestone:
    milestone_id: str
    sequence: int
    name: str
    description: str
    status: MilestoneStatus
    approval_required: bool
    dispatch_intent_id: str | None
    exit_criteria: tuple[str, ...]
    required_artifacts: tuple[str, ...]
    evidence: tuple[tuple[str, str], ...]

    @property
    def stable_key(self) -> str:
        return f"m{self.sequence:02d}"


@dataclass(frozen=True)
class PhaseClassification:
    milestone_key: str
    phase: LifecyclePhase
    confidence: float
    reasoning: str

    def to_inference(self, *, confirmed: bool) -> PhaseInference:
        return PhaseInference(
            milestone_key=self.milestone_key,
            phase=self.phase,
            confidence=CONFIRMED_CONFIDENCE if confirmed else self.confidence,
            reasoning=self.reasoning,
        )


@dataclass(frozen=True)
class ReconciliationPlan:
    """What reconciling one saga would do, or did.

    The same value describes a dry run and an applied run; ``applied`` is the only
    difference. That is deliberate: an operator reviewing a dry run is reading
    exactly the decisions the apply will make.
    """

    saga_id: str
    kind: LegacySagaKind
    applied: bool
    work_unit_id: str | None
    design_doc_revision_id: str | None
    compiled_plan_revision_id: str | None
    classifications: tuple[PhaseClassification, ...]
    adopted_statuses: tuple[tuple[str, MilestoneExecutionStatus], ...]
    dispatch_intent_links: tuple[tuple[str, str], ...]
    blockers: tuple[str, ...]
    derived_phase: LifecyclePhase
    derived_status: WorkUnitStatus

    def to_payload(self) -> dict[str, Any]:
        return {
            "saga_id": self.saga_id,
            "kind": self.kind.value,
            "applied": self.applied,
            "work_unit_id": self.work_unit_id,
            "design_doc_revision_id": self.design_doc_revision_id,
            "compiled_plan_revision_id": self.compiled_plan_revision_id,
            "classifications": [
                {
                    "milestone_key": item.milestone_key,
                    "phase": item.phase.value,
                    "confidence": item.confidence,
                    "reasoning": item.reasoning,
                }
                for item in self.classifications
            ],
            "adopted_statuses": {key: status.value for key, status in self.adopted_statuses},
            "dispatch_intent_links": dict(self.dispatch_intent_links),
            "blockers": list(self.blockers),
            "derived_phase": self.derived_phase.value,
            "derived_status": self.derived_status.value,
        }


# --------------------------------------------------------------------------- #
# Legacy reads
# --------------------------------------------------------------------------- #


def _load_legacy_milestones(saga_id: str) -> tuple[LegacyMilestone, ...]:
    with tx() as c:
        milestone_rows = c.execute(
            "SELECT * FROM saga_milestones WHERE saga_id=? ORDER BY sequence",
            (saga_id,),
        ).fetchall()
        evidence_rows = c.execute(
            "SELECT milestone_id, evidence_type, content FROM milestone_evidence "
            "WHERE saga_id=? ORDER BY created_at",
            (saga_id,),
        ).fetchall()
    evidence: dict[str, list[tuple[str, str]]] = {}
    for row in evidence_rows:
        data = rowdict(row)
        evidence.setdefault(str(data["milestone_id"]), []).append(
            (str(data["evidence_type"]), str(data["content"]))
        )
    milestones: list[LegacyMilestone] = []
    for row in milestone_rows:
        data = rowdict(row)
        milestone_id = str(data["milestone_id"])
        milestones.append(
            LegacyMilestone(
                milestone_id=milestone_id,
                sequence=int(data["sequence"]),
                name=str(data["name"]),
                description=str(data["description"] or ""),
                status=MilestoneStatus(str(data["status"])),
                approval_required=bool(data["approval_required"]),
                dispatch_intent_id=data["dispatch_intent_id"],
                exit_criteria=tuple(_json_list(data["exit_criteria_json"])),
                required_artifacts=tuple(_json_list(data["required_artifacts_json"])),
                evidence=tuple(evidence.get(milestone_id, ())),
            )
        )
    return tuple(milestones)


def _json_list(raw: Any) -> list[str]:
    import json

    if isinstance(raw, list):
        return [str(item) for item in raw]
    if not raw:
        return []
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in loaded] if isinstance(loaded, list) else []


def _legacy_saga_goal(saga_id: str) -> str:
    with tx() as c:
        row = c.execute("SELECT goal FROM sagas WHERE saga_id=?", (saga_id,)).fetchone()
    if row is None:
        raise repo.WorkUnitError(f"unknown legacy saga {saga_id!r}")
    return str(rowdict(row)["goal"])


def _dispatch_intent_status(intent_id: str) -> str | None:
    with tx() as c:
        row = c.execute(
            "SELECT status FROM dispatch_intents WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
    return str(rowdict(row)["status"]) if row is not None else None


def _latest_checkpoint_failure(intent_id: str) -> str | None:
    with tx() as c:
        row = c.execute(
            "SELECT status, reason FROM agent_execution_checkpoints WHERE intent_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (intent_id,),
        ).fetchone()
    if row is None:
        return None
    data = rowdict(row)
    return f"{data['status']}:{data.get('reason') or 'unspecified'}"


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

_PHASE_PROBES: tuple[tuple[re.Pattern[str], LifecyclePhase, float, str], ...] = (
    # Probe order is the classification policy. Verification outranks review
    # because a legacy milestone that runs a suite and then asks for sign-off is
    # verification work with an approval on it, and classifying it as REVIEW would
    # leave its implementation milestones with nothing verifying them.
    (
        re.compile(r"\b(deploy|release|ship|publish)\b", re.IGNORECASE),
        LifecyclePhase.DELIVER,
        0.75,
        "the milestone text names a deployment or release action",
    ),
    (
        re.compile(r"\b(verify|verification|test|tests|suite|acceptance)\b", re.IGNORECASE),
        LifecyclePhase.VERIFY,
        0.8,
        "the milestone text names verification work",
    ),
    (
        re.compile(r"\b(review|approval|approve|sign ?off)\b", re.IGNORECASE),
        LifecyclePhase.REVIEW,
        0.75,
        "the milestone text names a review or approval",
    ),
    (
        re.compile(r"\b(plan|design|decompose|architecture)\b", re.IGNORECASE),
        LifecyclePhase.PLAN,
        0.7,
        "the milestone text names planning or design work",
    ),
    (
        re.compile(r"\b(clarify|question|ambiguity)\b", re.IGNORECASE),
        LifecyclePhase.CLARIFY,
        0.7,
        "the milestone text names clarification work",
    ),
)


def classify_legacy_milestones(
    milestones: tuple[LegacyMilestone, ...],
) -> tuple[PhaseClassification, ...]:
    """Propose one lifecycle phase per legacy milestone.

    Every result is a proposal with a confidence below the confirmation threshold,
    so adoption cannot execute on a guess. The first milestone is proposed as
    ``PLAN`` when nothing else classified as planning, because the fixed lifecycle
    requires implementation to follow a plan and a legacy saga's first milestone is
    where that planning lived.
    """

    classifications: list[PhaseClassification] = []
    for milestone in milestones:
        text = f"{milestone.name} {milestone.description}"
        matched: PhaseClassification | None = None
        for pattern, phase, confidence, reasoning in _PHASE_PROBES:
            if pattern.search(text):
                matched = PhaseClassification(
                    milestone_key=milestone.stable_key,
                    phase=phase,
                    confidence=confidence,
                    reasoning=reasoning,
                )
                break
        if matched is None:
            matched = PhaseClassification(
                milestone_key=milestone.stable_key,
                phase=LifecyclePhase.IMPLEMENT,
                confidence=0.7,
                reasoning=(
                    "the milestone text names neither planning, verification, review, nor delivery"
                ),
            )
        classifications.append(matched)

    if not any(item.phase is LifecyclePhase.PLAN for item in classifications) and classifications:
        first = classifications[0]
        classifications[0] = PhaseClassification(
            milestone_key=first.milestone_key,
            phase=LifecyclePhase.PLAN,
            confidence=0.5,
            reasoning=(
                "no milestone classified as planning, so the first milestone is proposed as the "
                "plan the fixed lifecycle requires implementation to follow"
            ),
        )
    return tuple(classifications)


# --------------------------------------------------------------------------- #
# Synthesized DesignDoc
# --------------------------------------------------------------------------- #


def _legacy_delivery_contract(artifacts_by_key: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
    """The `## Required Artifacts` section an adopted saga owes.

    The compiler refuses a plan that changes the system and declares neither a
    DELIVER milestone nor a document-level contract, which is the right rule and
    the reason this exists: a legacy saga rarely has a milestone that classifies
    as DELIVER, so without this the adoption could not compile at all.

    The contract is the union of what the adopted milestones already require,
    rather than an invented one. Two properties follow, and both matter. It is
    satisfiable: a saga whose milestones all succeeded holds evidence for every
    type in it, so the adoption does not create a WorkUnit that is born unable to
    finish. And it adds no work: reconciliation preserves what the ledger said,
    and a contract naming an artifact no milestone produces would be this
    function deciding the legacy saga should have done more.
    """

    owed = sorted({artifact for artifacts in artifacts_by_key.values() for artifact in artifacts})
    if not owed:
        return ()
    return (
        "## Required Artifacts",
        "",
        *(f"- {artifact}" for artifact in owed),
        "",
    )


def render_legacy_design_doc(
    saga_id: str,
    goal: str,
    milestones: tuple[LegacyMilestone, ...],
    classifications: tuple[PhaseClassification, ...],
) -> str:
    """Render the adopted saga as a DesignDoc, preserving what the ledger said.

    The adoption needs a source of record, and a synthesized document is honest
    about being one: it states that it was generated, names the saga it came from,
    and carries the legacy criteria verbatim so the compiled plan traces to text a
    reader can check.
    """

    phases = {item.milestone_key: item.phase for item in classifications}
    artifacts_by_key = {
        milestone.stable_key: milestone.required_artifacts
        or EXECUTOR_REGISTRY[
            default_executor_for_phase(phases[milestone.stable_key])
        ].required_artifact_types
        for milestone in milestones
    }
    lines = [
        f"# Adopted legacy saga {saga_id}",
        "",
        "This document was generated by WorkUnit reconciliation from coordination",
        "ledger rows. It is the source of record for the adoption, not a rewrite of",
        "the original GAWD doc.",
        "",
        "## Requirements",
        "",
        f"- {goal}",
        "",
        "## Constraints",
        "",
        "- Adopted milestone outcomes come from ledger evidence and are never inferred.",
        "- Phase assignments are proposals until an operator confirms them.",
        "",
        "## Acceptance criteria",
        "",
        "- Every adopted milestone keeps the outcome the ledger already proves.",
        "",
        *_legacy_delivery_contract(artifacts_by_key),
    ]
    previous_key: str | None = None
    for milestone in milestones:
        phase = phases[milestone.stable_key]
        criteria = milestone.exit_criteria or (f"legacy milestone {milestone.name} completed",)
        artifacts = artifacts_by_key[milestone.stable_key]
        lines.extend(
            [
                f"## Milestone {milestone.stable_key}: {milestone.name}",
                "",
                f"Description: {milestone.description or milestone.name}",
                f"Acceptance: {', '.join(criteria)}",
                f"Artifacts: {', '.join(artifacts)}",
            ]
        )
        if previous_key is not None:
            lines.append(f"Depends on: {previous_key}")
        if milestone.approval_required and phase in {
            LifecyclePhase.REVIEW,
            LifecyclePhase.DELIVER,
        }:
            lines.append("Approval: required")
        lines.append("")
        previous_key = milestone.stable_key
    return "\n".join(lines) + "\n"


_ADOPTED_STATUS: dict[MilestoneStatus, MilestoneExecutionStatus] = {
    MilestoneStatus.COMPLETED: MilestoneExecutionStatus.SUCCEEDED,
    MilestoneStatus.IN_PROGRESS: MilestoneExecutionStatus.BLOCKED,
    MilestoneStatus.BLOCKED: MilestoneExecutionStatus.BLOCKED,
    MilestoneStatus.FAILED: MilestoneExecutionStatus.FAILED,
    MilestoneStatus.CANCELED: MilestoneExecutionStatus.CANCELLED,
    MilestoneStatus.PENDING: MilestoneExecutionStatus.PENDING,
}


def _adoption_artifacts(
    milestone: LegacyMilestone,
    required_artifacts: tuple[str, ...],
) -> tuple[ArtifactRecord, ...]:
    """Map preserved ledger evidence onto the artifact types the plan requires.

    The content hash covers the legacy evidence text, and the metadata names the
    evidence rows it came from, so an adopted success is traceable to the same
    bytes the legacy system recorded. A milestone with no evidence gets no
    artifacts, which is why it cannot be adopted as succeeded.
    """

    if not milestone.evidence:
        return ()
    content = "\n\n".join(f"[{evidence_type}] {body}" for evidence_type, body in milestone.evidence)
    return tuple(
        ArtifactRecord(
            artifact_type=parse_artifact_type(artifact_type),
            uri=f"legacy://milestone_evidence/{milestone.milestone_id}",
            content_hash=sha256_text(f"{artifact_type}:{content}"),
            media_type="text/plain",
            size_bytes=len(content.encode("utf-8")),
            producer_step_name="legacy_adoption",
            metadata={
                "legacy_adopted": True,
                "legacy_milestone_id": milestone.milestone_id,
                "evidence_types": sorted({item[0] for item in milestone.evidence}),
            },
        )
        for artifact_type in required_artifacts
    )


def reconcile_saga(
    saga_id: str,
    *,
    dry_run: bool = True,
    confirm_classification: bool = False,
) -> ReconciliationPlan:
    """Adopt one legacy saga, or explain why it cannot be adopted yet.

    Safe to repeat. An already-adopted saga returns its existing WorkUnit, and every
    fact the adoption records carries a deterministic idempotency key, so a second
    apply writes nothing new.
    """

    milestones = _load_legacy_milestones(saga_id)
    if not milestones:
        return ReconciliationPlan(
            saga_id=saga_id,
            kind=LegacySagaKind.DISPATCH_EXECUTION,
            applied=False,
            work_unit_id=None,
            design_doc_revision_id=None,
            compiled_plan_revision_id=None,
            classifications=(),
            adopted_statuses=(),
            dispatch_intent_links=(),
            blockers=(
                "this saga owns no milestones, so it is a dispatch-execution record "
                "rather than a project WorkUnit",
            ),
            derived_phase=LifecyclePhase.CLARIFY,
            derived_status=WorkUnitStatus.DRAFT,
        )

    goal = _legacy_saga_goal(saga_id)
    classifications = classify_legacy_milestones(milestones)
    existing = repo.find_work_unit_by_legacy_saga(saga_id)

    design_doc = render_legacy_design_doc(saga_id, goal, milestones, classifications)
    revision = repo.insert_design_doc_revision(
        design_doc_id=f"legacy_saga:{saga_id}",
        raw_content=design_doc,
        schema_version="parsed_design_doc.v1",
        source_path=None,
        created_by="reconciliation",
    )
    compiled = compile_design_doc_revision(
        revision.design_doc_revision_id,
        phase_inferences=tuple(
            item.to_inference(confirmed=confirm_classification) for item in classifications
        ),
    )

    adopted = tuple(
        (milestone.stable_key, _ADOPTED_STATUS[milestone.status]) for milestone in milestones
    )
    intent_links = tuple(
        (milestone.stable_key, milestone.dispatch_intent_id)
        for milestone in milestones
        if milestone.dispatch_intent_id
    )
    incomplete = [
        (key, status) for key, status in adopted if status is not MilestoneExecutionStatus.SUCCEEDED
    ]
    phases_by_key = {item.milestone_key: item.phase for item in classifications}
    derived_phase = (
        phases_by_key[incomplete[0][0]]
        if incomplete
        else max(phases_by_key.values(), key=phase_ordinal)
    )
    derived_status = (
        WorkUnitStatus.BLOCKED
        if any(status is MilestoneExecutionStatus.BLOCKED for _, status in incomplete)
        else WorkUnitStatus.RUNNING
        if incomplete
        else WorkUnitStatus.SUCCEEDED
    )

    blockers = list(compiled.execution_blockers)
    if compiled.compiled_plan_revision_id is None:
        blockers = [
            f"{item.code}: {item.message}"
            for item in compiled.diagnostics
            if item.severity.value == "ERROR"
        ]
    for milestone in milestones:
        if milestone.status is MilestoneStatus.COMPLETED and not milestone.evidence:
            blockers.append(
                f"legacy milestone {milestone.stable_key} is COMPLETED but the ledger holds no "
                "evidence for it, so it cannot be adopted as succeeded"
            )

    plan = ReconciliationPlan(
        saga_id=saga_id,
        kind=LegacySagaKind.PROJECT_SAGA,
        applied=False,
        work_unit_id=existing.work_unit_id if existing is not None else None,
        design_doc_revision_id=revision.design_doc_revision_id,
        compiled_plan_revision_id=compiled.compiled_plan_revision_id,
        classifications=classifications,
        adopted_statuses=adopted,
        dispatch_intent_links=intent_links,
        blockers=tuple(blockers),
        derived_phase=derived_phase,
        derived_status=derived_status,
    )
    if dry_run or blockers or compiled.compiled_plan_revision_id is None:
        return plan
    return _apply_reconciliation(plan, milestones)


def _apply_reconciliation(
    plan: ReconciliationPlan,
    milestones: tuple[LegacyMilestone, ...],
) -> ReconciliationPlan:
    assert plan.compiled_plan_revision_id is not None
    existing = repo.find_work_unit_by_legacy_saga(plan.saga_id)
    if existing is None:
        start = repo.start_work_unit(
            plan.compiled_plan_revision_id,
            title=f"adopted legacy saga {plan.saga_id}",
            legacy_saga_id=plan.saga_id,
        )
        work_unit_id = start.work_unit.work_unit_id
    else:
        work_unit_id = existing.work_unit_id

    revision = repo.get_compiled_plan_revision(plan.compiled_plan_revision_id)
    repo.record_fact(
        work_unit_id,
        LegacySagaReconciled(
            saga_id=plan.saga_id,
            milestone_count=len(milestones),
            derived_phase=plan.derived_phase,
            payload={"derived_status": plan.derived_status.value},
        ),
    )
    repo.record_fact(
        work_unit_id,
        WorkUnitTransition(
            status=WorkUnitStatus.RUNNING,
            current_phase=plan.derived_phase,
            reason=f"adopted legacy saga {plan.saga_id}",
        ),
    )

    by_key = {milestone.stable_key: milestone for milestone in milestones}
    for key, status in plan.adopted_statuses:
        if status is MilestoneExecutionStatus.PENDING:
            continue
        milestone = by_key[key]
        compiled = revision.plan.milestone(key)
        artifacts = _adoption_artifacts(milestone, compiled.required_artifacts)
        failure_summary: str | None = None
        failure_code: str | None = None
        if status in {MilestoneExecutionStatus.BLOCKED, MilestoneExecutionStatus.FAILED}:
            intent_status = (
                _dispatch_intent_status(milestone.dispatch_intent_id)
                if milestone.dispatch_intent_id
                else None
            )
            checkpoint = (
                _latest_checkpoint_failure(milestone.dispatch_intent_id)
                if milestone.dispatch_intent_id
                else None
            )
            failure_code = "legacy_adoption_incomplete"
            failure_summary = (
                f"adopted from legacy status {milestone.status.value}"
                + (f"; dispatch intent {intent_status}" if intent_status else "")
                + (f"; latest checkpoint {checkpoint}" if checkpoint else "")
            )
        # The adopted milestone walks the same edges live work does. Reaching a
        # terminal state through READY and RUNNING is what keeps the state machine
        # the single description of how a milestone can move.
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=compiled.phase,
                milestone_key=key,
                status=MilestoneExecutionStatus.READY,
                attempt=1,
            ),
        )
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=compiled.phase,
                milestone_key=key,
                status=MilestoneExecutionStatus.RUNNING,
                attempt=1,
                dispatch_intent_id=milestone.dispatch_intent_id,
            ),
        )
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=compiled.phase,
                milestone_key=key,
                status=status,
                attempt=1,
                dispatch_intent_id=milestone.dispatch_intent_id,
                result_summary=(
                    f"adopted from legacy milestone {milestone.milestone_id}"
                    if status is MilestoneExecutionStatus.SUCCEEDED
                    else None
                ),
                failure_code=failure_code,
                failure_summary=failure_summary,
                artifacts=artifacts,
            ),
        )

    # The adopted milestone states decide the WorkUnit's status, the same way a
    # live run's do. Leaving it RUNNING with a blocked milestone underneath would
    # be the split-authority bug this whole model exists to remove.
    repo.record_fact(
        work_unit_id,
        WorkUnitTransition(
            status=plan.derived_status,
            current_phase=plan.derived_phase,
            failure_code=(
                "legacy_adoption_incomplete"
                if plan.derived_status is WorkUnitStatus.BLOCKED
                else None
            ),
            failure_summary=(
                f"adopted legacy saga {plan.saga_id} with incomplete milestones"
                if plan.derived_status is WorkUnitStatus.BLOCKED
                else None
            ),
        ),
    )

    return ReconciliationPlan(
        saga_id=plan.saga_id,
        kind=plan.kind,
        applied=True,
        work_unit_id=work_unit_id,
        design_doc_revision_id=plan.design_doc_revision_id,
        compiled_plan_revision_id=plan.compiled_plan_revision_id,
        classifications=plan.classifications,
        adopted_statuses=plan.adopted_statuses,
        dispatch_intent_links=plan.dispatch_intent_links,
        blockers=(),
        derived_phase=plan.derived_phase,
        derived_status=plan.derived_status,
    )


def list_legacy_sagas() -> tuple[tuple[str, LegacySagaKind], ...]:
    """Every legacy saga with the kind reconciliation would treat it as."""

    with tx() as c:
        rows = c.execute(
            """
            SELECT s.saga_id AS saga_id, COUNT(m.milestone_id) AS milestone_count
            FROM sagas s
            LEFT JOIN saga_milestones m ON m.saga_id = s.saga_id
            GROUP BY s.saga_id
            ORDER BY s.created_at
            """
        ).fetchall()
    return tuple(
        (
            str(rowdict(row)["saga_id"]),
            LegacySagaKind.PROJECT_SAGA
            if int(rowdict(row)["milestone_count"]) > 0
            else LegacySagaKind.DISPATCH_EXECUTION,
        )
        for row in rows
    )


def reconcile_all(
    *,
    dry_run: bool = True,
    confirm_classification: bool = False,
) -> tuple[ReconciliationPlan, ...]:
    return tuple(
        reconcile_saga(
            saga_id,
            dry_run=dry_run,
            confirm_classification=confirm_classification,
        )
        for saga_id, kind in list_legacy_sagas()
        if kind is LegacySagaKind.PROJECT_SAGA
    )


__all__ = [
    "CONFIRMED_CONFIDENCE",
    "LegacyMilestone",
    "LegacySagaKind",
    "PhaseClassification",
    "ReconciliationPlan",
    "classify_legacy_milestones",
    "list_legacy_sagas",
    "reconcile_all",
    "reconcile_saga",
    "render_legacy_design_doc",
]
