# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Durable WorkUnit persistence and the one canonical transition operation.

Two rules shape this module.

First, immutability: design doc revisions, compiled plan revisions, compiled
milestones, dependency edges, and events are inserted and never updated. The only
sanctioned write to an existing revision row is stamping
``compiled_plan_revisions.work_unit_id`` when the WorkUnit that adopts the plan is
created, which is a link and not a change of content.

Second, single authority: every status change goes through ``record_fact``. It
locks the WorkUnit, validates the transition against the lifecycle state machines,
appends exactly one event, updates the summary and the affected milestone
execution, and inserts artifact references, all in one transaction. A replayed
fact returns the original event instead of writing a second one. No dispatcher,
CLI command, MCP tool, or agent writes these columns directly.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from ..coordination.store import ConnectionLike, iso, now, rowdict, tx
from ..ids import sha256_text
from .compiler import CompiledPlanOutcome, ValidationStatus
from .events import (
    ApprovalReceived,
    ApprovalRequested,
    ArtifactRecord,
    ArtifactRecorded,
    ArtifactType,
    AutomaticCrashRecovery,
    DecisionRequestKind,
    DecisionRequestStatus,
    DispatchIntentCreated,
    LegacySagaReconciled,
    LifecycleFact,
    MilestoneTransition,
    OperatorDecision,
    PhaseTransition,
    RequirableArtifact,
    WorkUnitEventType,
    WorkUnitTransition,
    idempotency_key,
    parse_artifact_type,
)
from .lifecycle import (
    LIFECYCLE_PROFILE,
    LIFECYCLE_PROFILE_VERSION,
    ORDERED_PHASES,
    FailureClass,
    LifecyclePhase,
    MilestoneExecutionStatus,
    PhaseStatus,
    WorkUnitStatus,
    assert_milestone_transition,
    assert_phase_transition,
    assert_work_unit_transition,
    phase_ordinal,
)
from .plan import CompiledMilestone, CompiledWorkPlan

ROOT_WORKFLOW_ID_PREFIX = "work-unit:"


def root_workflow_id_for(work_unit_id: str) -> str:
    """One WorkUnit, one root workflow ID, derived rather than stored twice."""

    return f"{ROOT_WORKFLOW_ID_PREFIX}{work_unit_id}"


def work_unit_id_for_plan(compiled_plan_revision_id: str) -> str:
    """Derive the WorkUnit identity from the plan it executes.

    Deriving rather than generating is what makes a repeated start request
    idempotent without a separate request-deduplication table: the second call
    computes the same identity, finds the row, and returns it.
    """

    return sha256_text(f"work_unit:{compiled_plan_revision_id}")[:32]


class WorkUnitError(RuntimeError):
    """A WorkUnit operation the domain refuses."""


class UnknownWorkUnit(WorkUnitError):
    def __init__(self, work_unit_id: str) -> None:
        super().__init__(f"unknown work unit {work_unit_id!r}")


class MissingRequiredArtifacts(WorkUnitError):
    """A milestone tried to succeed without the evidence its plan requires."""

    def __init__(self, milestone_key: str, missing: Sequence[str]) -> None:
        super().__init__(
            f"milestone {milestone_key!r} cannot succeed without artifacts: {', '.join(missing)}"
        )
        self.milestone_key = milestone_key
        self.missing = tuple(missing)


class DecisionRequestMismatch(WorkUnitError):
    """A decision was submitted against a request that cannot accept it."""


# --------------------------------------------------------------------------- #
# Row types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DesignDocRevisionRow:
    design_doc_revision_id: str
    design_doc_id: str
    revision_number: int
    content_hash: str
    raw_content: str
    source_path: str | None
    schema_version: str
    created_at: float
    created_by: str


@dataclass(frozen=True)
class CompiledPlanRevisionRow:
    compiled_plan_revision_id: str
    work_unit_id: str | None
    design_doc_revision_id: str
    compiler_version: str
    lifecycle_profile: str
    lifecycle_profile_version: int
    plan: CompiledWorkPlan
    plan_hash: str
    validation_status: ValidationStatus
    validation_errors: tuple[dict[str, Any], ...]
    execution_blockers: tuple[str, ...]
    created_at: float


@dataclass(frozen=True)
class WorkUnitRow:
    work_unit_id: str
    title: str
    status: WorkUnitStatus
    current_phase: str
    design_doc_revision_id: str
    compiled_plan_revision_id: str
    compiled_plan_hash: str
    lifecycle_profile: str
    lifecycle_profile_version: int
    root_workflow_id: str
    supersedes_work_unit_id: str | None
    legacy_saga_id: str | None
    created_at: float
    started_at: float | None
    completed_at: float | None
    blocked_at: float | None
    failure_code: str | None
    failure_summary: str | None
    version: int


@dataclass(frozen=True)
class MilestoneExecutionRow:
    milestone_execution_id: str
    work_unit_id: str
    milestone_id: str
    stable_key: str
    phase: LifecyclePhase
    ordinal: int
    title: str
    executor_kind: str
    requires_operator_approval: bool
    status: MilestoneExecutionStatus
    attempt: int
    child_workflow_id: str | None
    dispatch_intent_id: str | None
    started_at: float | None
    completed_at: float | None
    blocked_at: float | None
    failure_code: str | None
    failure_summary: str | None
    failure_class: FailureClass | None
    """How the last failure must be handled, or None if there has not been one.

    The one thing that answers "did this BLOCKED milestone spend an attempt?".
    `failure_code` is free text and cannot: `operator_decision_pending` and a
    genuine executor failure are both strings, and a budget that reads them as
    strings fails an approval gate that never ran.
    """

    result_summary: str | None
    version: int


@dataclass(frozen=True)
class WorkUnitEventRow:
    event_id: str
    work_unit_id: str
    sequence_number: int
    event_type: WorkUnitEventType
    phase: LifecyclePhase | None
    milestone_execution_id: str | None
    root_workflow_id: str
    child_workflow_id: str | None
    idempotency_key: str
    payload: dict[str, Any]
    occurred_at: float


@dataclass(frozen=True)
class ArtifactRow:
    artifact_id: str
    work_unit_id: str
    milestone_execution_id: str | None
    artifact_type: ArtifactType
    uri: str
    content_hash: str
    media_type: str | None
    size_bytes: int | None
    producer_workflow_id: str
    producer_step_name: str | None
    metadata: dict[str, Any]
    created_at: float


@dataclass(frozen=True)
class DecisionRequestRow:
    request_id: str
    work_unit_id: str
    milestone_execution_id: str | None
    request_kind: DecisionRequestKind
    prompt: str
    status: DecisionRequestStatus
    decision: OperatorDecision | None
    decision_payload: dict[str, Any]
    decided_by: str | None
    created_at: float
    resolved_at: float | None


@dataclass(frozen=True)
class EnqueueOutboxRow:
    outbox_id: str
    work_unit_id: str
    root_workflow_id: str
    design_doc_revision_id: str
    compiled_plan_revision_id: str
    compiled_plan_hash: str
    lifecycle_profile_version: int
    status: str
    attempts: int
    last_error: str | None


@dataclass(frozen=True)
class FactOutcome:
    """What one submitted fact did.

    ``applied`` distinguishes "this changed state" from "this fact was already
    recorded", which is the difference a replayed dispatcher outcome needs and the
    difference a test asserting idempotency has to be able to see.
    """

    applied: bool
    event: WorkUnitEventRow
    work_unit: WorkUnitRow


@dataclass(frozen=True)
class StartWorkUnitResult:
    work_unit: WorkUnitRow
    root_workflow_id: str
    created: bool


# --------------------------------------------------------------------------- #
# Row mapping
# --------------------------------------------------------------------------- #


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    loaded = json.loads(raw)
    return loaded if isinstance(loaded, dict) else {}


def _json_array(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    loaded = json.loads(raw)
    return loaded if isinstance(loaded, list) else []


def _design_doc_revision(row: Any) -> DesignDocRevisionRow:
    data = rowdict(row)
    return DesignDocRevisionRow(
        design_doc_revision_id=str(data["design_doc_revision_id"]),
        design_doc_id=str(data["design_doc_id"]),
        revision_number=int(data["revision_number"]),
        content_hash=str(data["content_hash"]),
        raw_content=str(data["raw_content"]),
        source_path=data["source_path"],
        schema_version=str(data["schema_version"]),
        created_at=float(data["created_at"]),
        created_by=str(data["created_by"]),
    )


def _compiled_plan_revision(row: Any) -> CompiledPlanRevisionRow:
    data = rowdict(row)
    payload = json.loads(str(data["plan_json"]))
    plan = CompiledWorkPlan.from_payload(payload)
    return CompiledPlanRevisionRow(
        compiled_plan_revision_id=str(data["compiled_plan_revision_id"]),
        work_unit_id=data["work_unit_id"],
        design_doc_revision_id=str(data["design_doc_revision_id"]),
        compiler_version=str(data["compiler_version"]),
        lifecycle_profile=str(data["lifecycle_profile"]),
        lifecycle_profile_version=int(data["lifecycle_profile_version"]),
        plan=plan,
        plan_hash=str(data["plan_hash"]),
        validation_status=ValidationStatus(str(data["validation_status"])),
        validation_errors=tuple(_json_array(data["validation_errors"])),
        execution_blockers=tuple(str(item) for item in _json_array(data["execution_blockers"])),
        created_at=float(data["created_at"]),
    )


def _work_unit(row: Any) -> WorkUnitRow:
    data = rowdict(row)
    return WorkUnitRow(
        work_unit_id=str(data["work_unit_id"]),
        title=str(data["title"]),
        status=WorkUnitStatus(str(data["status"])),
        current_phase=str(data["current_phase"]),
        design_doc_revision_id=str(data["design_doc_revision_id"]),
        compiled_plan_revision_id=str(data["compiled_plan_revision_id"]),
        compiled_plan_hash=str(data["compiled_plan_hash"]),
        lifecycle_profile=str(data["lifecycle_profile"]),
        lifecycle_profile_version=int(data["lifecycle_profile_version"]),
        root_workflow_id=str(data["root_workflow_id"]),
        supersedes_work_unit_id=data["supersedes_work_unit_id"],
        legacy_saga_id=data["legacy_saga_id"],
        created_at=float(data["created_at"]),
        started_at=data["started_at"],
        completed_at=data["completed_at"],
        blocked_at=data["blocked_at"],
        failure_code=data["failure_code"],
        failure_summary=data["failure_summary"],
        version=int(data["version"]),
    )


def _milestone_execution(row: Any) -> MilestoneExecutionRow:
    data = rowdict(row)
    return MilestoneExecutionRow(
        milestone_execution_id=str(data["milestone_execution_id"]),
        work_unit_id=str(data["work_unit_id"]),
        milestone_id=str(data["milestone_id"]),
        stable_key=str(data["stable_key"]),
        phase=LifecyclePhase(str(data["phase"])),
        ordinal=int(data["ordinal"]),
        title=str(data["title"]),
        executor_kind=str(data["executor_kind"]),
        requires_operator_approval=bool(data["requires_operator_approval"]),
        status=MilestoneExecutionStatus(str(data["status"])),
        attempt=int(data["attempt"]),
        child_workflow_id=data["child_workflow_id"],
        dispatch_intent_id=data["dispatch_intent_id"],
        started_at=data["started_at"],
        completed_at=data["completed_at"],
        blocked_at=data["blocked_at"],
        failure_code=data["failure_code"],
        failure_summary=data["failure_summary"],
        failure_class=(
            FailureClass(str(data["failure_class"])) if data.get("failure_class") else None
        ),
        result_summary=data["result_summary"],
        version=int(data["version"]),
    )


def _event(row: Any) -> WorkUnitEventRow:
    data = rowdict(row)
    phase = data["phase"]
    return WorkUnitEventRow(
        event_id=str(data["event_id"]),
        work_unit_id=str(data["work_unit_id"]),
        sequence_number=int(data["sequence_number"]),
        event_type=WorkUnitEventType(str(data["event_type"])),
        phase=LifecyclePhase(str(phase)) if phase else None,
        milestone_execution_id=data["milestone_execution_id"],
        root_workflow_id=str(data["root_workflow_id"]),
        child_workflow_id=data["child_workflow_id"],
        idempotency_key=str(data["idempotency_key"]),
        payload=_json_object(data["payload_json"]),
        occurred_at=float(data["occurred_at"]),
    )


def _artifact(row: Any) -> ArtifactRow:
    data = rowdict(row)
    return ArtifactRow(
        artifact_id=str(data["artifact_id"]),
        work_unit_id=str(data["work_unit_id"]),
        milestone_execution_id=data["milestone_execution_id"],
        artifact_type=parse_artifact_type(str(data["artifact_type"])),
        uri=str(data["uri"]),
        content_hash=str(data["content_hash"]),
        media_type=data["media_type"],
        size_bytes=data["size_bytes"],
        producer_workflow_id=str(data["producer_workflow_id"]),
        producer_step_name=data["producer_step_name"],
        metadata=_json_object(data["metadata_json"]),
        created_at=float(data["created_at"]),
    )


def _decision_request(row: Any) -> DecisionRequestRow:
    data = rowdict(row)
    decision = data["decision"]
    return DecisionRequestRow(
        request_id=str(data["request_id"]),
        work_unit_id=str(data["work_unit_id"]),
        milestone_execution_id=data["milestone_execution_id"],
        request_kind=DecisionRequestKind(str(data["request_kind"])),
        prompt=str(data["prompt"]),
        status=DecisionRequestStatus(str(data["status"])),
        decision=OperatorDecision(str(decision)) if decision else None,
        decision_payload=_json_object(data["decision_payload_json"]),
        decided_by=data["decided_by"],
        created_at=float(data["created_at"]),
        resolved_at=data["resolved_at"],
    )


# --------------------------------------------------------------------------- #
# Immutable definition persistence
# --------------------------------------------------------------------------- #


def insert_design_doc_revision(
    *,
    design_doc_id: str,
    raw_content: str,
    schema_version: str,
    structured_content: dict[str, Any] | None = None,
    source_path: str | None = None,
    created_by: str = "operator",
) -> DesignDocRevisionRow:
    """Persist one immutable source revision, or return the identical one.

    The exact source text is preserved verbatim. The parsed representation is
    stored beside it, never instead of it, so a future compiler version can start
    from the author's words rather than from an older parser's opinion of them.
    """

    content_hash = sha256_text(raw_content)
    t = now()
    with tx() as c:
        existing = c.execute(
            "SELECT * FROM design_doc_revisions WHERE design_doc_id=? AND content_hash=?",
            (design_doc_id, content_hash),
        ).fetchone()
        if existing is not None:
            return _design_doc_revision(existing)
        row = c.execute(
            "SELECT COALESCE(MAX(revision_number), 0) AS highest FROM design_doc_revisions "
            "WHERE design_doc_id=?",
            (design_doc_id,),
        ).fetchone()
        revision_number = int(rowdict(row)["highest"]) + 1
        revision_id = f"ddr_{sha256_text(f'{design_doc_id}:{content_hash}')[:24]}"
        c.execute(
            """
            INSERT INTO design_doc_revisions(
                design_doc_revision_id, design_doc_id, revision_number, content_hash,
                raw_content, structured_content, source_path, schema_version,
                created_at, created_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                design_doc_id,
                revision_number,
                content_hash,
                raw_content,
                json.dumps(structured_content, sort_keys=True) if structured_content else None,
                source_path,
                schema_version,
                t,
                created_by,
            ),
        )
        inserted = c.execute(
            "SELECT * FROM design_doc_revisions WHERE design_doc_revision_id=?",
            (revision_id,),
        ).fetchone()
    return _design_doc_revision(inserted)


def get_design_doc_revision(design_doc_revision_id: str) -> DesignDocRevisionRow:
    with tx() as c:
        row = c.execute(
            "SELECT * FROM design_doc_revisions WHERE design_doc_revision_id=?",
            (design_doc_revision_id,),
        ).fetchone()
    if row is None:
        raise WorkUnitError(f"unknown design doc revision {design_doc_revision_id!r}")
    return _design_doc_revision(row)


def insert_compiled_plan_revision(
    outcome: CompiledPlanOutcome,
    *,
    design_doc_revision_id: str,
) -> CompiledPlanRevisionRow:
    """Persist the plan, its normalized milestones, and its dependency edges atomically.

    The complete plan JSON stays as the immutable compiled artifact. Milestones are
    additionally normalized into rows so readiness, phase membership, and
    dependency direction are relational facts the database can constrain rather
    than opinions a JSON reader forms.
    """

    plan = outcome.plan
    plan_hash = plan.plan_hash()
    revision_id = f"cpr_{sha256_text(f'{design_doc_revision_id}:{plan_hash}')[:24]}"
    t = now()
    with tx() as c:
        existing = c.execute(
            "SELECT * FROM compiled_plan_revisions WHERE compiled_plan_revision_id=?",
            (revision_id,),
        ).fetchone()
        if existing is not None:
            return _compiled_plan_revision(existing)
        c.execute(
            """
            INSERT INTO compiled_plan_revisions(
                compiled_plan_revision_id, work_unit_id, design_doc_revision_id,
                compiler_version, lifecycle_profile, lifecycle_profile_version,
                plan_json, plan_hash, validation_status, validation_errors,
                execution_blockers, created_at
            ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                revision_id,
                design_doc_revision_id,
                plan.compiler_version,
                plan.lifecycle.profile,
                plan.lifecycle.profile_version,
                json.dumps(plan.to_payload(), sort_keys=True),
                plan_hash,
                outcome.validation_status.value,
                json.dumps([item.to_payload() for item in outcome.diagnostics], sort_keys=True),
                json.dumps(list(outcome.execution_blockers), sort_keys=True),
                t,
            ),
        )
        for milestone in plan.ordered_milestones():
            c.execute(
                """
                INSERT INTO compiled_milestones(
                    milestone_id, compiled_plan_revision_id, stable_key, title, description,
                    phase, ordinal, executor_kind, requires_operator_approval,
                    source_start, source_end, source_heading, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _compiled_milestone_id(revision_id, milestone.stable_key),
                    revision_id,
                    milestone.stable_key,
                    milestone.title,
                    milestone.description,
                    milestone.phase.value,
                    milestone.ordinal,
                    milestone.executor_kind.value,
                    1 if milestone.approval_policy.required else 0,
                    milestone.source_provenance.source_start,
                    milestone.source_provenance.source_end,
                    milestone.source_provenance.source_heading,
                    json.dumps(milestone.to_payload(), sort_keys=True),
                ),
            )
        for edge in plan.ordered_dependency_edges():
            c.execute(
                """
                INSERT INTO milestone_dependencies(
                    compiled_plan_revision_id, milestone_id, depends_on_milestone_id
                ) VALUES (?, ?, ?)
                """,
                (
                    revision_id,
                    _compiled_milestone_id(revision_id, edge.milestone_key),
                    _compiled_milestone_id(revision_id, edge.depends_on_key),
                ),
            )
        inserted = c.execute(
            "SELECT * FROM compiled_plan_revisions WHERE compiled_plan_revision_id=?",
            (revision_id,),
        ).fetchone()
    return _compiled_plan_revision(inserted)


def _compiled_milestone_id(compiled_plan_revision_id: str, stable_key: str) -> str:
    return f"cm_{sha256_text(f'{compiled_plan_revision_id}:{stable_key}')[:24]}"


def get_compiled_plan_revision(compiled_plan_revision_id: str) -> CompiledPlanRevisionRow:
    """Load a plan revision, verifying its stored hash on the way out.

    ``CompiledWorkPlan.from_payload`` raises when the payload does not hash to the
    hash stored with it, so a mutated row fails closed here rather than at some
    later point where the damage is already done.
    """

    with tx() as c:
        row = c.execute(
            "SELECT * FROM compiled_plan_revisions WHERE compiled_plan_revision_id=?",
            (compiled_plan_revision_id,),
        ).fetchone()
    if row is None:
        raise WorkUnitError(f"unknown compiled plan revision {compiled_plan_revision_id!r}")
    revision = _compiled_plan_revision(row)
    if revision.plan_hash != revision.plan.plan_hash():
        raise WorkUnitError(
            "stored plan hash does not match the stored plan content for "
            f"{compiled_plan_revision_id!r}"
        )
    return revision


# --------------------------------------------------------------------------- #
# WorkUnit creation
# --------------------------------------------------------------------------- #


def start_work_unit(
    compiled_plan_revision_id: str,
    *,
    title: str | None = None,
    approved_plan_hash: str | None = None,
    supersedes_work_unit_id: str | None = None,
    legacy_saga_id: str | None = None,
) -> StartWorkUnitResult:
    """Create one WorkUnit and its intent to run, atomically and idempotently.

    The WorkUnit row, its milestone execution rows, its first events, and the
    enqueue outbox row commit together in the coordination database. The DBOS
    system database is physically separate, so the enqueue is a transactional
    outbox row rather than a pretend cross-database transaction: the dispatcher
    hands DBOS the explicit workflow ID and only then marks the row delivered.

    A repeated call for the same plan revision returns the existing WorkUnit. It
    does not enqueue a second root execution, because the identity is derived from
    the plan and the unique index on ``root_workflow_id`` makes the duplicate
    unrepresentable rather than merely unlikely.
    """

    revision = get_compiled_plan_revision(compiled_plan_revision_id)
    if revision.execution_blockers:
        raise WorkUnitError(
            "compiled plan has unresolved execution blockers: "
            + "; ".join(revision.execution_blockers)
        )
    plan = revision.plan
    if (
        plan.permission_policy is not None
        and plan.permission_policy.requires_start_approval
        and approved_plan_hash != revision.plan_hash
    ):
        raise WorkUnitError(
            "this plan requests gated capabilities; start must approve its exact "
            f"compiled hash {revision.plan_hash!r}"
        )
    work_unit_id = work_unit_id_for_plan(compiled_plan_revision_id)
    root_workflow_id = root_workflow_id_for(work_unit_id)
    resolved_title = title or (
        plan.ordered_milestones()[0].title if plan.milestones else "work unit"
    )
    t = now()

    with tx() as c:
        existing = c.execute(
            "SELECT * FROM work_units WHERE work_unit_id=? FOR UPDATE",
            (work_unit_id,),
        ).fetchone()
        if existing is not None:
            return StartWorkUnitResult(
                work_unit=_work_unit(existing),
                root_workflow_id=root_workflow_id,
                created=False,
            )
        c.execute(
            """
            INSERT INTO work_units(
                work_unit_id, title, status, current_phase, design_doc_revision_id,
                compiled_plan_revision_id, compiled_plan_hash, lifecycle_profile,
                lifecycle_profile_version, root_workflow_id, supersedes_work_unit_id,
                legacy_saga_id, created_at, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                work_unit_id,
                resolved_title,
                WorkUnitStatus.QUEUED.value,
                ORDERED_PHASES[0].value,
                revision.design_doc_revision_id,
                compiled_plan_revision_id,
                revision.plan_hash,
                plan.lifecycle.profile,
                plan.lifecycle.profile_version,
                root_workflow_id,
                supersedes_work_unit_id,
                legacy_saga_id,
                t,
            ),
        )
        # A link, not a content change: the plan revision now names the single
        # WorkUnit that executes it.
        c.execute(
            "UPDATE compiled_plan_revisions SET work_unit_id=? "
            "WHERE compiled_plan_revision_id=? AND work_unit_id IS NULL",
            (work_unit_id, compiled_plan_revision_id),
        )
        for milestone in plan.ordered_milestones():
            milestone_id = _compiled_milestone_id(compiled_plan_revision_id, milestone.stable_key)
            c.execute(
                """
                INSERT INTO milestone_executions(
                    milestone_execution_id, work_unit_id, milestone_id, status, attempt, version
                ) VALUES (?, ?, ?, ?, 0, 1)
                """,
                (
                    _milestone_execution_id(work_unit_id, milestone.stable_key),
                    work_unit_id,
                    milestone_id,
                    MilestoneExecutionStatus.PENDING.value,
                ),
            )
        for event_type, payload in (
            (
                WorkUnitEventType.WORK_UNIT_CREATED,
                {"work_unit_id": work_unit_id, "title": resolved_title},
            ),
            (
                WorkUnitEventType.PLAN_BOUND,
                {
                    "compiled_plan_revision_id": compiled_plan_revision_id,
                    "compiled_plan_hash": revision.plan_hash,
                    "design_doc_revision_id": revision.design_doc_revision_id,
                    "lifecycle_profile": plan.lifecycle.profile,
                    "lifecycle_profile_version": plan.lifecycle.profile_version,
                },
            ),
            (
                WorkUnitEventType.ROOT_WORKFLOW_ENQUEUED,
                {"root_workflow_id": root_workflow_id},
            ),
        ):
            _insert_event(
                c,
                work_unit_id=work_unit_id,
                root_workflow_id=root_workflow_id,
                event_type=event_type,
                phase=None,
                milestone_execution_id=None,
                child_workflow_id=None,
                key=f"{root_workflow_id}:-:-:0:{event_type.value}",
                payload=payload,
                occurred_at=t,
            )
        c.execute(
            """
            INSERT INTO work_unit_enqueue_outbox(
                outbox_id, work_unit_id, root_workflow_id, design_doc_revision_id,
                compiled_plan_revision_id, compiled_plan_hash, lifecycle_profile_version,
                status, attempts, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?)
            """,
            (
                f"wuo_{work_unit_id}",
                work_unit_id,
                root_workflow_id,
                revision.design_doc_revision_id,
                compiled_plan_revision_id,
                revision.plan_hash,
                plan.lifecycle.profile_version,
                t,
            ),
        )
        created = c.execute(
            "SELECT * FROM work_units WHERE work_unit_id=?",
            (work_unit_id,),
        ).fetchone()
    return StartWorkUnitResult(
        work_unit=_work_unit(created),
        root_workflow_id=root_workflow_id,
        created=True,
    )


def _milestone_execution_id(work_unit_id: str, stable_key: str) -> str:
    return f"mex_{sha256_text(f'{work_unit_id}:{stable_key}')[:24]}"


# --------------------------------------------------------------------------- #
# The canonical transition operation
# --------------------------------------------------------------------------- #


def _insert_event(
    c: ConnectionLike,
    *,
    work_unit_id: str,
    root_workflow_id: str,
    event_type: WorkUnitEventType,
    phase: LifecyclePhase | None,
    milestone_execution_id: str | None,
    child_workflow_id: str | None,
    key: str,
    payload: dict[str, Any],
    occurred_at: float,
) -> WorkUnitEventRow:
    row = c.execute(
        "SELECT COALESCE(MAX(sequence_number), 0) AS highest FROM work_unit_events "
        "WHERE work_unit_id=?",
        (work_unit_id,),
    ).fetchone()
    sequence_number = int(rowdict(row)["highest"]) + 1
    event_id = f"wue_{uuid.uuid4().hex[:24]}"
    c.execute(
        """
        INSERT INTO work_unit_events(
            event_id, work_unit_id, sequence_number, event_type, phase,
            milestone_execution_id, root_workflow_id, child_workflow_id,
            idempotency_key, payload_json, occurred_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            work_unit_id,
            sequence_number,
            event_type.value,
            phase.value if phase is not None else None,
            milestone_execution_id,
            root_workflow_id,
            child_workflow_id,
            key,
            json.dumps(payload, sort_keys=True, default=str),
            occurred_at,
        ),
    )
    inserted = c.execute(
        "SELECT * FROM work_unit_events WHERE event_id=?",
        (event_id,),
    ).fetchone()
    return _event(inserted)


def _load_work_unit(c: ConnectionLike, work_unit_id: str, *, lock: bool = False) -> WorkUnitRow:
    row = c.execute(
        f"SELECT * FROM work_units WHERE work_unit_id=?{' FOR UPDATE' if lock else ''}",
        (work_unit_id,),
    ).fetchone()
    if row is None:
        raise UnknownWorkUnit(work_unit_id)
    return _work_unit(row)


def _load_milestone_execution(
    c: ConnectionLike,
    work_unit_id: str,
    stable_key: str,
) -> MilestoneExecutionRow:
    row = c.execute(
        """
        SELECT e.*, m.stable_key, m.phase, m.ordinal, m.title, m.executor_kind,
               m.requires_operator_approval
        FROM milestone_executions e
        JOIN compiled_milestones m ON m.milestone_id = e.milestone_id
        WHERE e.work_unit_id=? AND m.stable_key=?
        """,
        (work_unit_id, stable_key),
    ).fetchone()
    if row is None:
        raise WorkUnitError(f"work unit {work_unit_id!r} has no milestone {stable_key!r}")
    return _milestone_execution(row)


def _required_artifacts(c: ConnectionLike, milestone_id: str) -> tuple[str, ...]:
    row = c.execute(
        "SELECT metadata_json FROM compiled_milestones WHERE milestone_id=?",
        (milestone_id,),
    ).fetchone()
    if row is None:
        return ()
    payload = _json_object(rowdict(row)["metadata_json"])
    return tuple(str(item) for item in payload.get("required_artifacts", ()))


def _recorded_artifact_types(c: ConnectionLike, milestone_execution_id: str) -> set[str]:
    """The requirable evidence already on record for this milestone execution.

    Filtered to ``RequirableArtifact`` rather than returning every stored name,
    because the one caller is deciding whether a milestone may report success.
    A diagnostic exists only because something failed, so letting one count here
    would let a failed run discharge the very requirement it failed to meet.
    """

    rows = c.execute(
        "SELECT artifact_type FROM work_unit_artifacts WHERE milestone_execution_id=?",
        (milestone_execution_id,),
    ).fetchall()
    recorded = (parse_artifact_type(str(rowdict(row)["artifact_type"])) for row in rows)
    return {item.value for item in recorded if isinstance(item, RequirableArtifact)}


def _insert_artifacts(
    c: ConnectionLike,
    *,
    work_unit_id: str,
    milestone_execution_id: str | None,
    producer_workflow_id: str,
    artifacts: Iterable[ArtifactRecord],
    occurred_at: float,
) -> None:
    for artifact in artifacts:
        existing = c.execute(
            "SELECT artifact_id FROM work_unit_artifacts "
            "WHERE work_unit_id=? AND artifact_type=? AND content_hash=?",
            (work_unit_id, artifact.artifact_type.value, artifact.content_hash),
        ).fetchone()
        if existing is not None:
            continue
        c.execute(
            """
            INSERT INTO work_unit_artifacts(
                artifact_id, work_unit_id, milestone_execution_id, artifact_type, uri,
                content_hash, media_type, size_bytes, producer_workflow_id,
                producer_step_name, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"wua_{sha256_text(f'{work_unit_id}:{artifact.artifact_type.value}:{artifact.content_hash}')[:24]}",
                work_unit_id,
                milestone_execution_id,
                artifact.artifact_type.value,
                artifact.uri,
                artifact.content_hash,
                artifact.media_type,
                artifact.size_bytes,
                producer_workflow_id,
                artifact.producer_step_name,
                json.dumps(artifact.metadata, sort_keys=True, default=str),
                occurred_at,
            ),
        )


def _work_unit_status_for_milestone(
    current: WorkUnitStatus,
    status: MilestoneExecutionStatus,
) -> WorkUnitStatus | None:
    """How a milestone status moves the WorkUnit summary.

    Only the two operator-visible conditions propagate upward automatically. A
    milestone failing does not fail the WorkUnit here: the phase exit policy in
    the root workflow decides that, because a failed milestone can be retried,
    corrected, or replanned first.
    """

    match status:
        case MilestoneExecutionStatus.RUNNING:
            return WorkUnitStatus.RUNNING if current is not WorkUnitStatus.RUNNING else None
        case MilestoneExecutionStatus.WAITING_FOR_OPERATOR:
            return WorkUnitStatus.WAITING_FOR_OPERATOR
        case _:
            return None


def record_fact(
    work_unit_id: str,
    fact: LifecycleFact,
    *,
    child_workflow_id: str | None = None,
) -> FactOutcome:
    """Apply one fact to the WorkUnit aggregate, atomically and idempotently.

    This is the only writer of ``work_units``, ``milestone_executions``,
    ``work_unit_artifacts``, and ``work_unit_events``. Everything else asks for a
    fact to be recorded, which is why an invalid transition is impossible to reach
    from a dispatcher, a CLI command, or an agent.
    """

    t = now()
    with tx() as c:
        unit = _load_work_unit(c, work_unit_id, lock=True)
        key = idempotency_key(unit.root_workflow_id, fact)
        existing = c.execute(
            "SELECT * FROM work_unit_events WHERE idempotency_key=?",
            (key,),
        ).fetchone()
        if existing is not None:
            return FactOutcome(applied=False, event=_event(existing), work_unit=unit)

        milestone: MilestoneExecutionRow | None = None
        milestone_key = getattr(fact, "milestone_key", None)
        if milestone_key:
            milestone = _load_milestone_execution(c, work_unit_id, str(milestone_key))

        new_status: WorkUnitStatus | None = None
        new_phase: str | None = None
        failure_code: str | None = None
        failure_summary: str | None = None

        match fact:
            case WorkUnitTransition():
                assert_work_unit_transition(unit.status, fact.status)
                new_status = fact.status
                if fact.current_phase is not None:
                    new_phase = fact.current_phase.value
                failure_code = fact.failure_code
                failure_summary = fact.failure_summary
            case PhaseTransition():
                current = _phase_status_from_events(c, work_unit_id).get(
                    fact.phase, PhaseStatus.PENDING
                )
                assert_phase_transition(current, fact.status)
                new_phase = fact.phase.value
            case MilestoneTransition():
                assert milestone is not None
                assert_milestone_transition(milestone.status, fact.status)
                if fact.status is MilestoneExecutionStatus.SUCCEEDED:
                    required = set(_required_artifacts(c, milestone.milestone_id))
                    present = _recorded_artifact_types(c, milestone.milestone_execution_id) | {
                        artifact.artifact_type.value
                        for artifact in fact.artifacts
                        if artifact.satisfies_requirement
                    }
                    missing = sorted(required - present)
                    if missing:
                        raise MissingRequiredArtifacts(milestone.stable_key, missing)
                new_status = _work_unit_status_for_milestone(unit.status, fact.status)
                new_phase = fact.phase.value
            case ApprovalRequested():
                assert milestone is not None
                _insert_decision_request(
                    c,
                    request_id=fact.request_id,
                    work_unit_id=work_unit_id,
                    milestone_execution_id=milestone.milestone_execution_id,
                    kind=fact.kind,
                    prompt=fact.prompt,
                    occurred_at=t,
                )
                new_phase = fact.phase.value
            case ApprovalReceived():
                assert milestone is not None
                _resolve_decision_request(
                    c,
                    fact=fact,
                    work_unit_id=work_unit_id,
                    milestone_execution_id=milestone.milestone_execution_id,
                    occurred_at=t,
                )
                new_phase = fact.phase.value
            case ArtifactRecorded():
                assert milestone is not None
                new_phase = fact.phase.value
            case DispatchIntentCreated():
                assert milestone is not None
                _link_milestone_to_dispatch_intent(
                    c,
                    milestone=milestone,
                    dispatch_intent_id=fact.dispatch_intent_id,
                )
                new_phase = fact.phase.value
            case AutomaticCrashRecovery():
                # Nothing to update. The repair it names was written by the facts
                # `recover_dead_execution` recorded; this only marks that an
                # unattended reconciler was the one who asked for them, so the
                # automatic budget has something of its own to count.
                pass
            case LegacySagaReconciled():
                new_phase = fact.derived_phase.value

        artifacts: tuple[ArtifactRecord, ...] = ()
        if isinstance(fact, MilestoneTransition):
            artifacts = fact.artifacts
        elif isinstance(fact, ArtifactRecorded):
            artifacts = (fact.artifact,)
        if artifacts:
            _insert_artifacts(
                c,
                work_unit_id=work_unit_id,
                milestone_execution_id=(
                    milestone.milestone_execution_id if milestone is not None else None
                ),
                producer_workflow_id=child_workflow_id or unit.root_workflow_id,
                artifacts=artifacts,
                occurred_at=t,
            )

        if isinstance(fact, MilestoneTransition):
            assert milestone is not None
            _update_milestone_execution(
                c,
                milestone=milestone,
                fact=fact,
                child_workflow_id=child_workflow_id,
                occurred_at=t,
            )

        event = _insert_event(
            c,
            work_unit_id=work_unit_id,
            root_workflow_id=unit.root_workflow_id,
            event_type=fact.event_type,
            phase=getattr(fact, "phase", None),
            milestone_execution_id=(
                milestone.milestone_execution_id if milestone is not None else None
            ),
            child_workflow_id=child_workflow_id,
            key=key,
            payload=fact.event_payload(),
            occurred_at=t,
        )

        _update_work_unit_summary(
            c,
            unit=unit,
            status=new_status,
            current_phase=new_phase,
            failure_code=failure_code,
            failure_summary=failure_summary,
            occurred_at=t,
        )
        updated = _load_work_unit(c, work_unit_id)
    return FactOutcome(applied=True, event=event, work_unit=updated)


def _link_milestone_to_dispatch_intent(
    c: ConnectionLike,
    *,
    milestone: MilestoneExecutionRow,
    dispatch_intent_id: str,
) -> None:
    """Name the intent this milestone attempt is waiting on.

    `DispatchIntentCreated` appended an event and updated nothing, so the summary
    column every reader consults was NULL for every WorkUnit the live path ever
    produced. The id survived only inside `work_unit_events.payload_json`, which
    is history rather than state, and `cancellation.py` derives both the intents
    to refuse and the agent leases to stop from the column - so a cancellation
    stopped DBOS workflows and left the agent process running, which is verbatim
    the failure that module says it was written to fix.

    Separate from `_update_milestone_execution` on purpose. That function is
    typed on `MilestoneTransition` and derives `started_at`/`completed_at`/
    `blocked_at` from a status this fact does not carry. Creating an intent is
    not a status change, so it must not touch `status`, `attempt`, or any
    timestamp; widening that function to a union would put back the
    branch-on-shape the sum type exists to remove.

    It overwrites rather than coalescing, which is the opposite of
    `_update_milestone_execution`'s treatment of the same column and is the right
    reading here: a second `DispatchIntentCreated` for one milestone is a genuine
    new intent, because the intent's idempotency key includes the attempt. The
    column names the live intent, not the first one ever created.
    """

    cursor = c.execute(
        """
        UPDATE milestone_executions
        SET dispatch_intent_id=?, version=version+1
        WHERE milestone_execution_id=? AND version=?
        """,
        (dispatch_intent_id, milestone.milestone_execution_id, milestone.version),
    )
    if cursor.rowcount != 1:
        raise WorkUnitError(
            f"concurrent modification of milestone execution {milestone.milestone_execution_id!r}"
        )


def _update_milestone_execution(
    c: ConnectionLike,
    *,
    milestone: MilestoneExecutionRow,
    fact: MilestoneTransition,
    child_workflow_id: str | None,
    occurred_at: float,
) -> None:
    started_at = (
        occurred_at
        if fact.status is MilestoneExecutionStatus.RUNNING and milestone.started_at is None
        else milestone.started_at
    )
    completed_at = (
        occurred_at
        if fact.status
        in {
            MilestoneExecutionStatus.SUCCEEDED,
            MilestoneExecutionStatus.FAILED,
            MilestoneExecutionStatus.SKIPPED,
            MilestoneExecutionStatus.CANCELLED,
        }
        else milestone.completed_at
    )
    blocked_at = (
        occurred_at
        if fact.status
        in {MilestoneExecutionStatus.BLOCKED, MilestoneExecutionStatus.WAITING_FOR_OPERATOR}
        else milestone.blocked_at
    )
    cursor = c.execute(
        """
        UPDATE milestone_executions
        SET status=?, attempt=?, child_workflow_id=?, dispatch_intent_id=?,
            started_at=?, completed_at=?, blocked_at=?, failure_code=?,
            failure_summary=?, failure_class=?, result_summary=?, version=version+1
        WHERE milestone_execution_id=? AND version=?
        """,
        (
            fact.status.value,
            max(milestone.attempt, fact.attempt),
            child_workflow_id or fact.child_workflow_id or milestone.child_workflow_id,
            fact.dispatch_intent_id or milestone.dispatch_intent_id,
            started_at,
            completed_at,
            blocked_at,
            fact.failure_code if fact.failure_code is not None else milestone.failure_code,
            fact.failure_summary if fact.failure_summary is not None else milestone.failure_summary,
            # Coalesced like the code beside it, so a status change that carries
            # no failure does not erase the one that put the milestone here.
            (
                fact.failure_class.value
                if fact.failure_class is not None
                else (milestone.failure_class.value if milestone.failure_class else None)
            ),
            fact.result_summary if fact.result_summary is not None else milestone.result_summary,
            milestone.milestone_execution_id,
            milestone.version,
        ),
    )
    if cursor.rowcount != 1:
        raise WorkUnitError(
            f"concurrent modification of milestone execution {milestone.milestone_execution_id!r}"
        )


def _update_work_unit_summary(
    c: ConnectionLike,
    *,
    unit: WorkUnitRow,
    status: WorkUnitStatus | None,
    current_phase: str | None,
    failure_code: str | None,
    failure_summary: str | None,
    occurred_at: float,
) -> None:
    resolved_status = status or unit.status
    started_at = (
        occurred_at
        if resolved_status is WorkUnitStatus.RUNNING and unit.started_at is None
        else unit.started_at
    )
    completed_at = (
        occurred_at
        if resolved_status
        in {WorkUnitStatus.SUCCEEDED, WorkUnitStatus.FAILED, WorkUnitStatus.CANCELLED}
        else unit.completed_at
    )
    blocked_at = (
        occurred_at
        if resolved_status in {WorkUnitStatus.BLOCKED, WorkUnitStatus.WAITING_FOR_OPERATOR}
        else unit.blocked_at
    )
    cursor = c.execute(
        """
        UPDATE work_units
        SET status=?, current_phase=?, started_at=?, completed_at=?, blocked_at=?,
            failure_code=?, failure_summary=?, version=version+1
        WHERE work_unit_id=? AND version=?
        """,
        (
            resolved_status.value,
            current_phase or unit.current_phase,
            started_at,
            completed_at,
            blocked_at,
            failure_code if failure_code is not None else unit.failure_code,
            failure_summary if failure_summary is not None else unit.failure_summary,
            unit.work_unit_id,
            unit.version,
        ),
    )
    if cursor.rowcount != 1:
        raise WorkUnitError(f"concurrent modification of work unit {unit.work_unit_id!r}")


def _insert_decision_request(
    c: ConnectionLike,
    *,
    request_id: str,
    work_unit_id: str,
    milestone_execution_id: str,
    kind: DecisionRequestKind,
    prompt: str,
    occurred_at: float,
) -> None:
    existing = c.execute(
        "SELECT request_id FROM work_unit_decision_requests WHERE request_id=?",
        (request_id,),
    ).fetchone()
    if existing is not None:
        return
    c.execute(
        """
        INSERT INTO work_unit_decision_requests(
            request_id, work_unit_id, milestone_execution_id, request_kind, prompt,
            status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            work_unit_id,
            milestone_execution_id,
            kind.value,
            prompt,
            DecisionRequestStatus.PENDING.value,
            occurred_at,
        ),
    )


def _resolve_decision_request(
    c: ConnectionLike,
    *,
    fact: ApprovalReceived,
    work_unit_id: str,
    milestone_execution_id: str,
    occurred_at: float,
) -> None:
    row = c.execute(
        "SELECT * FROM work_unit_decision_requests WHERE request_id=?",
        (fact.request_id,),
    ).fetchone()
    if row is None:
        raise DecisionRequestMismatch(f"unknown decision request {fact.request_id!r}")
    request = _decision_request(row)
    if request.work_unit_id != work_unit_id:
        raise DecisionRequestMismatch(
            f"decision request {fact.request_id!r} belongs to work unit {request.work_unit_id!r}"
        )
    if request.milestone_execution_id != milestone_execution_id:
        raise DecisionRequestMismatch(
            f"decision request {fact.request_id!r} does not name this milestone"
        )
    if request.status is not DecisionRequestStatus.PENDING:
        raise DecisionRequestMismatch(
            f"decision request {fact.request_id!r} is already {request.status.value}"
        )
    c.execute(
        """
        UPDATE work_unit_decision_requests
        SET status=?, decision=?, decision_payload_json=?, decided_by=?,
            response_idempotency_key=?, resolved_at=?
        WHERE request_id=? AND status=?
        """,
        (
            DecisionRequestStatus.RESOLVED.value,
            fact.decision.value,
            json.dumps(fact.decision_payload, sort_keys=True, default=str),
            fact.decided_by,
            fact.response_idempotency_key,
            occurred_at,
            fact.request_id,
            DecisionRequestStatus.PENDING.value,
        ),
    )


# --------------------------------------------------------------------------- #
# Readers
# --------------------------------------------------------------------------- #


def get_work_unit(work_unit_id: str) -> WorkUnitRow:
    with tx() as c:
        return _load_work_unit(c, work_unit_id)


def find_work_unit_by_root_workflow(root_workflow_id: str) -> WorkUnitRow | None:
    with tx() as c:
        row = c.execute(
            "SELECT * FROM work_units WHERE root_workflow_id=?",
            (root_workflow_id,),
        ).fetchone()
    return _work_unit(row) if row is not None else None


def find_work_unit_by_legacy_saga(saga_id: str) -> WorkUnitRow | None:
    with tx() as c:
        row = c.execute(
            "SELECT * FROM work_units WHERE legacy_saga_id=?",
            (saga_id,),
        ).fetchone()
    return _work_unit(row) if row is not None else None


def list_work_units(status: WorkUnitStatus | None = None) -> tuple[WorkUnitRow, ...]:
    with tx() as c:
        if status is None:
            rows = c.execute("SELECT * FROM work_units ORDER BY created_at DESC").fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM work_units WHERE status=? ORDER BY created_at DESC",
                (status.value,),
            ).fetchall()
    return tuple(_work_unit(row) for row in rows)


def list_milestone_executions(work_unit_id: str) -> tuple[MilestoneExecutionRow, ...]:
    with tx() as c:
        rows = c.execute(
            """
            SELECT e.*, m.stable_key, m.phase, m.ordinal, m.title, m.executor_kind,
                   m.requires_operator_approval
            FROM milestone_executions e
            JOIN compiled_milestones m ON m.milestone_id = e.milestone_id
            WHERE e.work_unit_id=?
            """,
            (work_unit_id,),
        ).fetchall()
    # Lifecycle order, not alphabetical order. Sorting the phase column in SQL
    # would put DELIVER before IMPLEMENT, which is not what a reader of a phase
    # list means by "in order".
    return tuple(
        sorted(
            (_milestone_execution(row) for row in rows),
            key=lambda item: (phase_ordinal(item.phase), item.ordinal, item.stable_key),
        )
    )


def list_work_unit_events(
    work_unit_id: str,
    *,
    after_sequence: int = 0,
    limit: int = 200,
) -> tuple[WorkUnitEventRow, ...]:
    """One WorkUnit's event trace, refusing an id that names no WorkUnit.

    Same argument as `list_work_unit_artifacts` below, and the same failure: an
    empty trace for a mistyped id reads as "nothing has happened to this run".
    An empty trace for a *real* id stays a legitimate answer, which is why the
    check is on the WorkUnit rather than on the row count.
    """

    with tx() as c:
        _load_work_unit(c, work_unit_id)
        rows = c.execute(
            "SELECT * FROM work_unit_events WHERE work_unit_id=? AND sequence_number > ? "
            "ORDER BY sequence_number LIMIT ?",
            (work_unit_id, after_sequence, limit),
        ).fetchall()
    return tuple(_event(row) for row in rows)


def list_work_unit_artifacts(work_unit_id: str) -> tuple[ArtifactRow, ...]:
    """Every artifact one WorkUnit produced, refusing an id that names none.

    The existence check is not ceremony. Without it a mistyped or truncated id
    returns an empty tuple, the caller reports `ok` with no artifacts, and an
    operator reads that as "this run produced no evidence" when the truth is
    "you asked about a WorkUnit that does not exist". That happened: a
    truncated id was pasted from a message, and the empty answer was taken for
    a finding about a blocked run.

    `get_work_unit` already refuses the same input with `unknown work unit`, so
    this is two commands at one layer agreeing rather than a new policy.
    """

    with tx() as c:
        _load_work_unit(c, work_unit_id)
        rows = c.execute(
            "SELECT * FROM work_unit_artifacts WHERE work_unit_id=? "
            "ORDER BY created_at, artifact_id",
            (work_unit_id,),
        ).fetchall()
    return tuple(_artifact(row) for row in rows)


def list_decision_requests(
    work_unit_id: str,
    *,
    status: DecisionRequestStatus | None = None,
) -> tuple[DecisionRequestRow, ...]:
    with tx() as c:
        if status is None:
            rows = c.execute(
                "SELECT * FROM work_unit_decision_requests WHERE work_unit_id=? "
                "ORDER BY created_at",
                (work_unit_id,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM work_unit_decision_requests WHERE work_unit_id=? AND status=? "
                "ORDER BY created_at",
                (work_unit_id, status.value),
            ).fetchall()
    return tuple(_decision_request(row) for row in rows)


def get_decision_request(request_id: str) -> DecisionRequestRow | None:
    with tx() as c:
        row = c.execute(
            "SELECT * FROM work_unit_decision_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
    return _decision_request(row) if row is not None else None


def _phase_status_from_events(
    c: ConnectionLike,
    work_unit_id: str,
) -> dict[LifecyclePhase, PhaseStatus]:
    rows = c.execute(
        "SELECT event_type, phase FROM work_unit_events WHERE work_unit_id=? "
        "ORDER BY sequence_number",
        (work_unit_id,),
    ).fetchall()
    statuses: dict[LifecyclePhase, PhaseStatus] = {}
    for row in rows:
        data = rowdict(row)
        phase_value = data["phase"]
        if not phase_value:
            continue
        status = _PHASE_STATUS_BY_EVENT.get(WorkUnitEventType(str(data["event_type"])))
        if status is None:
            continue
        statuses[LifecyclePhase(str(phase_value))] = status
    return statuses


_PHASE_STATUS_BY_EVENT: dict[WorkUnitEventType, PhaseStatus] = {
    WorkUnitEventType.PHASE_PENDING: PhaseStatus.PENDING,
    WorkUnitEventType.PHASE_STARTED: PhaseStatus.RUNNING,
    WorkUnitEventType.PHASE_COMPLETED: PhaseStatus.SUCCEEDED,
    WorkUnitEventType.PHASE_SKIPPED: PhaseStatus.SKIPPED,
    WorkUnitEventType.PHASE_BLOCKED: PhaseStatus.BLOCKED,
    WorkUnitEventType.PHASE_FAILED: PhaseStatus.FAILED,
    WorkUnitEventType.PHASE_CANCELLED: PhaseStatus.CANCELLED,
}


def automatic_crash_recovery_count(work_unit_id: str) -> int:
    """How many times an unattended reconciler has repaired this WorkUnit.

    Deliberately not `execution_epoch`. That counts every `WORK_UNIT_BLOCKED` and
    `WORK_UNIT_WAITING_FOR_OPERATOR` event, which is every halt however caused: a
    phase that finished and parked, an approval gate waiting on a person, an
    operator resume. Using it as a crash-retry budget would let a WorkUnit that
    asks three approval questions exhaust its allowance for surviving crashes,
    and would let one that crashes repeatedly between approvals never touch it.

    The two counters answer different questions and the epoch's name says which
    one it answers. This one counts events only the reconciler writes.
    """

    with tx() as c:
        row = c.execute(
            "SELECT COUNT(*) AS recoveries FROM work_unit_events "
            "WHERE work_unit_id=? AND event_type=?",
            (work_unit_id, WorkUnitEventType.AUTOMATIC_CRASH_RECOVERY.value),
        ).fetchone()
    return int(rowdict(row)["recoveries"])


def execution_epoch(work_unit_id: str) -> int:
    """How many times this WorkUnit has halted, derived from its own history.

    Counting the halts rather than storing a counter keeps the epoch a property of
    the log: any process computing it from the same events gets the same answer.
    That is what makes a resumed run's transition keys distinct from the previous
    run's while staying stable across a replay of the same run, so re-entry after a
    crash is absorbed as a duplicate but an operator resume is not.
    """

    with tx() as c:
        row = c.execute(
            "SELECT COUNT(*) AS halts FROM work_unit_events "
            "WHERE work_unit_id=? AND event_type IN (?, ?)",
            (
                work_unit_id,
                WorkUnitEventType.WORK_UNIT_BLOCKED.value,
                WorkUnitEventType.WORK_UNIT_WAITING_FOR_OPERATOR.value,
            ),
        ).fetchone()
    return int(rowdict(row)["halts"])


def phase_statuses(work_unit_id: str) -> dict[LifecyclePhase, PhaseStatus]:
    """Current phase statuses, projected from the append-only event log.

    Phases have no row of their own on purpose. Deriving their status from events
    means the projection and the history cannot disagree, and it is what lets the
    cockpit rebuild itself from definitions plus events.
    """

    with tx() as c:
        return _phase_status_from_events(c, work_unit_id)


# --------------------------------------------------------------------------- #
# Enqueue outbox
# --------------------------------------------------------------------------- #


def list_pending_enqueues(limit: int = 20) -> tuple[EnqueueOutboxRow, ...]:
    with tx() as c:
        rows = c.execute(
            "SELECT * FROM work_unit_enqueue_outbox WHERE status='PENDING' "
            "ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()
    return tuple(
        EnqueueOutboxRow(
            outbox_id=str(rowdict(row)["outbox_id"]),
            work_unit_id=str(rowdict(row)["work_unit_id"]),
            root_workflow_id=str(rowdict(row)["root_workflow_id"]),
            design_doc_revision_id=str(rowdict(row)["design_doc_revision_id"]),
            compiled_plan_revision_id=str(rowdict(row)["compiled_plan_revision_id"]),
            compiled_plan_hash=str(rowdict(row)["compiled_plan_hash"]),
            lifecycle_profile_version=int(rowdict(row)["lifecycle_profile_version"]),
            status=str(rowdict(row)["status"]),
            attempts=int(rowdict(row)["attempts"]),
            last_error=rowdict(row)["last_error"],
        )
        for row in rows
    )


def mark_enqueue_delivered(work_unit_id: str) -> None:
    """Mark the outbox row delivered only after DBOS accepted the enqueue."""

    t = now()
    with tx() as c:
        c.execute(
            "UPDATE work_unit_enqueue_outbox SET status='DELIVERED', delivered_at=? "
            "WHERE work_unit_id=? AND status='PENDING'",
            (t, work_unit_id),
        )


def mark_enqueue_failed(work_unit_id: str, error: str) -> None:
    with tx() as c:
        c.execute(
            "UPDATE work_unit_enqueue_outbox SET attempts=attempts+1, last_error=? "
            "WHERE work_unit_id=? AND status='PENDING'",
            (error, work_unit_id),
        )


def event_to_payload(event: WorkUnitEventRow) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "sequence_number": event.sequence_number,
        "event_type": event.event_type.value,
        "phase": event.phase.value if event.phase is not None else None,
        "milestone_execution_id": event.milestone_execution_id,
        "root_workflow_id": event.root_workflow_id,
        "child_workflow_id": event.child_workflow_id,
        "payload": event.payload,
        "occurred_at": iso(event.occurred_at),
    }


def compiled_milestone_for(work_unit_id: str, stable_key: str) -> CompiledMilestone:
    unit = get_work_unit(work_unit_id)
    revision = get_compiled_plan_revision(unit.compiled_plan_revision_id)
    return revision.plan.milestone(stable_key)


__all__ = [
    "ArtifactRow",
    "CompiledPlanRevisionRow",
    "DecisionRequestMismatch",
    "DecisionRequestRow",
    "DesignDocRevisionRow",
    "EnqueueOutboxRow",
    "FactOutcome",
    "LIFECYCLE_PROFILE",
    "LIFECYCLE_PROFILE_VERSION",
    "MilestoneExecutionRow",
    "MissingRequiredArtifacts",
    "ROOT_WORKFLOW_ID_PREFIX",
    "StartWorkUnitResult",
    "UnknownWorkUnit",
    "WorkUnitError",
    "WorkUnitEventRow",
    "WorkUnitRow",
    "compiled_milestone_for",
    "event_to_payload",
    "execution_epoch",
    "find_work_unit_by_legacy_saga",
    "find_work_unit_by_root_workflow",
    "get_compiled_plan_revision",
    "get_decision_request",
    "get_design_doc_revision",
    "get_work_unit",
    "insert_compiled_plan_revision",
    "insert_design_doc_revision",
    "list_decision_requests",
    "list_milestone_executions",
    "list_pending_enqueues",
    "list_work_unit_artifacts",
    "list_work_unit_events",
    "list_work_units",
    "mark_enqueue_delivered",
    "mark_enqueue_failed",
    "phase_statuses",
    "record_fact",
    "root_workflow_id_for",
    "start_work_unit",
    "work_unit_id_for_plan",
]
