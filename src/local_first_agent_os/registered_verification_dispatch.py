# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Run the registered gate selected by a durable milestone, without an agent."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import assert_never

from . import host_verification as host
from .contracts import DispatchIntentStatus, LeaseStatus
from .coordination.contracts import (
    DispatchKind,
    DispatchTerminalStatus,
    ExecutionLeaseTerminalStatus,
)
from .coordination.execution import complete_execution_lease, open_execution_lease
from .coordination.outcomes import DispatchPromotionState, DispatchResultOrigin, DispatchResultState
from .coordination.store import rowdict, tx
from .dispatch_payloads import (
    AutomatedRunObservation,
    DispatchReportEnvelope,
    InterruptedRunObservation,
    UnverifiedArtifactObservation,
)
from .dispatcher import (
    DispatchDeferralReason,
    IntentDeferred,
    IntentResult,
    TerminalIntentResult,
)
from .execution_admission import (
    AuthorizedExecution,
    ExecutionAdmissionError,
    ExecutionAdmissionRefusal,
    ExecutionContract,
    ExecutionDriver,
    admit_execution,
    require_authorized_execution,
)
from .project_center import load_project_center
from .spawn_authority import SpawnAuthority
from .work_units import repository as work_units
from .work_units.executors import (
    ExecutorKind,
    execution_driver_for,
)

# Receipt persistence follows process termination, within the same owned lease.
RECEIPT_PERSISTENCE_GRACE_SECONDS = 60


@dataclass(frozen=True)
class RegisteredGateDispatch:
    subject: host.VerificationSubject
    source_repository: Path
    source_commit: str
    commands: tuple[str, ...]
    timeout_seconds: int
    claim_identity: str
    permission_digest: str
    authorization: AuthorizedExecution


def registered_gate_dispatch(intent_id: str) -> RegisteredGateDispatch | None:
    """Select only by the immutable compiled executor and current durable link."""

    with tx() as connection:
        rows = connection.execute(
            "SELECT d.status, d.claimed_by, d.claimed_at, d.target_project_id, "
            "d.base_commit_sha, d.permitted_capabilities, cm.executor_kind, cm.stable_key, "
            "m.work_unit_id, m.attempt, w.compiled_plan_revision_id, w.compiled_plan_hash "
            "FROM dispatch_intents d "
            "JOIN milestone_executions m ON m.dispatch_intent_id=d.intent_id "
            "JOIN compiled_milestones cm ON cm.milestone_id=m.milestone_id "
            "JOIN work_units w ON w.work_unit_id=m.work_unit_id WHERE d.intent_id=?",
            (intent_id,),
        ).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError("dispatch must identify exactly one current milestone")
    row = rowdict(rows[0])
    if execution_driver_for(ExecutorKind(row["executor_kind"])) is not (
        ExecutionDriver.REGISTERED_VERIFICATION
    ):
        return None
    if (
        DispatchIntentStatus(row["status"]) is not DispatchIntentStatus.CLAIMED
        or not row["claimed_by"]
        or row["claimed_at"] is None
    ):
        raise ValueError("registered verification requires a durable dispatch claim")
    plan = work_units.get_compiled_plan_revision(row["compiled_plan_revision_id"]).plan
    milestone = plan.milestone(row["stable_key"])
    if plan.plan_hash() != row["compiled_plan_hash"]:
        raise ValueError("registered verification plan identity does not match the WorkUnit")
    if plan.target_project_id != row["target_project_id"]:
        raise ValueError("registered verification project does not match its plan")
    capabilities = json.loads(row["permitted_capabilities"])
    if not isinstance(capabilities, list) or not all(
        isinstance(item, str) for item in capabilities
    ):
        raise ValueError("registered verification capabilities must be declared names")
    authority = SpawnAuthority.from_names(capabilities)
    authorization = admit_execution(
        ExecutionContract(ExecutionDriver.REGISTERED_VERIFICATION), authority
    )
    if isinstance(authorization, ExecutionAdmissionRefusal):
        raise ExecutionAdmissionError(authorization)
    if authority != SpawnAuthority.from_names(milestone.tool_policy.permitted_tools):
        raise ValueError("registered verification dispatch authority differs from its plan")
    source_commit = row["base_commit_sha"]
    if not isinstance(source_commit, str) or not source_commit:
        raise ValueError("registered verification requires an exact dependency source commit")
    project = load_project_center().project_by_id(row["target_project_id"])
    if not project.verification_commands:
        raise ValueError("project has no registered verification commands")
    subject = host.VerificationSubject(
        intent_id=intent_id,
        target_project_id=row["target_project_id"],
        work_unit_id=row["work_unit_id"],
        milestone_key=row["stable_key"],
        attempt=row["attempt"],
        compiled_plan_hash=row["compiled_plan_hash"],
    )
    permission = json.dumps(
        {"subject": subject.model_dump(), "capabilities": authority.to_names()}, sort_keys=True
    )
    return RegisteredGateDispatch(
        subject=subject,
        source_repository=project.expanded_path,
        source_commit=source_commit,
        commands=tuple(project.verification_commands),
        timeout_seconds=milestone.timeout_seconds,
        claim_identity=f"{row['claimed_by']}:{row['claimed_at']}",
        permission_digest=hashlib.sha256(permission.encode()).hexdigest(),
        authorization=authorization,
    )


def _report(
    request: RegisteredGateDispatch, outcome: host.VerificationProcessOutcome
) -> TerminalIntentResult:
    match outcome:
        case host.VerificationUnavailable(reason=reason):
            observation = InterruptedRunObservation(
                status="UNAVAILABLE",
                executor=ExecutionDriver.REGISTERED_VERIFICATION.value,
                target_project_id=request.subject.target_project_id,
                output_summary=reason,
                external_agents_started=False,
                auto_merge=False,
            )
            state = DispatchResultState.FAILED
            terminal = DispatchTerminalStatus.FAILED
            error: str | None = reason
        case host.VerificationCancelled():
            observation = InterruptedRunObservation(
                status="CANCELED",
                executor=ExecutionDriver.REGISTERED_VERIFICATION.value,
                target_project_id=request.subject.target_project_id,
                output_summary="registered verification was cancelled",
                verification_commands=request.commands,
                verification_output=tuple(c.stdout + c.stderr for c in outcome.captures),
                external_agents_started=False,
                auto_merge=False,
                artifacts=(
                    UnverifiedArtifactObservation(
                        artifact_type=host.REFERENCE_KIND,
                        schema_version="host_verification_receipt_reference.v1",
                        content={"receipt_id": outcome.receipt_id},
                    ),
                ),
            )
            state = DispatchResultState.FAILED
            terminal = DispatchTerminalStatus.FAILED
            error = "registered verification was cancelled"
        case host.VerificationPassed() | host.VerificationFailed():
            passed = isinstance(outcome, host.VerificationPassed)
            observation = AutomatedRunObservation(
                status="COMPLETED" if passed else "VERIFICATION_FAILED",
                executor=ExecutionDriver.REGISTERED_VERIFICATION.value,
                target_project_id=request.subject.target_project_id,
                output_summary="registered verification passed"
                if passed
                else "registered verification failed",
                verification_commands=request.commands,
                verification_output=tuple(c.stdout + c.stderr for c in outcome.captures),
                external_agents_started=False,
                auto_merge=False,
                artifacts=(
                    UnverifiedArtifactObservation(
                        artifact_type=host.REFERENCE_KIND,
                        schema_version="host_verification_receipt_reference.v1",
                        content={"receipt_id": outcome.receipt_id},
                    ),
                    UnverifiedArtifactObservation(
                        artifact_type="worktree_commit_checkpoint",
                        schema_version="worktree_commit_checkpoint.v1",
                        content={
                            "commit_sha": request.source_commit,
                            "base_head_sha": request.source_commit,
                        },
                    ),
                ),
            )
            state = DispatchResultState.COMPLETED if passed else DispatchResultState.FAILED
            terminal = DispatchTerminalStatus.DONE if passed else DispatchTerminalStatus.FAILED
            error = None if passed else "registered verification did not pass"
        case unreachable:
            assert_never(unreachable)
    report = DispatchReportEnvelope(
        schema_version="dispatch_runner_result.v1",
        intent_id=request.subject.intent_id,
        target_project_id=request.subject.target_project_id,
        kind=DispatchKind.CODE,
        result_origin=DispatchResultOrigin.AUTOMATED,
        result_state=state,
        promotion_state=DispatchPromotionState.RESULT_RECORDED,
        run_result=observation,
    )
    return terminal, report.model_dump_json(exclude_none=True), error


def _retained_outcome(request: RegisteredGateDispatch, lease_id: str) -> IntentResult:
    """Recover a retained result without inventing another execution attempt."""

    deferred = IntentDeferred(
        request.subject.intent_id, lease_id, DispatchDeferralReason.RETAINED_OUTCOME_UNAVAILABLE
    )
    with tx() as connection:
        row = connection.execute(
            "SELECT * FROM agent_execution_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
    if row is None:
        return deferred
    lease = rowdict(row)
    try:
        commands = json.loads(lease["command_json"])
        result = json.loads(lease["result_json"] or "{}")
        status = LeaseStatus(lease["status"])
    except (ValueError, TypeError):
        return deferred
    if (
        lease["intent_id"] != request.subject.intent_id
        or lease["target_project_id"] != request.subject.target_project_id
        or lease["source_revision"] != request.source_commit
        or lease["permission_envelope_sha256"] != request.permission_digest
        or lease["task_role"] != ExecutionDriver.REGISTERED_VERIFICATION.value
        or commands != list(request.commands)
    ):
        return deferred
    if status in {LeaseStatus.ACTIVE, LeaseStatus.CANCEL_REQUESTED}:
        return IntentDeferred(
            request.subject.intent_id, lease_id, DispatchDeferralReason.EXECUTION_ACTIVE
        )
    if not isinstance(result, Mapping):
        return deferred
    receipt_id = result.get("receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id:
        if "resource_cleanup" in result:
            try:
                host.PendingVerificationResource.model_validate(result["resource_cleanup"])
            except ValueError:
                return deferred
            return IntentDeferred(
                request.subject.intent_id,
                lease_id,
                DispatchDeferralReason.RESOURCE_CLEANUP_PENDING,
            )
        return deferred
    outcome = host.resolve_retained_verification_outcome(
        receipt_id, request.subject, lease_id, request.source_commit
    )
    if isinstance(outcome, host.VerificationCleanupPending):
        return IntentDeferred(
            request.subject.intent_id, lease_id, DispatchDeferralReason.RESOURCE_CLEANUP_PENDING
        )
    if isinstance(outcome, host.VerificationUnavailable):
        return deferred
    expected_status = (
        LeaseStatus.COMPLETED
        if isinstance(outcome, host.VerificationPassed)
        else LeaseStatus.CANCELED
        if isinstance(outcome, host.VerificationCancelled)
        else LeaseStatus.FAILED
    )
    if status is not expected_status:
        return deferred
    return _report(request, outcome)


def run_registered_gate_dispatch(request: RegisteredGateDispatch) -> IntentResult:
    """A claim launches once; reentry preserves its active or retained execution."""

    require_authorized_execution(request.authorization, ExecutionDriver.REGISTERED_VERIFICATION)
    worker_id = f"registered-verifier:{uuid.uuid4()}"
    idempotency_key = f"registered-verifier:{request.subject.intent_id}:{request.claim_identity}"
    # The lease API's lookup and insertion use separate SQL statements. Serialize
    # this driver's entrants before calling that owner, rather than race a second
    # insert and turn a uniqueness exception into a false terminal failure.
    with tx() as connection:
        connection.execute(
            "SELECT pg_advisory_xact_lock(?)",
            (int(hashlib.sha256(idempotency_key.encode()).hexdigest()[:15], 16),),
        )
        try:
            current = registered_gate_dispatch(request.subject.intent_id)
        except ValueError:
            current = None
        if (
            current is None
            or not 0 < request.timeout_seconds <= current.timeout_seconds
            or replace(current, timeout_seconds=request.timeout_seconds) != request
        ):
            return IntentDeferred(
                request.subject.intent_id, None, DispatchDeferralReason.CLAIM_CHANGED
            )
        opened = open_execution_lease(
            idempotency_key,
            worker_id,
            intent_id=request.subject.intent_id,
            target_project_id=request.subject.target_project_id,
            task_role=ExecutionDriver.REGISTERED_VERIFICATION.value,
            source_revision=request.source_commit,
            permission_envelope_sha256=request.permission_digest,
            command_json=json.dumps(request.commands),
            timeout_seconds=request.timeout_seconds + RECEIPT_PERSISTENCE_GRACE_SECONDS,
        )
    if not opened.get("ok"):
        raise RuntimeError("registered verification could not open its execution lease")
    if not opened["created"]:
        return _retained_outcome(request, str(opened["lease"]["lease_id"]))
    lease_id = str(opened["lease"]["lease_id"])
    outcome: host.VerificationOutcome | None = None
    try:
        outcome = host.run_registered_verification(
            intent_id=request.subject.intent_id,
            lease_id=lease_id,
            worker_id=worker_id,
            source_repository=request.source_repository,
            source_commit=request.source_commit,
            base_commit=request.source_commit,
            timeout_seconds=request.timeout_seconds,
        )
    finally:
        process_outcome = (
            outcome.verification
            if isinstance(outcome, host.VerificationCleanupPending)
            else outcome
        )
        status = (
            ExecutionLeaseTerminalStatus.COMPLETED
            if isinstance(process_outcome, host.VerificationPassed)
            else ExecutionLeaseTerminalStatus.CANCELED
            if isinstance(process_outcome, host.VerificationCancelled)
            else ExecutionLeaseTerminalStatus.FAILED
        )
        retained_result: dict[str, object] = (
            {"receipt_id": process_outcome.receipt_id}
            if isinstance(
                process_outcome,
                host.VerificationPassed | host.VerificationFailed | host.VerificationCancelled,
            )
            else {"verification_unavailable": True}
        )
        if isinstance(outcome, host.VerificationCleanupPending):
            retained_result["resource_cleanup"] = outcome.resource.model_dump(mode="json")
        closed = complete_execution_lease(
            lease_id,
            status,
            result_json=json.dumps(retained_result),
            error=(
                process_outcome.reason
                if isinstance(process_outcome, host.VerificationUnavailable)
                else None
                if isinstance(process_outcome, host.VerificationPassed)
                else "registered verification did not pass"
            ),
        )
        if not closed.get("ok"):
            raise RuntimeError("registered verification lease completion was not recorded")
    assert outcome is not None, "registered verifier returned no typed outcome"
    if isinstance(outcome, host.VerificationCleanupPending):
        return IntentDeferred(
            request.subject.intent_id, lease_id, DispatchDeferralReason.RESOURCE_CLEANUP_PENDING
        )
    return _report(request, outcome)
