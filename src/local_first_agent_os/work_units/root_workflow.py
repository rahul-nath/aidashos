# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The root DBOS workflow and the engine that drives the fixed lifecycle.

One WorkUnit is one root workflow execution. The root iterates the fixed phases,
each phase is a child workflow, and a milestone that does real work is a child
workflow of its phase. Every database read, every clock read, and every call into
the world happens inside a DBOS step, so the workflow bodies stay deterministic
and replay cleanly.

Re-entry is safe by construction rather than by luck. Phase and milestone status
live in the domain tables, so a root execution that starts again after a crash
skips what already terminated. DBOS step checkpoints then prevent repeating the
individual operations inside a milestone that was mid-flight.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .._dbos_runtime import DBOS, SetWorkflowID, dbos_step, dbos_workflow
from ..constants import dispatch_settlement_topic, operator_decision_topic
from ..coordination.failures import FailureV1, exceptional_failure
from ..coordination.store import emit
from ..ids import sha256_text
from . import repository as repo
from .events import (
    ApprovalOutcome,
    ApprovalRequested,
    ArtifactKind,
    ArtifactRecord,
    DecisionRequestKind,
    DecisionRequestStatus,
    Denied,
    MilestoneTransition,
    OperatorDecision,
    PhaseTransition,
    RequirableArtifact,
    WorkUnitTransition,
    decision_outcome,
    parse_artifact_type,
)
from .execution import (
    DeferrableMilestoneRuntime,
    DispatchLedgerPoller,
    DispatchParked,
    DispatchParkedError,
    DispatchSettled,
    DispatchStillActive,
    DispatchWaitResult,
    DispatchWaitTimeout,
    MilestoneAwaitingDispatch,
    MilestoneContext,
    MilestoneExecutorRuntime,
    MilestoneOutcome,
    MilestoneSucceeded,
    classify_dispatch_intent,
    dispatch_backed_runtime,
    dispatch_intent_row,
    evidence_artifact,
)
from .execution_recovery import execution_workflow_id
from .executors import ExecutorKind
from .lifecycle import (
    ORDERED_PHASES,
    TERMINAL_PHASE_STATUSES,
    TERMINAL_WORK_UNIT_STATUSES,
    FailureClass,
    LifecyclePhase,
    MilestoneExecutionStatus,
    PhaseStatus,
    WorkUnitPhaseMarker,
    WorkUnitStatus,
)
from .plan import CompiledWorkPlan, LegacyUnspecifiedDelivery
from .retry import ATTEMPT_BUDGET_EXHAUSTED
from .scheduling import (
    bounded_batch,
    compute_phase_work_set,
    evaluate_phase_exit,
    resolve_schedule_width,
)

SCHEMA_VERSION_WORK_UNIT_EXECUTION = "work_unit_execution.v1"

# A segment of a DBOS workflow identity, not the dispatch-source marker the
# coordination ledger parses. The two spell the same characters and mean
# different things: unifying them would couple durable execution identity to a
# ledger routing key that can change independently.
_WORKFLOW_ID_MILESTONE_SEGMENT = ":milestone:"


def notify_operator_decision(
    work_unit_id: str, milestone_key: str, attempt: int, request_id: str
) -> bool:
    """Wake the milestone parked on this decision request, if one is.

    Returns whether a notification was sent, which is what a test can assert on.
    False is the ordinary answer when no DBOS runtime is active, which is every
    inline execution.

    Best effort by construction, and deliberately so, exactly as
    `notify_dispatch_settlement` is. The waiter's `recv` carries its own durable
    timeout and re-reads the row when it wakes, so a notification that never
    arrives costs latency and not correctness. Raising here would turn a delivery
    problem into a failure of the decision that has already committed.
    """

    if not _dbos_active():
        return False
    assert DBOS is not None
    target = milestone_workflow_id(repo.root_workflow_id_for(work_unit_id), milestone_key, attempt)
    try:
        DBOS.send(
            target,
            {"request_id": request_id},
            topic=operator_decision_topic(request_id),
        )
    except Exception as exc:  # pragma: no cover - delivery is not authoritative
        emit(
            "notify_operator_decision_failed",
            {"request_id": request_id, "error": f"{type(exc).__name__}: {exc}"},
        )
        return False
    return True


def milestone_workflow_id(root_workflow_id: str, milestone_key: str, attempt: int) -> str:
    """The DBOS workflow identity of one milestone attempt.

    Module-level and public because two unrelated callers now need to name the
    same workflow: the engine, which runs it, and the decision writer, which has
    to address a notification at whatever is parked inside it. Those two spelling
    it separately is the failure this prevents, and it would be silent: the
    notification would go nowhere and the wait would expire on its clock.
    """

    return f"{root_workflow_id}{_WORKFLOW_ID_MILESTONE_SEGMENT}{milestone_key}:{attempt}"


# This module had no logger at all, which is how a swallowed exception managed
# to leave no trace anywhere.
logger = logging.getLogger(__name__)


class EnqueueDelivery(StrEnum):
    """How a root execution reaches a process.

    ``DURABLE`` hands the workflow to DBOS and returns; when DBOS is not active the
    outbox row stays pending for a runtime that can take it. ``INLINE`` drives the
    lifecycle in the calling process, which is what a test or a single-shot
    operator command wants.

    These name the working states rather than an internal switch. The mode a caller
    asks for is the mode it gets: a durable request never silently runs the whole
    lifecycle in the foreground, which is exactly what a CLI invocation did before
    this distinction existed.
    """

    DURABLE = "DURABLE"
    INLINE = "INLINE"


class ExecutionInputMismatch(RuntimeError):
    """The workflow input does not match the durable WorkUnit it names.

    Fail closed. A mismatch means the execution was authorized against different
    bytes than the ones on disk, and no retry makes that safe.
    """


@dataclass(frozen=True)
class ExecutionSnapshot:
    """The immutable execution input, verified once at the first checkpoint."""

    work_unit_id: str
    root_workflow_id: str
    title: str
    design_doc_revision_id: str
    compiled_plan_revision_id: str
    compiled_plan_hash: str
    lifecycle_profile: str
    lifecycle_profile_version: int
    plan: CompiledWorkPlan

    def to_payload(self) -> dict[str, Any]:
        return {
            "work_unit_id": self.work_unit_id,
            "root_workflow_id": self.root_workflow_id,
            "title": self.title,
            "design_doc_revision_id": self.design_doc_revision_id,
            "compiled_plan_revision_id": self.compiled_plan_revision_id,
            "compiled_plan_hash": self.compiled_plan_hash,
            "lifecycle_profile": self.lifecycle_profile,
            "lifecycle_profile_version": self.lifecycle_profile_version,
            "plan": self.plan.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ExecutionSnapshot:
        return cls(
            work_unit_id=str(payload["work_unit_id"]),
            root_workflow_id=str(payload["root_workflow_id"]),
            title=str(payload["title"]),
            design_doc_revision_id=str(payload["design_doc_revision_id"]),
            compiled_plan_revision_id=str(payload["compiled_plan_revision_id"]),
            compiled_plan_hash=str(payload["compiled_plan_hash"]),
            lifecycle_profile=str(payload["lifecycle_profile"]),
            lifecycle_profile_version=int(payload["lifecycle_profile_version"]),
            plan=CompiledWorkPlan.from_payload(payload["plan"]),
        )


# --------------------------------------------------------------------------- #
# Durable steps: every side effect the lifecycle has
# --------------------------------------------------------------------------- #


@dbos_step()
def load_execution_snapshot_step(
    work_unit_id: str,
    design_doc_revision_id: str,
    compiled_plan_revision_id: str,
    compiled_plan_hash: str,
    lifecycle_profile_version: int,
) -> dict[str, Any]:
    """Load and verify the immutable execution input.

    Four equalities must hold before any work happens: the stored plan hashes to
    the hash the workflow was started with, the WorkUnit points at that plan
    revision, the plan was compiled from the DesignDoc revision named in the
    input, and the root workflow ID is the one derived from the WorkUnit identity.
    Any mismatch stops the execution here.
    """

    unit = repo.get_work_unit(work_unit_id)
    revision = repo.get_compiled_plan_revision(compiled_plan_revision_id)

    if revision.plan_hash != compiled_plan_hash:
        raise ExecutionInputMismatch(
            f"plan hash mismatch: stored {revision.plan_hash}, input {compiled_plan_hash}"
        )
    if unit.compiled_plan_revision_id != compiled_plan_revision_id:
        raise ExecutionInputMismatch(
            f"work unit {work_unit_id!r} executes plan revision "
            f"{unit.compiled_plan_revision_id!r}, not {compiled_plan_revision_id!r}"
        )
    if unit.compiled_plan_hash != compiled_plan_hash:
        raise ExecutionInputMismatch(
            f"work unit {work_unit_id!r} is bound to plan hash {unit.compiled_plan_hash}"
        )
    if revision.design_doc_revision_id != design_doc_revision_id:
        raise ExecutionInputMismatch(
            f"plan revision was compiled from {revision.design_doc_revision_id!r}, "
            f"not {design_doc_revision_id!r}"
        )
    if unit.root_workflow_id != repo.root_workflow_id_for(work_unit_id):
        raise ExecutionInputMismatch(
            f"work unit {work_unit_id!r} has root workflow {unit.root_workflow_id!r}"
        )
    if revision.lifecycle_profile_version != lifecycle_profile_version:
        raise ExecutionInputMismatch(
            f"plan revision targets lifecycle profile version "
            f"{revision.lifecycle_profile_version}, not {lifecycle_profile_version}"
        )

    return ExecutionSnapshot(
        work_unit_id=work_unit_id,
        root_workflow_id=unit.root_workflow_id,
        title=unit.title,
        design_doc_revision_id=design_doc_revision_id,
        compiled_plan_revision_id=compiled_plan_revision_id,
        compiled_plan_hash=compiled_plan_hash,
        lifecycle_profile=revision.lifecycle_profile,
        lifecycle_profile_version=revision.lifecycle_profile_version,
        plan=revision.plan,
    ).to_payload()


@dbos_step()
def read_work_unit_state_step(work_unit_id: str) -> dict[str, Any]:
    """The current durable state the scheduler needs, read in one place."""

    unit = repo.get_work_unit(work_unit_id)
    executions = repo.list_milestone_executions(work_unit_id)
    phases = repo.phase_statuses(work_unit_id)
    artifacts = repo.list_work_unit_artifacts(work_unit_id)
    return {
        "epoch": repo.execution_epoch(work_unit_id),
        "status": unit.status.value,
        "current_phase": unit.current_phase,
        "milestone_statuses": {item.stable_key: item.status.value for item in executions},
        "milestone_attempts": {item.stable_key: item.attempt for item in executions},
        "phase_statuses": {phase.value: status.value for phase, status in phases.items()},
        "artifact_types": sorted({item.artifact_type.value for item in artifacts}),
    }


@dbos_step()
def record_work_unit_transition_step(
    work_unit_id: str,
    status: str,
    current_phase: str | None = None,
    failure_code: str | None = None,
    failure_summary: str | None = None,
    reason: str | None = None,
    epoch: int = 0,
) -> dict[str, Any]:
    phase: LifecyclePhase | WorkUnitPhaseMarker | None = None
    if current_phase == WorkUnitPhaseMarker.COMPLETE.value:
        phase = WorkUnitPhaseMarker.COMPLETE
    elif current_phase is not None:
        phase = LifecyclePhase(current_phase)
    outcome = repo.record_fact(
        work_unit_id,
        WorkUnitTransition(
            status=WorkUnitStatus(status),
            current_phase=phase,
            failure_code=failure_code,
            failure_summary=failure_summary,
            reason=reason,
            epoch=epoch,
        ),
    )
    return {"applied": outcome.applied, "sequence_number": outcome.event.sequence_number}


@dbos_step()
def record_phase_transition_step(
    work_unit_id: str,
    phase: str,
    status: str,
    reason: str | None = None,
    epoch: int = 0,
) -> dict[str, Any]:
    outcome = repo.record_fact(
        work_unit_id,
        PhaseTransition(
            phase=LifecyclePhase(phase),
            status=PhaseStatus(status),
            reason=reason,
            epoch=epoch,
        ),
    )
    return {"applied": outcome.applied, "sequence_number": outcome.event.sequence_number}


@dbos_step()
def record_milestone_transition_step(
    work_unit_id: str,
    phase: str,
    milestone_key: str,
    status: str,
    attempt: int,
    child_workflow_id: str | None = None,
    dispatch_intent_id: str | None = None,
    result_summary: str | None = None,
    failure_code: str | None = None,
    failure_summary: str | None = None,
    failure_class: str | None = None,
    artifacts_json: str = "[]",
) -> dict[str, Any]:
    artifacts = tuple(
        ArtifactRecord(
            artifact_type=parse_artifact_type(str(item["artifact_type"])),
            uri=str(item["uri"]),
            content_hash=str(item["content_hash"]),
            media_type=item.get("media_type"),
            size_bytes=item.get("size_bytes"),
            producer_step_name=item.get("producer_step_name"),
            metadata=dict(item.get("metadata") or {}),
        )
        for item in json.loads(artifacts_json)
    )
    try:
        outcome = repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase(phase),
                milestone_key=milestone_key,
                status=MilestoneExecutionStatus(status),
                attempt=attempt,
                child_workflow_id=child_workflow_id,
                dispatch_intent_id=dispatch_intent_id,
                result_summary=result_summary,
                failure_code=failure_code,
                failure_summary=failure_summary,
                failure_class=FailureClass(failure_class) if failure_class else None,
                artifacts=artifacts,
            ),
            child_workflow_id=child_workflow_id,
        )
    except repo.MissingRequiredArtifacts as exc:
        # The evidence gate is a domain rule, not a crash: the milestone reported
        # success it cannot prove, so it fails with that as its recorded reason.
        outcome = repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase(phase),
                milestone_key=milestone_key,
                status=MilestoneExecutionStatus.FAILED,
                attempt=attempt,
                child_workflow_id=child_workflow_id,
                failure_code="missing_required_artifacts",
                failure_summary=str(exc),
            ),
            child_workflow_id=child_workflow_id,
        )
        return {
            "applied": outcome.applied,
            "status": MilestoneExecutionStatus.FAILED.value,
            "sequence_number": outcome.event.sequence_number,
        }
    return {
        "applied": outcome.applied,
        "status": status,
        "sequence_number": outcome.event.sequence_number,
    }


@dbos_step()
def request_operator_decision_step(
    work_unit_id: str,
    phase: str,
    milestone_key: str,
    attempt: int,
    prompt: str,
) -> dict[str, Any]:
    """Persist the approval request, then park the milestone and the WorkUnit.

    The request ID is derived from the milestone rather than from the attempt. An
    operator approves a milestone, not one try at it, so a retry after a transient
    failure honors the decision already made instead of asking again. A denial is
    equally durable: it fails the milestone every time it is read.

    An already-resolved request short-circuits, so a resumed milestone does not
    park itself for an instant on a decision that has already been made.
    """

    request_id = f"wud_{sha256_text(f'{work_unit_id}:{milestone_key}')[:24]}"
    existing = repo.get_decision_request(request_id)
    if existing is not None and existing.status is not DecisionRequestStatus.PENDING:
        return {"request_id": request_id, "status": existing.status.value}
    repo.record_fact(
        work_unit_id,
        ApprovalRequested(
            phase=LifecyclePhase(phase),
            milestone_key=milestone_key,
            attempt=attempt,
            request_id=request_id,
            prompt=prompt,
        ),
    )
    repo.record_fact(
        work_unit_id,
        MilestoneTransition(
            phase=LifecyclePhase(phase),
            milestone_key=milestone_key,
            status=MilestoneExecutionStatus.WAITING_FOR_OPERATOR,
            attempt=attempt,
            payload={"request_id": request_id},
        ),
    )
    return {"request_id": request_id, "status": DecisionRequestStatus.PENDING.value}


@dbos_step()
def read_operator_decision_step(request_id: str) -> dict[str, Any]:
    request = repo.get_decision_request(request_id)
    if request is None:
        return {"status": "MISSING", "decision": None}
    return {
        "status": request.status.value,
        "decision": request.decision.value if request.decision is not None else None,
        # The kind travels with the answer because neither means anything alone.
        # A caller holding only "APPROVED" cannot tell whether it resolved the
        # request it is waiting on, which is exactly how an ANSWERED once passed
        # for consent.
        "request_kind": request.request_kind.value,
        "decided_by": request.decided_by,
        "payload": request.decision_payload,
    }


def _milestone_context(
    work_unit_id: str,
    compiled_plan_revision_id: str,
    compiled_plan_hash: str,
    milestone_key: str,
    attempt: int,
    child_workflow_id: str,
) -> MilestoneContext:
    """Rebuild the executor's view of one milestone from durable state.

    Both executor steps rebuild it rather than one passing it to the other: a
    ``MilestoneContext`` holds a compiled milestone, and shipping that between
    two steps would mean serializing part of a hashed plan into a step's result
    and trusting the copy. The plan-hash check below is the reason to reload
    instead, and it has to run in both steps for the same reason it runs at all.
    """

    revision = repo.get_compiled_plan_revision(compiled_plan_revision_id)
    if revision.plan_hash != compiled_plan_hash:
        raise ExecutionInputMismatch("plan hash changed between phases")
    return MilestoneContext(
        work_unit_id=work_unit_id,
        root_workflow_id=repo.root_workflow_id_for(work_unit_id),
        child_workflow_id=child_workflow_id,
        milestone=revision.plan.milestone(milestone_key),
        attempt=attempt,
        design_doc_revision_id=revision.design_doc_revision_id,
        compiled_plan_hash=compiled_plan_hash,
        target_project_id=revision.plan.target_project_id,
        design_doc_excerpt=revision.plan.document_context.render(),
        document_context=revision.plan.document_context,
    )


def _outcome_payload(outcome: MilestoneOutcome) -> dict[str, Any]:
    if isinstance(outcome, MilestoneSucceeded):
        return {
            "state": "settled",
            "succeeded": True,
            "result_summary": outcome.result_summary,
            "artifacts": [artifact.to_payload() for artifact in outcome.artifacts],
        }
    return {
        "state": "settled",
        "succeeded": False,
        "failure_class": outcome.failure_class.value,
        "failure_code": outcome.failure_code,
        "failure_summary": outcome.failure_summary,
        "artifacts": [artifact.to_payload() for artifact in outcome.artifacts],
    }


@dbos_step()
def start_milestone_executor_step(
    work_unit_id: str,
    compiled_plan_revision_id: str,
    compiled_plan_hash: str,
    milestone_key: str,
    attempt: int,
    child_workflow_id: str,
) -> dict[str, Any]:
    """Perform one milestone's side effect and checkpoint what it started.

    This is the only place model calls, commands, and dispatch submissions
    happen, which is what keeps them out of the deterministic workflow bodies.

    It used to also contain the wait for the result, and that was the defect: a
    step checkpoints when it returns, so a step that submitted work and then
    blocked for an hour had recorded nothing when the process died at minute
    fifty-nine. Recovery re-ran it from the top and submitted the work a second
    time. Returning as soon as the submission is durable is what makes the
    replay a no-op, and the intent's idempotency key is what makes it safe even
    when the crash lands between the submit and this return.
    """

    context = _milestone_context(
        work_unit_id,
        compiled_plan_revision_id,
        compiled_plan_hash,
        milestone_key,
        attempt,
        child_workflow_id,
    )
    runtime = get_engine().runtime
    if not isinstance(runtime, DeferrableMilestoneRuntime):
        # A runtime that cannot defer finishes inside this step, exactly as every
        # runtime did before the split. There is nothing to wait for, so there is
        # nothing for the workflow body to wait on.
        return _outcome_payload(runtime.run(context))

    started = runtime.start(context)
    if isinstance(started, MilestoneAwaitingDispatch):
        return {
            "state": "awaiting",
            "dispatch_intent_id": started.dispatch_intent_id,
            "timeout_seconds": started.timeout_seconds,
        }
    return _outcome_payload(started)


@dbos_step()
def read_dispatch_intent_row_step(dispatch_intent_id: str) -> dict[str, Any] | None:
    """Read one dispatch intent row.

    A step because it touches the ledger, and a workflow body must not - the same
    rule that forced `settle_milestone_executor_step` to exist. Classifying the
    row is pure, so that stays in the body where the decision is made.
    """

    return dispatch_intent_row(dispatch_intent_id)


@dbos_step()
def settle_milestone_executor_step(
    work_unit_id: str,
    compiled_plan_revision_id: str,
    compiled_plan_hash: str,
    milestone_key: str,
    attempt: int,
    child_workflow_id: str,
    dispatch_intent_id: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Turn a settled dispatch intent into this milestone's outcome.

    A step because it reads the ledger, and a workflow body must not. It is
    called only after the wait has ended, and it re-reads the row rather than
    trusting whatever woke it.
    """

    context = _milestone_context(
        work_unit_id,
        compiled_plan_revision_id,
        compiled_plan_hash,
        milestone_key,
        attempt,
        child_workflow_id,
    )
    runtime = get_engine().runtime
    if not isinstance(runtime, DeferrableMilestoneRuntime):
        # Reachable only if the engine's runtime was swapped between the start
        # and the settle, which would mean this milestone is being settled by
        # something other than what submitted it. Fail closed rather than invent
        # an outcome for work this runtime never started.
        raise RuntimeError(
            f"milestone {milestone_key!r} is awaiting dispatch intent "
            f"{dispatch_intent_id}, but the engine's runtime cannot settle it"
        )
    awaiting = MilestoneAwaitingDispatch(
        dispatch_intent_id=dispatch_intent_id,
        timeout_seconds=timeout_seconds,
    )
    return _outcome_payload(runtime.settle(context, awaiting))


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #


def _dbos_active() -> bool:
    from ..dbos_app import is_dbos_active

    return is_dbos_active() and SetWorkflowID is not None and DBOS is not None


def _dbos_configured() -> bool:
    """Whether the workflow decorators here are real DBOS decorators.

    The same two facts `_dbos_runtime` used to choose them. True with the
    runtime unlaunched is the one state where calling a decorated function is
    not a plain call: DBOS intercepts it and reaches for the system database.
    """

    from ..settings import get_settings

    return DBOS is not None and get_settings().use_dbos


@dataclass
class WorkUnitEngine:
    """Lifecycle orchestration with its side effects injected.

    The runtime is a constructor argument rather than an import, so the same engine
    drives real dispatch-backed execution and a deterministic simulation. The
    approval wait is bounded: exceeding it leaves the WorkUnit parked in
    ``WAITING_FOR_OPERATOR``, which is a durable state an operator resumes, not a
    failure.
    """

    runtime: MilestoneExecutorRuntime
    approval_wait_seconds: float = 86400.0
    approval_poll_seconds: float = 2.0
    sleeper: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic

    # ----------------------------- root ----------------------------------- #

    def execute(
        self,
        *,
        work_unit_id: str,
        design_doc_revision_id: str,
        compiled_plan_revision_id: str,
        compiled_plan_hash: str,
        lifecycle_profile_version: int,
    ) -> dict[str, Any]:
        snapshot = ExecutionSnapshot.from_payload(
            load_execution_snapshot_step(
                work_unit_id,
                design_doc_revision_id,
                compiled_plan_revision_id,
                compiled_plan_hash,
                lifecycle_profile_version,
            )
        )
        state = read_work_unit_state_step(work_unit_id)
        status = WorkUnitStatus(str(state["status"]))
        if status in TERMINAL_WORK_UNIT_STATUSES:
            return self._result(snapshot, state)
        epoch = int(state["epoch"])
        if status is WorkUnitStatus.QUEUED:
            record_work_unit_transition_step(
                work_unit_id,
                WorkUnitStatus.RUNNING.value,
                ORDERED_PHASES[0].value,
                epoch=epoch,
            )
        elif status in {WorkUnitStatus.WAITING_FOR_OPERATOR, WorkUnitStatus.BLOCKED}:
            record_work_unit_transition_step(
                work_unit_id,
                WorkUnitStatus.RUNNING.value,
                reason="root execution resumed",
                epoch=epoch,
            )

        for phase in snapshot.plan.lifecycle.ordered_phases:
            state = read_work_unit_state_step(work_unit_id)
            if WorkUnitStatus(str(state["status"])) in TERMINAL_WORK_UNIT_STATUSES:
                return self._result(snapshot, state)
            phase_status = self._run_phase_boundary(snapshot, phase, state)
            if phase_status in {PhaseStatus.SUCCEEDED, PhaseStatus.SKIPPED}:
                continue
            return self._halt(snapshot, phase, phase_status)

        return self._complete(snapshot)

    def _run_phase_boundary(
        self,
        snapshot: ExecutionSnapshot,
        phase: LifecyclePhase,
        state: dict[str, Any],
    ) -> PhaseStatus:
        recorded = {
            LifecyclePhase(key): PhaseStatus(value)
            for key, value in dict(state["phase_statuses"]).items()
        }
        existing = recorded.get(phase)
        if existing is not None and existing in TERMINAL_PHASE_STATUSES:
            # Already settled by an earlier execution of this root workflow.
            return existing
        if _dbos_active():
            assert SetWorkflowID is not None
            with SetWorkflowID(phase_workflow_id(snapshot, phase, int(state["epoch"]))):
                payload = run_phase_workflow(
                    snapshot.work_unit_id,
                    snapshot.design_doc_revision_id,
                    snapshot.compiled_plan_revision_id,
                    snapshot.compiled_plan_hash,
                    snapshot.lifecycle_profile_version,
                    phase.value,
                )
        else:
            payload = self.run_phase(
                work_unit_id=snapshot.work_unit_id,
                design_doc_revision_id=snapshot.design_doc_revision_id,
                compiled_plan_revision_id=snapshot.compiled_plan_revision_id,
                compiled_plan_hash=snapshot.compiled_plan_hash,
                lifecycle_profile_version=snapshot.lifecycle_profile_version,
                phase=phase,
            )
        return PhaseStatus(str(payload["status"]))

    def _halt(
        self,
        snapshot: ExecutionSnapshot,
        phase: LifecyclePhase,
        phase_status: PhaseStatus,
    ) -> dict[str, Any]:
        state = read_work_unit_state_step(snapshot.work_unit_id)
        milestone_statuses = {
            key: MilestoneExecutionStatus(value)
            for key, value in dict(state["milestone_statuses"]).items()
        }
        waiting = any(
            status is MilestoneExecutionStatus.WAITING_FOR_OPERATOR
            for status in milestone_statuses.values()
        )
        epoch = int(state["epoch"])
        if phase_status is PhaseStatus.FAILED:
            record_work_unit_transition_step(
                snapshot.work_unit_id,
                WorkUnitStatus.FAILED.value,
                phase.value,
                failure_code=f"{phase.value.lower()}_phase_failed",
                failure_summary=f"phase {phase.value} failed and its policy blocks the lifecycle",
                epoch=epoch,
            )
        elif phase_status is PhaseStatus.CANCELLED:
            record_work_unit_transition_step(
                snapshot.work_unit_id,
                WorkUnitStatus.CANCELLED.value,
                phase.value,
                reason=f"phase {phase.value} was cancelled",
                epoch=epoch,
            )
        elif waiting:
            record_work_unit_transition_step(
                snapshot.work_unit_id,
                WorkUnitStatus.WAITING_FOR_OPERATOR.value,
                phase.value,
                reason=f"phase {phase.value} is waiting for an operator decision",
                epoch=epoch,
            )
        else:
            record_work_unit_transition_step(
                snapshot.work_unit_id,
                WorkUnitStatus.BLOCKED.value,
                phase.value,
                failure_code=f"{phase.value.lower()}_phase_blocked",
                failure_summary=f"phase {phase.value} cannot proceed without intervention",
                epoch=epoch,
            )
        return self._result(snapshot, read_work_unit_state_step(snapshot.work_unit_id))

    def _complete(self, snapshot: ExecutionSnapshot) -> dict[str, Any]:
        state = read_work_unit_state_step(snapshot.work_unit_id)
        epoch = int(state["epoch"])
        if isinstance(snapshot.plan.delivery_contract, LegacyUnspecifiedDelivery):
            record_work_unit_transition_step(
                snapshot.work_unit_id,
                WorkUnitStatus.FAILED.value,
                ORDERED_PHASES[-1].value,
                failure_code="missing_delivery_contract",
                failure_summary=(
                    "this legacy compiled plan did not state whether terminal delivery "
                    "evidence was required"
                ),
                epoch=epoch,
            )
            return self._result(snapshot, read_work_unit_state_step(snapshot.work_unit_id))
        produced = set(state["artifact_types"])
        missing = sorted(set(snapshot.plan.required_final_artifacts) - produced)
        if missing:
            record_work_unit_transition_step(
                snapshot.work_unit_id,
                WorkUnitStatus.FAILED.value,
                ORDERED_PHASES[-1].value,
                failure_code="missing_required_final_artifacts",
                failure_summary="required final artifacts absent: " + ", ".join(missing),
                epoch=epoch,
            )
        else:
            record_work_unit_transition_step(
                snapshot.work_unit_id,
                WorkUnitStatus.SUCCEEDED.value,
                WorkUnitPhaseMarker.COMPLETE.value,
                epoch=epoch,
            )
        return self._result(snapshot, read_work_unit_state_step(snapshot.work_unit_id))

    def _result(self, snapshot: ExecutionSnapshot, state: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION_WORK_UNIT_EXECUTION,
            "work_unit_id": snapshot.work_unit_id,
            "root_workflow_id": snapshot.root_workflow_id,
            "compiled_plan_hash": snapshot.compiled_plan_hash,
            "status": state["status"],
            "current_phase": state["current_phase"],
            "phase_statuses": state["phase_statuses"],
            "milestone_statuses": state["milestone_statuses"],
        }

    # ----------------------------- phase ---------------------------------- #

    def run_phase(
        self,
        *,
        work_unit_id: str,
        design_doc_revision_id: str,
        compiled_plan_revision_id: str,
        compiled_plan_hash: str,
        lifecycle_profile_version: int,
        phase: LifecyclePhase,
    ) -> dict[str, Any]:
        """Execute one phase: select, schedule, aggregate, and apply exit policy.

        The phase re-verifies the same immutable input the root verified. That is
        not redundant: a phase child workflow can be recovered independently, and
        it must never trust identifiers it did not check itself.
        """

        snapshot = ExecutionSnapshot.from_payload(
            load_execution_snapshot_step(
                work_unit_id,
                design_doc_revision_id,
                compiled_plan_revision_id,
                compiled_plan_hash,
                lifecycle_profile_version,
            )
        )
        plan = snapshot.plan
        epoch = int(read_work_unit_state_step(work_unit_id)["epoch"])
        if not plan.milestones_in_phase(phase):
            record_phase_transition_step(
                work_unit_id,
                phase.value,
                PhaseStatus.SKIPPED.value,
                reason="the compiled plan assigns no milestones to this phase",
                epoch=epoch,
            )
            return {"phase": phase.value, "status": PhaseStatus.SKIPPED.value}

        record_phase_transition_step(
            work_unit_id,
            phase.value,
            PhaseStatus.RUNNING.value,
            epoch=epoch,
        )

        # A phase loop must be able to prove it is making progress. The signature
        # is the durable state the scheduler reacts to; if a full iteration leaves
        # it unchanged, the phase cannot advance on its own and exits to its exit
        # policy rather than spinning.
        previous_signature: tuple[tuple[str, str], ...] | None = None
        while True:
            state = read_work_unit_state_step(work_unit_id)
            if WorkUnitStatus(str(state["status"])) is WorkUnitStatus.CANCELLED:
                break
            statuses = {
                key: MilestoneExecutionStatus(value)
                for key, value in dict(state["milestone_statuses"]).items()
            }
            attempts = {key: int(value) for key, value in dict(state["milestone_attempts"]).items()}
            work_set = compute_phase_work_set(plan, phase, statuses)
            for key in work_set.unreachable:
                record_milestone_transition_step(
                    work_unit_id,
                    phase.value,
                    key,
                    MilestoneExecutionStatus.SKIPPED.value,
                    attempts.get(key, 0) + 1,
                    failure_code="dependency_unreachable",
                    failure_summary="a dependency did not succeed, so this milestone cannot run",
                )
            signature = tuple(
                sorted((key, f"{value}:{attempts.get(key, 0)}") for key, value in statuses.items())
            )
            if signature == previous_signature:
                break
            previous_signature = signature
            if not work_set.ready:
                break
            # Recomputed per iteration rather than hoisted: the ready set shrinks
            # and refills as milestones settle, and the width follows it. A width
            # computed once from the first ready set would keep asking for a
            # concurrency the graph stopped being able to use.
            width = resolve_schedule_width(plan, work_set.ready)
            batch = bounded_batch(work_set.ready, width.effective)
            # A milestone already marked READY keeps the attempt it was marked
            # with. Incrementing again would record a second READY fact for the
            # same try and leave the attempt counter ahead of the work.
            attempt_by_key = {
                key: (
                    attempts.get(key, 1)
                    if statuses.get(key) is MilestoneExecutionStatus.READY
                    else attempts.get(key, 0) + 1
                )
                for key in batch
            }
            # A milestone whose next try would exceed the attempt budget its plan
            # compiled is failed here rather than run. `max_attempts` was carried
            # into the plan, hashed, and then consulted nowhere, so a milestone
            # that failed repeatedly was retried until the loop's own signature
            # check happened to stop changing - a bound by accident, unrelated to
            # the one the document asked for.
            # Count attempts already spent, not the one about to start: a fresh
            # milestone has spent none and must run even where the budget is one.
            # Only a milestone being retried after a failure has spent an attempt.
            # A parked one has not: `review.operator` permits a single attempt, so
            # counting its wait for a human as a spent try failed every approval
            # gate the moment it asked for one.
            exhausted = tuple(
                key
                for key in batch
                if statuses.get(key) is MilestoneExecutionStatus.FAILED
                and attempts.get(key, 0) >= plan.milestone(key).failure_policy.max_attempts
            )
            for key in exhausted:
                spent = plan.milestone(key).failure_policy.max_attempts
                record_milestone_transition_step(
                    work_unit_id,
                    phase.value,
                    key,
                    MilestoneExecutionStatus.FAILED.value,
                    attempts.get(key, spent),
                    failure_code=ATTEMPT_BUDGET_EXHAUSTED,
                    failure_summary=(f"milestone {key} exhausted its {spent} permitted attempt(s)"),
                )
            batch = tuple(key for key in batch if key not in exhausted)
            if not batch:
                # Recording the failures changed the state, so the loop's own
                # signature check decides whether anything is still runnable.
                # Breaking here instead would abandon phases that never ran.
                continue
            for key in batch:
                if statuses.get(key) is MilestoneExecutionStatus.READY:
                    continue
                record_milestone_transition_step(
                    work_unit_id,
                    phase.value,
                    key,
                    MilestoneExecutionStatus.READY.value,
                    attempt_by_key[key],
                )
            self._run_batch(snapshot, phase, batch, attempt_by_key)

        final_state = read_work_unit_state_step(work_unit_id)
        statuses = {
            key: MilestoneExecutionStatus(value)
            for key, value in dict(final_state["milestone_statuses"]).items()
        }
        status = evaluate_phase_exit(plan, phase, statuses)
        record_phase_transition_step(
            work_unit_id,
            phase.value,
            status.value,
            epoch=int(final_state["epoch"]),
        )
        return {"phase": phase.value, "status": status.value}

    def _run_batch(
        self,
        snapshot: ExecutionSnapshot,
        phase: LifecyclePhase,
        batch: tuple[str, ...],
        attempt_by_key: dict[str, int],
    ) -> None:
        """Run independent ready milestones concurrently, then wait for all of them.

        Parallel execution changes only when work happens. Each milestone records
        its own transitions, so siblings completing in either order leave the same
        durable state.
        """

        if len(batch) == 1:
            key = batch[0]
            self._run_milestone_boundary(snapshot, phase, key, attempt_by_key[key])
            return
        if _dbos_active():
            assert DBOS is not None and SetWorkflowID is not None
            handles = []
            for key in batch:
                attempt = attempt_by_key[key]
                with SetWorkflowID(self._milestone_workflow_id(snapshot, key, attempt)):
                    handles.append(
                        DBOS.start_workflow(
                            execute_milestone_workflow,
                            snapshot.work_unit_id,
                            snapshot.design_doc_revision_id,
                            snapshot.compiled_plan_revision_id,
                            snapshot.compiled_plan_hash,
                            snapshot.lifecycle_profile_version,
                            phase.value,
                            key,
                            attempt,
                        )
                    )
            for handle in handles:
                handle.get_result()
            return
        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            futures = [
                pool.submit(
                    self._run_milestone_boundary,
                    snapshot,
                    phase,
                    key,
                    attempt_by_key[key],
                )
                for key in batch
            ]
            for future in futures:
                future.result()

    # A segment of a DBOS workflow identity, not the dispatch-source marker the
    # coordination ledger parses. The two spell the same characters and mean
    # different things: unifying them would couple durable execution identity to
    # a ledger routing key that can change independently.
    def _await_dispatch_settlement(
        self,
        *,
        child_workflow_id: str,
        dispatch_intent_id: str,
        timeout_seconds: float,
    ) -> DispatchWaitResult:
        """Block this milestone until its dispatch intent stops moving.

        Under DBOS this is `recv`, which is why the wait had to move out of the
        step: `recv` raises unless it is called from a workflow body. It costs no
        polling. One listener thread per process holds a Postgres `LISTEN`, and a
        waiting workflow blocks on an event that listener signals.

        Three properties make it safe rather than merely cheaper. A message sent
        before this call is still delivered, because `recv` checks the
        notifications table before it waits, so a fast agent cannot settle into a
        lost wakeup. A completed `recv` is checkpointed, so a replay after a
        crash returns the message instead of waiting again. And the timeout is a
        durable sleep, so a restart does not silently restart the clock.

        The row is read **before** the wait and again **after** it. Before,
        because a durable `recv` whose notification was already consumed - by a
        process that then died, which is exactly what a restart mid-flight
        produces - waits the whole bound for a message that no longer exists,
        while the answer is sitting in a row it could have looked at. After,
        because a wake is a hint that something changed and the ledger is what
        changed; trusting the message would make the outcome depend on who sent
        it.

        It returns what it found rather than nothing. Returning nothing is why
        the caller had one response for three different situations, and why an
        intent that paused was reported as an agent that never answered.

        Without DBOS there is no channel to be woken on, and polling the ledger
        is the only honest answer. Both paths end at the same question.
        """

        already = classify_dispatch_intent(
            dispatch_intent_id,
            read_dispatch_intent_row_step(dispatch_intent_id),
            waited_seconds=0.0,
        )
        if not isinstance(already, DispatchStillActive):
            return already

        if _dbos_active():
            assert DBOS is not None
            DBOS.recv(
                topic=dispatch_settlement_topic(dispatch_intent_id),
                timeout_seconds=timeout_seconds,
            )
            return classify_dispatch_intent(
                dispatch_intent_id,
                read_dispatch_intent_row_step(dispatch_intent_id),
                waited_seconds=timeout_seconds,
            )

        runtime = get_engine().runtime
        if not isinstance(runtime, DispatchLedgerPoller):
            raise RuntimeError(
                f"milestone {child_workflow_id} is awaiting dispatch intent "
                f"{dispatch_intent_id}, but its runtime offers neither DBOS "
                "notification nor a poll to wait with"
            )
        return runtime.poll_until_stopped(dispatch_intent_id, timeout_seconds)

    def _block_on_halted_dispatch(
        self,
        *,
        work_unit_id: str,
        phase: LifecyclePhase,
        milestone_key: str,
        attempt: int,
        child_workflow_id: str,
        failure_code: str,
        failure_summary: str,
    ) -> dict[str, Any]:
        """Park the milestone on a dispatch that stopped without an outcome.

        ``BLOCKED`` either way, because both endings are resumable and neither is
        a decision that the work failed. The failure code is what differs, and it
        is the whole point: `dispatch_paused` names a checkpoint an operator can
        go and look at, `dispatch_wait_elapsed` names a clock that ran out. One
        code for both said the second when the ledger knew the first.
        """

        record_milestone_transition_step(
            work_unit_id,
            phase.value,
            milestone_key,
            MilestoneExecutionStatus.BLOCKED.value,
            attempt,
            child_workflow_id=child_workflow_id,
            failure_code=failure_code,
            failure_summary=failure_summary,
        )
        return {"status": MilestoneExecutionStatus.BLOCKED.value}

    def _milestone_workflow_id(
        self,
        snapshot: ExecutionSnapshot,
        milestone_key: str,
        attempt: int,
    ) -> str:
        return milestone_workflow_id(snapshot.root_workflow_id, milestone_key, attempt)

    def _run_milestone_boundary(
        self,
        snapshot: ExecutionSnapshot,
        phase: LifecyclePhase,
        milestone_key: str,
        attempt: int,
    ) -> dict[str, Any]:
        if _dbos_active():
            assert SetWorkflowID is not None
            with SetWorkflowID(self._milestone_workflow_id(snapshot, milestone_key, attempt)):
                return execute_milestone_workflow(
                    snapshot.work_unit_id,
                    snapshot.design_doc_revision_id,
                    snapshot.compiled_plan_revision_id,
                    snapshot.compiled_plan_hash,
                    snapshot.lifecycle_profile_version,
                    phase.value,
                    milestone_key,
                    attempt,
                )
        return self.execute_milestone(
            work_unit_id=snapshot.work_unit_id,
            design_doc_revision_id=snapshot.design_doc_revision_id,
            compiled_plan_revision_id=snapshot.compiled_plan_revision_id,
            compiled_plan_hash=snapshot.compiled_plan_hash,
            lifecycle_profile_version=snapshot.lifecycle_profile_version,
            phase=phase,
            milestone_key=milestone_key,
            attempt=attempt,
        )

    # --------------------------- milestone -------------------------------- #

    def execute_milestone(
        self,
        *,
        work_unit_id: str,
        design_doc_revision_id: str,
        compiled_plan_revision_id: str,
        compiled_plan_hash: str,
        lifecycle_profile_version: int,
        phase: LifecyclePhase,
        milestone_key: str,
        attempt: int,
    ) -> dict[str, Any]:
        """Run one milestone through its gate, its executor, and its evidence."""

        snapshot = ExecutionSnapshot.from_payload(
            load_execution_snapshot_step(
                work_unit_id,
                design_doc_revision_id,
                compiled_plan_revision_id,
                compiled_plan_hash,
                lifecycle_profile_version,
            )
        )
        milestone = snapshot.plan.milestone(milestone_key)
        child_workflow_id = milestone_workflow_id(
            repo.root_workflow_id_for(work_unit_id), milestone_key, attempt
        )

        if milestone.approval_policy.required:
            decision = self._await_operator_decision(
                work_unit_id=work_unit_id,
                phase=phase,
                milestone_key=milestone_key,
                attempt=attempt,
                prompt=milestone.approval_policy.prompt,
            )
            if decision is None:
                record_milestone_transition_step(
                    work_unit_id,
                    phase.value,
                    milestone_key,
                    MilestoneExecutionStatus.BLOCKED.value,
                    attempt,
                    child_workflow_id=child_workflow_id,
                    failure_code="operator_decision_pending",
                    failure_summary="the approval wait elapsed with no operator decision",
                )
                return {"status": MilestoneExecutionStatus.BLOCKED.value}
            # Exhaustive over `ApprovalOutcome`, which has exactly two members.
            # The rule this replaces was "not DENIED means go", and "not denied"
            # is not consent: it also admitted a clarification answer, and would
            # have admitted every member added to the decision enum later.
            if isinstance(decision, Denied):
                record_milestone_transition_step(
                    work_unit_id,
                    phase.value,
                    milestone_key,
                    MilestoneExecutionStatus.FAILED.value,
                    attempt,
                    child_workflow_id=child_workflow_id,
                    failure_code="operator_denied",
                    failure_summary=(
                        f"operator {decision.decided_by} denied the approval this "
                        "milestone required"
                    ),
                )
                return {"status": MilestoneExecutionStatus.FAILED.value}

        record_milestone_transition_step(
            work_unit_id,
            phase.value,
            milestone_key,
            MilestoneExecutionStatus.RUNNING.value,
            attempt,
            child_workflow_id=child_workflow_id,
        )

        if milestone.executor_kind is ExecutorKind.REVIEW_OPERATOR:
            # The approval already granted above IS this milestone's work. Its
            # evidence is the decision record, produced here rather than by a
            # runtime that could have produced it without an operator.
            outcome = MilestoneSucceeded(
                result_summary=f"operator approved {milestone_key}",
                artifacts=(
                    evidence_artifact(
                        MilestoneContext(
                            work_unit_id=work_unit_id,
                            root_workflow_id=repo.root_workflow_id_for(work_unit_id),
                            child_workflow_id=child_workflow_id,
                            milestone=milestone,
                            attempt=attempt,
                            design_doc_revision_id=snapshot.design_doc_revision_id,
                            compiled_plan_hash=compiled_plan_hash,
                        ),
                        RequirableArtifact(ArtifactKind.OPERATOR_APPROVAL),
                        content=f"approved:{milestone_key}:{attempt}",
                        step_name="operator_approval",
                    ),
                ),
            )
            payload: dict[str, Any] = {
                "succeeded": True,
                "result_summary": outcome.result_summary,
                "artifacts": [artifact.to_payload() for artifact in outcome.artifacts],
            }
        else:
            try:
                payload = start_milestone_executor_step(
                    work_unit_id,
                    compiled_plan_revision_id,
                    compiled_plan_hash,
                    milestone_key,
                    attempt,
                    child_workflow_id,
                )
                if payload["state"] == "awaiting":
                    # The wait happens here, in the workflow body, and not in
                    # either step. `DBOS.recv` refuses to run inside a step, and
                    # a step that blocked would checkpoint nothing for the whole
                    # duration of the block, which is the defect this split
                    # exists to remove.
                    waited = self._await_dispatch_settlement(
                        child_workflow_id=child_workflow_id,
                        dispatch_intent_id=str(payload["dispatch_intent_id"]),
                        timeout_seconds=float(payload["timeout_seconds"]),
                    )
                    # Three findings, three responses. One of them used to be
                    # missing, so an intent that paused waited out its whole
                    # bound and was then reported as one that never answered.
                    match waited:
                        case DispatchSettled():
                            payload = settle_milestone_executor_step(
                                work_unit_id,
                                compiled_plan_revision_id,
                                compiled_plan_hash,
                                milestone_key,
                                attempt,
                                child_workflow_id,
                                str(payload["dispatch_intent_id"]),
                                float(payload["timeout_seconds"]),
                            )
                        case DispatchParked():
                            return self._block_on_halted_dispatch(
                                work_unit_id=work_unit_id,
                                phase=phase,
                                milestone_key=milestone_key,
                                attempt=attempt,
                                child_workflow_id=child_workflow_id,
                                failure_code="dispatch_paused",
                                failure_summary=waited.describe(),
                            )
                        case DispatchStillActive():
                            return self._block_on_halted_dispatch(
                                work_unit_id=work_unit_id,
                                phase=phase,
                                milestone_key=milestone_key,
                                attempt=attempt,
                                child_workflow_id=child_workflow_id,
                                failure_code="dispatch_wait_elapsed",
                                failure_summary=waited.describe(),
                            )
            except DispatchParkedError as exc:
                return self._block_on_halted_dispatch(
                    work_unit_id=work_unit_id,
                    phase=phase,
                    milestone_key=milestone_key,
                    attempt=attempt,
                    child_workflow_id=child_workflow_id,
                    failure_code="dispatch_paused",
                    failure_summary=str(exc),
                )
            except DispatchWaitTimeout as exc:
                return self._block_on_halted_dispatch(
                    work_unit_id=work_unit_id,
                    phase=phase,
                    milestone_key=milestone_key,
                    attempt=attempt,
                    child_workflow_id=child_workflow_id,
                    failure_code="dispatch_wait_elapsed",
                    failure_summary=str(exc),
                )

        if payload["succeeded"]:
            result = record_milestone_transition_step(
                work_unit_id,
                phase.value,
                milestone_key,
                MilestoneExecutionStatus.SUCCEEDED.value,
                attempt,
                child_workflow_id=child_workflow_id,
                result_summary=str(payload["result_summary"]),
                artifacts_json=json.dumps(payload["artifacts"], sort_keys=True),
            )
            return {"status": str(result["status"])}

        failure_class = FailureClass(str(payload["failure_class"]))
        status = self._status_for_failure(failure_class)
        if status is MilestoneExecutionStatus.WAITING_FOR_OPERATOR:
            request = request_operator_decision_step(
                work_unit_id,
                phase.value,
                milestone_key,
                attempt,
                prompt=(
                    f"Milestone {milestone_key} needs an operator decision: "
                    f"{payload['failure_summary']}"
                ),
            )
            return {
                "status": MilestoneExecutionStatus.WAITING_FOR_OPERATOR.value,
                "request_id": request["request_id"],
            }
        record_milestone_transition_step(
            work_unit_id,
            phase.value,
            milestone_key,
            status.value,
            attempt,
            child_workflow_id=child_workflow_id,
            failure_code=str(payload["failure_code"]),
            failure_summary=str(payload["failure_summary"]),
            # The class is what a later retry decision needs. Writing only the
            # code left the resume path guessing from a string which BLOCKED
            # milestones had spent a try, and it could not.
            failure_class=failure_class.value,
            artifacts_json=json.dumps(payload.get("artifacts") or [], sort_keys=True),
        )
        return {"status": status.value}

    def _status_for_failure(self, failure_class: FailureClass) -> MilestoneExecutionStatus:
        """Map a failure class onto the state the milestone should occupy.

        Transient retries are the step's own business, so a failure that reaches
        here has already exhausted them. What is left is whether the work can be
        picked up again (blocked), needs a person (waiting), or is over (failed).
        """

        match failure_class:
            case FailureClass.TRANSIENT | FailureClass.CORRECTABLE | FailureClass.REQUIRES_REPLAN:
                return MilestoneExecutionStatus.BLOCKED
            case FailureClass.REQUIRES_OPERATOR:
                return MilestoneExecutionStatus.WAITING_FOR_OPERATOR
            case FailureClass.POLICY_VIOLATION | FailureClass.NONRECOVERABLE:
                return MilestoneExecutionStatus.FAILED

    def _await_operator_decision(
        self,
        *,
        work_unit_id: str,
        phase: LifecyclePhase,
        milestone_key: str,
        attempt: int,
        prompt: str,
    ) -> ApprovalOutcome | None:
        """Park the milestone on a named request and wait durably for its answer.

        The wait is against the durable request row rather than in-process state,
        which is why it survives a restart: a new execution finds the same request
        and either sees the decision or keeps waiting.

        The loop re-reads rather than trusting whatever woke it, for the same
        reason the dispatch wait does: a wake is a hint that something changed,
        and the row is what changed. That also makes a spurious wake harmless.

        Returns the answer as a value the caller can only branch on exhaustively,
        rather than a row with a string in it. `request_operator_decision_step`
        opens an APPROVAL request, so `decision_outcome` is asked for that kind
        literally and narrows its result to `ApprovalOutcome`: a clarification
        answer cannot come back from here, and a caller cannot write a branch
        that pretends one did.
        """

        request = request_operator_decision_step(
            work_unit_id,
            phase.value,
            milestone_key,
            attempt,
            prompt,
        )
        request_id = str(request["request_id"])
        deadline = self.clock() + self.approval_wait_seconds
        while True:
            decision = read_operator_decision_step(request_id)
            if decision["status"] == DecisionRequestStatus.RESOLVED.value:
                # A resolved row whose kind and decision do not pair is a row the
                # write path refuses to create, so `decision_outcome` raising here
                # means the invariant was broken behind its back. Failing loudly
                # is the point; the previous code read such a row as consent.
                return decision_outcome(
                    DecisionRequestKind.APPROVAL,
                    OperatorDecision(str(decision["decision"])),
                    decision.get("payload") or {},
                    decided_by=str(decision.get("decided_by") or "operator"),
                )
            remaining = deadline - self.clock()
            if remaining <= 0:
                return None
            self._wait_for_operator_decision(request_id, remaining)

    def _wait_for_operator_decision(self, request_id: str, timeout_seconds: float) -> None:
        """Sleep until the decision lands, or until the budget runs out.

        Under DBOS this is `recv`, the same channel the dispatch wait uses, and it
        writes nothing while it waits. That is the whole point: the poll it
        replaces called a DBOS step every couple of seconds, so an approval left
        overnight checkpointed tens of thousands of rows and the recorded step
        count depended on how long a human took. A workflow body whose shape
        depends on human latency cannot replay.

        Without DBOS there is no channel to be woken on and polling is the only
        honest answer, so that path is kept rather than dropped. It is bounded by
        whichever is smaller, the poll interval or the remaining budget, so the
        wait never overshoots its own deadline.
        """

        if _dbos_active():
            assert DBOS is not None
            DBOS.recv(
                topic=operator_decision_topic(request_id),
                timeout_seconds=timeout_seconds,
            )
            return
        self.sleeper(min(self.approval_poll_seconds, timeout_seconds))


_engine: WorkUnitEngine | None = None


def get_engine() -> WorkUnitEngine:
    global _engine
    if _engine is None:
        _engine = WorkUnitEngine(runtime=dispatch_backed_runtime())
    return _engine


def set_engine(engine: WorkUnitEngine) -> None:
    """Install the engine the DBOS workflow entrypoints resolve.

    Injection at the boundary rather than inside the workflow bodies: DBOS calls
    module-level functions, so the runtime it should use has to be resolvable from
    module scope.
    """

    global _engine
    _engine = engine


# --------------------------------------------------------------------------- #
# DBOS workflow entrypoints
# --------------------------------------------------------------------------- #


@dbos_workflow()
def execute_work_unit(
    work_unit_id: str,
    design_doc_revision_id: str,
    compiled_plan_revision_id: str,
    compiled_plan_hash: str,
    lifecycle_profile_version: int,
) -> dict[str, Any]:
    """The root workflow. Every identifier is explicit; none of them is "latest"."""

    return get_engine().execute(
        work_unit_id=work_unit_id,
        design_doc_revision_id=design_doc_revision_id,
        compiled_plan_revision_id=compiled_plan_revision_id,
        compiled_plan_hash=compiled_plan_hash,
        lifecycle_profile_version=lifecycle_profile_version,
    )


@dbos_workflow()
def run_phase_workflow(
    work_unit_id: str,
    design_doc_revision_id: str,
    compiled_plan_revision_id: str,
    compiled_plan_hash: str,
    lifecycle_profile_version: int,
    phase: str,
) -> dict[str, Any]:
    return get_engine().run_phase(
        work_unit_id=work_unit_id,
        design_doc_revision_id=design_doc_revision_id,
        compiled_plan_revision_id=compiled_plan_revision_id,
        compiled_plan_hash=compiled_plan_hash,
        lifecycle_profile_version=lifecycle_profile_version,
        phase=LifecyclePhase(phase),
    )


@dbos_workflow()
def execute_milestone_workflow(
    work_unit_id: str,
    design_doc_revision_id: str,
    compiled_plan_revision_id: str,
    compiled_plan_hash: str,
    lifecycle_profile_version: int,
    phase: str,
    milestone_key: str,
    attempt: int,
) -> dict[str, Any]:
    return get_engine().execute_milestone(
        work_unit_id=work_unit_id,
        design_doc_revision_id=design_doc_revision_id,
        compiled_plan_revision_id=compiled_plan_revision_id,
        compiled_plan_hash=compiled_plan_hash,
        lifecycle_profile_version=lifecycle_profile_version,
        phase=LifecyclePhase(phase),
        milestone_key=milestone_key,
        attempt=attempt,
    )


@dataclass(frozen=True)
class InlineRunRefused:
    """An inline run stopped before touching an unlaunched runtime's database.

    A `@dbos_workflow` function does not degrade to a plain call when DBOS is
    configured but not launched: DBOS intercepts the call and reaches for the
    system database, which raises. Observed live on 2026-08-23, when
    `resume_work_unit --inline` from the one-shot CLI answered with
    `DBOSException: System database accessed before DBOS was launched` instead
    of a resume. A delivery outcome is an answer, never a stack trace.
    """

    reason: str


def _drive_root_inline(
    unit: repo.WorkUnitRow, workflow_id: str
) -> dict[str, Any] | InlineRunRefused:
    """Run the root execution in this process, durably when DBOS can launch.

    The inline caller is a one-shot operator process, so launching here is the
    designed lifecycle: `run_workflow_durably` set the launch-on-demand
    pattern, and the CLI's exit boundary (`exit_code_after_runtime_shutdown`)
    stops what this starts. Launched, the direct call runs as a real durable
    workflow under `workflow_id`, blocking until it finishes, which is what
    inline promises. Only when the decorators are the identity ones does the
    call run as plain Python, and then no launch is attempted.
    """

    if _dbos_configured() and not _dbos_active():
        from ..dbos_app import launch_dbos

        launch_dbos()
    if _dbos_active():
        assert SetWorkflowID is not None
        with SetWorkflowID(workflow_id):
            result = execute_work_unit(
                unit.work_unit_id,
                unit.design_doc_revision_id,
                unit.compiled_plan_revision_id,
                unit.compiled_plan_hash,
                unit.lifecycle_profile_version,
            )
        return {"result": result, "durable": True}
    if _dbos_configured():
        return InlineRunRefused(
            reason=(
                "DBOS is configured but its runtime could not be launched, so "
                "the inline run stopped before touching the system database; "
                "check the DBOS system database and retry"
            )
        )
    result = execute_work_unit(
        unit.work_unit_id,
        unit.design_doc_revision_id,
        unit.compiled_plan_revision_id,
        unit.compiled_plan_hash,
        unit.lifecycle_profile_version,
    )
    return {"result": result, "durable": False}


def start_root_workflow(
    work_unit_id: str,
    delivery: EnqueueDelivery = EnqueueDelivery.DURABLE,
) -> dict[str, Any]:
    """Hand the root execution to DBOS under its derived workflow ID.

    Called by the enqueue dispatcher. The workflow ID is the WorkUnit identity, so
    a second delivery of the same outbox row re-enters the same execution instead
    of starting a rival one.

    A ``DURABLE`` request with no active DBOS runtime is not delivered. Saying so is
    the point: the outbox row stays pending until something durable can take it,
    rather than the caller quietly becoming the executor.
    """

    unit = repo.get_work_unit(work_unit_id)
    if _dbos_active():
        assert DBOS is not None and SetWorkflowID is not None
        with SetWorkflowID(unit.root_workflow_id):
            handle = DBOS.start_workflow(
                execute_work_unit,
                unit.work_unit_id,
                unit.design_doc_revision_id,
                unit.compiled_plan_revision_id,
                unit.compiled_plan_hash,
                unit.lifecycle_profile_version,
            )
        return {
            "work_unit_id": work_unit_id,
            "workflow_id": handle.workflow_id,
            "delivered": True,
            "durable": True,
        }
    if delivery is EnqueueDelivery.DURABLE:
        return {
            "work_unit_id": work_unit_id,
            "delivered": False,
            "durable": False,
            "reason": "no active DBOS runtime; the enqueue stays pending",
        }
    driven = _drive_root_inline(unit, unit.root_workflow_id)
    if isinstance(driven, InlineRunRefused):
        return {
            "work_unit_id": work_unit_id,
            "delivered": False,
            "durable": False,
            "reason": driven.reason,
        }
    return {
        "work_unit_id": work_unit_id,
        "result": driven["result"],
        "delivered": True,
        "durable": driven["durable"],
    }


def resume_root_workflow(
    work_unit_id: str,
    delivery: EnqueueDelivery = EnqueueDelivery.DURABLE,
) -> dict[str, Any]:
    """Drive a parked WorkUnit again under a derived continuation ID.

    A WorkUnit still has exactly one root workflow identity. A continuation is not
    a rival execution of it: the ID is derived from that identity plus the halt
    count, so the same resume request coalesces while a genuinely new resume gets
    its own DBOS execution. DBOS refuses to re-run a workflow ID that already
    returned, which is why a continuation of a halted run cannot reuse the root ID.

    The ID comes from `execution_workflow_id` rather than being spelled again
    here, because the recovery pass asks that same function which execution the
    epoch names. Two spellings that agreed only by inspection is how the epoch
    came to name a dead workflow in the first place.

    At epoch zero that function yields the root ID itself, which is correct and
    was previously not what happened: a caller resuming a WorkUnit that had never
    halted got `:resume:0`, a second execution running beside a live root rather
    than coalescing onto it. Reaching this with a *dead* root is what recovery
    exists to prevent, and it advances the epoch before the derivation is made.

    A first-class ``work_unit_executions`` model is the eventual home for this. It
    is deliberately not introduced yet, because the product has one rerun
    requirement (operator resume) and not a general one.
    """

    unit = repo.get_work_unit(work_unit_id)
    epoch = repo.execution_epoch(work_unit_id)
    continuation_id = execution_workflow_id(unit.root_workflow_id, epoch)
    if _dbos_active():
        assert DBOS is not None and SetWorkflowID is not None
        with SetWorkflowID(continuation_id):
            handle = DBOS.start_workflow(
                execute_work_unit,
                unit.work_unit_id,
                unit.design_doc_revision_id,
                unit.compiled_plan_revision_id,
                unit.compiled_plan_hash,
                unit.lifecycle_profile_version,
            )
        return {
            "work_unit_id": work_unit_id,
            "workflow_id": handle.workflow_id,
            "continuation_of": unit.root_workflow_id,
            "delivered": True,
            "durable": True,
        }
    if delivery is EnqueueDelivery.DURABLE:
        return {
            "work_unit_id": work_unit_id,
            "continuation_of": unit.root_workflow_id,
            "delivered": False,
            "durable": False,
            "reason": "no active DBOS runtime; resume again from a durable runtime",
        }
    driven = _drive_root_inline(unit, continuation_id)
    if isinstance(driven, InlineRunRefused):
        return {
            "work_unit_id": work_unit_id,
            "continuation_of": unit.root_workflow_id,
            "delivered": False,
            "durable": False,
            "reason": driven.reason,
        }
    return {
        "work_unit_id": work_unit_id,
        "continuation_of": unit.root_workflow_id,
        "result": driven["result"],
        "delivered": True,
        "durable": driven["durable"],
    }


@dataclass(frozen=True)
class EnqueueFailed:
    """Starting one WorkUnit's root execution raised.

    The case that had no representation. `drain_enqueue_outbox` caught the
    exception, incremented the row's attempt counter, and `continue`d without
    appending anything, so its return value could say "delivered" and "deferred"
    and could not say "raised". Every consumer read that silence as an empty
    queue: the drainer reset its stall counter and reported `Idle`, the CLI
    printed `{"ok": true, "outcomes": []}`, and `start_work_unit` returned a
    success payload with an empty `dispatch`. The only trace of the crash was a
    WorkUnit left RUNNING.
    """

    work_unit_id: str
    failure: FailureV1
    attempts: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "work_unit_id": self.work_unit_id,
            "delivered": False,
            "durable": False,
            "failed": True,
            "attempts": self.attempts,
            "failure": self.failure.to_dict(),
            "reason": self.failure.message,
        }


@dataclass(frozen=True)
class EnqueueSettled:
    """Starting one WorkUnit's root execution returned an answer.

    Delivered or deferred; the payload's own `delivered` flag says which, and
    that distinction already had a representation worth keeping. What it lacked
    was a sibling for the third case.
    """

    work_unit_id: str
    payload: dict[str, Any]

    @property
    def delivered(self) -> bool:
        return bool(self.payload.get("delivered"))

    def to_payload(self) -> dict[str, Any]:
        return dict(self.payload)


# One row's fate. A sum, so a consumer that forgets the failed case is a type
# error rather than a drainer that reports an empty queue.
type EnqueueOutcome = EnqueueSettled | EnqueueFailed


def _deliver_resume_enqueue(work_unit_id: str, delivery: EnqueueDelivery) -> dict[str, Any]:
    """Deliver one RESUME outbox row through the full operator resume path.

    Deliberately the whole of `service.resume_work_unit`, not a bare
    `resume_root_workflow`: crashed-execution recovery, the
    indeterminate-liveness refusal, and the per-milestone retry decisions are
    the resume, and delivering around them would mint a continuation on an
    epoch nothing repaired. The import is deferred because the service imports
    this module at its top; by the time a drain runs, both are fully loaded.

    A WorkUnit that reached a terminal state between the decision and this
    delivery has nothing to resume, and `resume_work_unit` would refuse it with
    an exception every pass, forever. The intent is consumed instead: the
    payload says `delivered` so the drain closes the row, and the reason says
    that nothing ran.
    """

    from . import service

    unit = repo.get_work_unit(work_unit_id)
    if unit.status in TERMINAL_WORK_UNIT_STATUSES:
        return {
            "work_unit_id": work_unit_id,
            "delivered": True,
            "durable": False,
            "reason": (
                f"work unit is {unit.status.value}; the resume intent is obsolete "
                "and was consumed without resuming"
            ),
        }
    return service.resume_work_unit(work_unit_id, delivery=delivery)


def drain_enqueue_outbox(
    limit: int = 20,
    delivery: EnqueueDelivery = EnqueueDelivery.DURABLE,
) -> tuple[EnqueueOutcome, ...]:
    """Deliver pending root-workflow enqueues idempotently.

    Each row names its own delivery: a START row hands the first root execution
    to a runtime, a RESUME row re-drives a halted WorkUnit through the operator
    resume path.

    A row is marked delivered only after the execution was actually accepted, so a
    crash between the two leaves a row that is retried rather than a WorkUnit that
    never starts, and an undeliverable row stays pending rather than being lost.

    A row whose start *raised* is now returned as `EnqueueFailed` rather than
    skipped. Catching it is still right - one bad row must not stop the drain -
    but catching it and returning nothing meant the caller who asked for the run
    got no error at all.
    """

    outcomes: list[EnqueueOutcome] = []
    for row in repo.list_pending_enqueues(limit):
        try:
            if row.kind is repo.EnqueueKind.RESUME:
                payload = _deliver_resume_enqueue(row.work_unit_id, delivery)
            else:
                payload = start_root_workflow(row.work_unit_id, delivery)
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the drain
            failure = exceptional_failure(exc, operation="drain_enqueue_outbox")
            repo.mark_enqueue_failed(row.work_unit_id, failure.message)
            logger.error(
                "work_unit_enqueue_failed",
                extra={
                    **failure.observability_fields(),
                    "detail": (
                        f"starting work unit {row.work_unit_id} raised "
                        f"{failure.exception_type}: {failure.message}"
                    ),
                },
                exc_info=exc,
            )
            outcomes.append(
                EnqueueFailed(
                    work_unit_id=row.work_unit_id,
                    failure=failure,
                    # The row's own counter, read before this attempt was added
                    # to it, so `attempts + 1` is how many have now been spent.
                    attempts=row.attempts + 1,
                )
            )
            continue
        if payload.get("delivered"):
            repo.mark_enqueue_delivered(row.work_unit_id)
        outcomes.append(EnqueueSettled(work_unit_id=row.work_unit_id, payload=payload))
    return tuple(outcomes)


def phase_workflow_id(snapshot: ExecutionSnapshot, phase: LifecyclePhase, epoch: int) -> str:
    """The durable identity of one phase execution, scoped to its epoch.

    The epoch is what the root continuation and the milestone attempt already
    carry, and the phase level was the only one without it. That mattered because
    `run_phase` returns normally when a milestone parks: the phase workflow
    completes, its ID is consumed, and an operator resume re-enters the same ID.
    DBOS does not re-run a workflow that already returned, so the resume either
    replayed the stale "still parked" result and silently did nothing, or was
    refused outright.

    Within one epoch the ID is stable, which is what lets a crash mid-phase
    resume the same execution instead of starting a rival one.
    """

    return f"{snapshot.root_workflow_id}:phase:{phase.value}:epoch:{epoch}"


def fresh_child_workflow_id(prefix: str) -> str:
    """A unique child ID for work with no natural identity, used sparingly."""

    return f"{prefix}:{uuid.uuid4().hex[:12]}"


__all__ = [
    "SCHEMA_VERSION_WORK_UNIT_EXECUTION",
    "EnqueueDelivery",
    "ExecutionInputMismatch",
    "ExecutionSnapshot",
    "WorkUnitEngine",
    "drain_enqueue_outbox",
    "execute_milestone_workflow",
    "execute_work_unit",
    "get_engine",
    "resume_root_workflow",
    "run_phase_workflow",
    "set_engine",
    "start_root_workflow",
]
