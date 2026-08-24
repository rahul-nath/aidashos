# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The refinery's queue as durable rows.

Invariant 11 of `docs/completed/refinery_integration_queue_design.md`: every queue state
transition is a durable row, and the queue is never reconstructed by inspecting
git. This module is the whole of that. `refinery/` decides; this writes down
what was decided and reads it back.

The row is the immutable subject in columns plus the state's own fields in one
JSON payload behind a discriminator, which is the sum in `refinery/requests.py`
serialized without flattening it. `state_of` is what picks the column and
`_decode_state` is what reads it back, so the two cannot disagree about which
variant a row is.

Enqueue is bound to approval resolution and not to submission, and it is bound
inside the same transaction. An approval that flipped to APPROVED without its
queue row would be a branch a human agreed to land that the refinery will never
see, which is the two-halves-that-never-meet defect this design was written
against, arriving through a crash instead of through a missing call.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, assert_never

from ..project_center import LinkedProject, load_project_center
from ..refinery.enqueue import (
    EnqueueAdmission,
    EnqueueAdmitted,
    EnqueueRefused,
    GitRepositoryProbe,
    ProjectRegistry,
    RepositoryProbe,
    admit_to_queue,
)
from ..refinery.requests import (
    BisectCause,
    BisectedOut,
    GateFailed,
    InFlight,
    Integrated,
    IntegrationAttemptId,
    IntegrationBatchId,
    IntegrationRequest,
    IntegrationRequestId,
    IntegrationRequestState,
    IntegrationSubject,
    MergeConflict,
    Queued,
    WithdrawalReason,
    Withdrawn,
    require_integration_transition,
    state_of,
)
from .outcomes import DispatchPromotionState, require_dispatch_promotion_transition
from .store import (
    ConnectionLike,
    connect,
    decode_json_array,
    err,
    iso,
    ok,
    rowdict,
    sql_status_list,
)

_LIVE_STATES = (IntegrationRequestState.QUEUED, IntegrationRequestState.IN_FLIGHT)
"""The states the partial unique index covers, named once for both readers."""


@dataclass(frozen=True)
class IntegrationEnqueued:
    """A new row. This resolution is what put the commit in the queue."""

    request: Queued


@dataclass(frozen=True)
class AlreadyQueued:
    """A live request already holds this commit, so nothing was written.

    Not a refusal. It is what idempotency looks like from the inside: a replayed
    resolution finds its own earlier work and returns it, and a caller that
    cannot tell the two apart still gets the request it asked for.
    """

    request: IntegrationRequest


type EnqueueOutcome = IntegrationEnqueued | AlreadyQueued


def admit_code_merge_approval(
    approval_id: str,
    payload: Mapping[str, object],
    *,
    enqueued_at: float,
    registry: ProjectRegistry | None = None,
    probe_for: Callable[[LinkedProject], RepositoryProbe] = GitRepositoryProbe.for_project,
) -> EnqueueAdmission:
    """Decide, outside any transaction, whether this approval may join the queue.

    Outside on purpose. The decision reads a config file and spawns up to two git
    subprocesses, and a transaction held open across a subprocess is a pool
    connection held for however long that subprocess decides to take. This
    process has been burned by connections outliving their work before; the
    transaction that follows is pure SQL and short.
    """

    return admit_to_queue(
        payload,
        approval_id=approval_id,
        request_id=IntegrationRequestId(str(uuid.uuid4())),
        enqueued_at=enqueued_at,
        registry=registry if registry is not None else load_project_center(),
        probe_for=probe_for,
    )


def record_queued_request(
    c: ConnectionLike,
    request: Queued,
    *,
    recorded_at: float,
) -> EnqueueOutcome:
    """Write the queued request, or return the live one that already holds its commit.

    `ON CONFLICT DO NOTHING` against the partial unique index rather than a
    select-then-insert. The check and the insert would be two statements with a
    gap between them, and the gap is exactly where a replay and a first
    resolution both find nothing and both write.
    """

    subject = request.subject
    inserted = c.execute(
        """
        INSERT INTO integration_requests(
            request_id, target_project_id, branch_name, base_head_sha, commit_sha,
            approval_id, intent_id, pow_wow_id, milestone_key, changed_files_json,
            enqueued_at, state, state_payload_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        (
            subject.request_id,
            subject.target_project_id,
            subject.branch_name,
            subject.base_head_sha,
            subject.commit_sha,
            subject.approval_id,
            subject.intent_id,
            subject.pow_wow_id,
            subject.milestone_key,
            json.dumps(list(subject.changed_files)),
            subject.enqueued_at,
            IntegrationRequestState.QUEUED.value,
            json.dumps(_encode_state(request)),
            recorded_at,
        ),
    )
    if inserted.rowcount == 1:
        return IntegrationEnqueued(request)
    existing = c.execute(
        f"""
        SELECT * FROM integration_requests
        WHERE target_project_id = ? AND commit_sha = ?
          AND state IN ({sql_status_list(*_LIVE_STATES)})
        """,
        (subject.target_project_id, subject.commit_sha),
    ).fetchone()
    if existing is None:
        # The insert wrote nothing and no live row holds the commit, which the
        # only conflict target on this table cannot produce. Crash rather than
        # report a queue position that does not exist.
        raise RuntimeError(
            f"integration request for {subject.commit_sha} in {subject.target_project_id} "
            "was neither inserted nor found; the queue's uniqueness constraint and this "
            "read disagree"
        )
    return AlreadyQueued(integration_request_from_row(rowdict(existing)))


def apply_integration_transition(
    c: ConnectionLike,
    current: IntegrationRequest,
    target: IntegrationRequest,
    *,
    recorded_at: float,
) -> None:
    """Move one request to its next state, refusing the moves that are not moves.

    Every write to a live row goes through here rather than through bespoke SQL
    per transition, because `require_integration_transition` is what makes a
    terminal state terminal and a subject immutable, and a second write path
    would be a second chance to skip it. The check runs before the UPDATE, so a
    refused transition writes nothing.

    The UPDATE is guarded on the state the caller read. Only one refinery holds a
    project at a time, so a mismatch is not contention; it is this process having
    acted on a row that changed underneath it, which under an advisory lock means
    the lock was not held. Crashing beats writing.
    """

    require_integration_transition(current, target)
    subject = target.subject
    updated = c.execute(
        """
        UPDATE integration_requests
           SET state = ?, state_payload_json = ?, updated_at = ?
         WHERE request_id = ? AND state = ?
        """,
        (
            state_of(target).value,
            json.dumps(_encode_state(target)),
            recorded_at,
            subject.request_id,
            state_of(current).value,
        ),
    )
    if updated.rowcount != 1:
        raise RuntimeError(
            f"integration request {subject.request_id} was not {state_of(current)} when this "
            f"refinery tried to make it {state_of(target)}; the per-project advisory lock is "
            "the only thing that makes that impossible, so it was not held"
        )


def claim_requests_for_attempt(
    c: ConnectionLike,
    requests: Sequence[Queued],
    *,
    batch_id: IntegrationBatchId,
    attempt_id: IntegrationAttemptId,
    recorded_at: float,
) -> tuple[InFlight, ...]:
    """Take a batch out of the queue and into one attempt.

    Written before any git happens, so a refinery that dies mid-batch is
    recoverable rather than ambiguous: `recover_in_flight_requests` returns every
    `InFlight` row for the project to `Queued` on restart. That recovery is only
    safe because nothing advances the integrated branch unless a gate went green,
    which makes an unfinished attempt redoable from scratch.
    """

    claimed: list[InFlight] = []
    for request in requests:
        in_flight = InFlight(subject=request.subject, batch_id=batch_id, attempt_id=attempt_id)
        apply_integration_transition(c, request, in_flight, recorded_at=recorded_at)
        claimed.append(in_flight)
    return tuple(claimed)


def return_requests_to_queue(
    c: ConnectionLike,
    requests: Sequence[InFlight],
    *,
    recorded_at: float,
) -> int:
    """Undo a claim for requests an abandoned run judged nobody on.

    A dirty working tree, a checkout on the wrong branch, or a worktree that
    could not be allocated are statements about the operator's machine, not about
    anyone's diff. The design says so explicitly and this is where it is true:
    the rows go back to `Queued` and are re-attempted from whatever tip exists
    next time.
    """

    for request in requests:
        apply_integration_transition(
            c,
            request,
            Queued(subject=request.subject),
            recorded_at=recorded_at,
        )
    return len(requests)


def record_bisected_out(
    c: ConnectionLike,
    request: InFlight,
    *,
    cause: BisectCause,
    stack_beneath: Sequence[IntegrationRequestId],
    stack_base_sha: str,
    evidence_artifact_id: str,
    recorded_at: float,
) -> BisectedOut:
    """Park one request, keeping everything needed to act on it later.

    `BisectedOut` is terminal and it is not a failure of the milestone that
    produced it. The branch, the commit, the base, the cause, and the combination
    it lost against all survive here, because the remedy an operator is offered
    is a bounded revision against the new base and that is not composable from a
    row that only says "did not land".
    """

    parked = BisectedOut(
        subject=request.subject,
        batch_id=request.batch_id,
        cause=cause,
        stack_beneath=tuple(stack_beneath),
        stack_base_sha=stack_base_sha,
        evidence_artifact_id=evidence_artifact_id,
        bisected_at=recorded_at,
    )
    apply_integration_transition(c, request, parked, recorded_at=recorded_at)
    return parked


def record_integrated(
    c: ConnectionLike,
    request: InFlight,
    *,
    integration_commit_sha: str,
    recorded_at: float,
) -> Integrated:
    """Persist the one fact that represents ``MERGE_APPROVED -> MERGED``."""

    require_dispatch_promotion_transition(
        DispatchPromotionState.MERGE_APPROVED,
        DispatchPromotionState.MERGED,
    )
    integrated = Integrated(
        subject=request.subject,
        batch_id=request.batch_id,
        integration_commit_sha=integration_commit_sha,
        integrated_at=recorded_at,
    )
    apply_integration_transition(c, request, integrated, recorded_at=recorded_at)
    return integrated


def recover_in_flight_requests(
    c: ConnectionLike,
    *,
    target_project_id: str,
    recorded_at: float,
) -> tuple[IntegrationRequestId, ...]:
    """Return every outstanding attempt for one project to the queue.

    Run while holding the project's refinery lock and before selection, because
    `select_next_batch` crashes on an `InFlight` row rather than skipping it: a
    second stack built on a base the first refinery was about to invalidate would
    silently lose one of the two batches.

    Nothing here inspects git. An `InFlight` row means an attempt started, and
    the invariants make an unfinished attempt redoable whatever state its
    worktree was left in, so the row is the whole question.
    """

    outstanding = [
        request
        for request in read_integration_requests(c, target_project_id=target_project_id)
        if isinstance(request, InFlight)
    ]
    return_requests_to_queue(c, outstanding, recorded_at=recorded_at)
    return tuple(request.subject.request_id for request in outstanding)


def read_integration_requests(
    c: ConnectionLike,
    *,
    target_project_id: str | None = None,
    approval_id: str | None = None,
) -> tuple[IntegrationRequest, ...]:
    """Every request matching the filter, in queue order.

    Ordered here rather than by the caller because `(enqueued_at, request_id)` is
    the total order the bisect rule preserves through every split, and a reader
    that sorted differently would hand `select_next_batch` a queue that was never
    FIFO. `SelectedBatch` refuses one, but refusing late is worse than not
    producing one.
    """

    clauses: list[str] = []
    params: list[Any] = []
    if target_project_id is not None:
        clauses.append("target_project_id = ?")
        params.append(target_project_id)
    if approval_id is not None:
        clauses.append("approval_id = ?")
        params.append(approval_id)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = c.execute(
        f"SELECT * FROM integration_requests {where} ORDER BY enqueued_at, request_id",
        params,
    ).fetchall()
    return tuple(integration_request_from_row(rowdict(row)) for row in rows)


def integration_request_from_row(row: Mapping[str, Any]) -> IntegrationRequest:
    """Rebuild the sum from one row, or refuse to guess."""

    subject = IntegrationSubject(
        request_id=IntegrationRequestId(str(row["request_id"])),
        target_project_id=str(row["target_project_id"]),
        branch_name=str(row["branch_name"]),
        base_head_sha=str(row["base_head_sha"]),
        commit_sha=str(row["commit_sha"]),
        approval_id=str(row["approval_id"]),
        intent_id=str(row["intent_id"]),
        pow_wow_id=str(row["pow_wow_id"]),
        milestone_key=(str(row["milestone_key"]) if row["milestone_key"] is not None else None),
        changed_files=tuple(str(item) for item in decode_json_array(row["changed_files_json"])),
        enqueued_at=float(row["enqueued_at"]),
    )
    state = IntegrationRequestState(str(row["state"]))
    payload = json.loads(str(row["state_payload_json"]))
    return _decode_state(state, subject, payload)


def _encode_state(request: IntegrationRequest) -> dict[str, Any]:
    """The variant's own fields, and only those. The subject is already columns."""

    match request:
        case Queued():
            return {}
        case InFlight():
            return {"batch_id": request.batch_id, "attempt_id": request.attempt_id}
        case Integrated():
            return {
                "batch_id": request.batch_id,
                "integration_commit_sha": request.integration_commit_sha,
                "integrated_at": request.integrated_at,
            }
        case BisectedOut():
            return {
                "batch_id": request.batch_id,
                "cause": _encode_cause(request.cause),
                "stack_beneath": list(request.stack_beneath),
                "stack_base_sha": request.stack_base_sha,
                "evidence_artifact_id": request.evidence_artifact_id,
                "bisected_at": request.bisected_at,
            }
        case Withdrawn():
            return {"reason": request.reason.value, "withdrawn_at": request.withdrawn_at}
    assert_never(request)


def _decode_state(
    state: IntegrationRequestState,
    subject: IntegrationSubject,
    payload: Mapping[str, Any],
) -> IntegrationRequest:
    match state:
        case IntegrationRequestState.QUEUED:
            return Queued(subject=subject)
        case IntegrationRequestState.IN_FLIGHT:
            return InFlight(
                subject=subject,
                batch_id=IntegrationBatchId(str(payload["batch_id"])),
                attempt_id=IntegrationAttemptId(str(payload["attempt_id"])),
            )
        case IntegrationRequestState.INTEGRATED:
            return Integrated(
                subject=subject,
                batch_id=IntegrationBatchId(str(payload["batch_id"])),
                integration_commit_sha=str(payload["integration_commit_sha"]),
                integrated_at=float(payload["integrated_at"]),
            )
        case IntegrationRequestState.BISECTED_OUT:
            return BisectedOut(
                subject=subject,
                batch_id=IntegrationBatchId(str(payload["batch_id"])),
                cause=_decode_cause(payload["cause"]),
                stack_beneath=tuple(
                    IntegrationRequestId(str(item)) for item in payload["stack_beneath"]
                ),
                stack_base_sha=str(payload["stack_base_sha"]),
                evidence_artifact_id=str(payload["evidence_artifact_id"]),
                bisected_at=float(payload["bisected_at"]),
            )
        case IntegrationRequestState.WITHDRAWN:
            return Withdrawn(
                subject=subject,
                reason=WithdrawalReason(str(payload["reason"])),
                withdrawn_at=float(payload["withdrawn_at"]),
            )
    assert_never(state)


_MERGE_CONFLICT = "MERGE_CONFLICT"
_GATE_FAILED = "GATE_FAILED"


def _encode_cause(cause: BisectCause) -> dict[str, Any]:
    match cause:
        case MergeConflict():
            return {"kind": _MERGE_CONFLICT, "conflicted_paths": list(cause.conflicted_paths)}
        case GateFailed():
            return {
                "kind": _GATE_FAILED,
                "command": cause.command,
                "exit_code": cause.exit_code,
                "output_excerpt": cause.output_excerpt,
            }
    assert_never(cause)


def _decode_cause(payload: Mapping[str, Any]) -> BisectCause:
    kind = str(payload["kind"])
    if kind == _MERGE_CONFLICT:
        return MergeConflict(
            conflicted_paths=tuple(str(item) for item in payload["conflicted_paths"])
        )
    if kind == _GATE_FAILED:
        return GateFailed(
            command=str(payload["command"]),
            exit_code=int(payload["exit_code"]),
            output_excerpt=str(payload["output_excerpt"]),
        )
    raise ValueError(f"unknown bisect cause kind {kind!r} in a durable integration request")


def list_integration_requests(
    target_project_id: str | None = None,
    state: str | None = None,
) -> dict[str, Any]:
    """The queue, as an operator sees it.

    Read-only, and here from the queue's first milestone rather than from the one
    that drains it. A durable table only test code can read is the exact shape
    this repository has already paid for: an operator who resolves a merge is
    handed a request id, and this is how they find out what became of it.
    """

    if state is not None:
        try:
            state = IntegrationRequestState(state).value
        except ValueError:
            return err(
                "invalid_state",
                state=state,
                valid=[member.value for member in IntegrationRequestState],
            )
    with connect() as connection:
        requests = read_integration_requests(connection, target_project_id=target_project_id)
    return ok(
        requests=[
            describe_integration_request(request)
            for request in requests
            if state is None or state_of(request).value == state
        ]
    )


def describe_integration_request(request: IntegrationRequest) -> dict[str, Any]:
    """One request flattened for a reader, with the variant's fields under `state`.

    Flattened rather than nested for the subject, because every request has one
    and a reader scanning a list wants the branch and the sha at the top level.
    The variant stays behind its discriminator for the opposite reason: a
    `cause` at the top level would read as a field every request has.
    """

    subject = request.subject
    return {
        "request_id": subject.request_id,
        "state": state_of(request).value,
        "target_project_id": subject.target_project_id,
        "branch_name": subject.branch_name,
        "base_head_sha": subject.base_head_sha,
        "commit_sha": subject.commit_sha,
        "approval_id": subject.approval_id,
        "intent_id": subject.intent_id,
        "pow_wow_id": subject.pow_wow_id,
        "milestone_key": subject.milestone_key,
        "changed_files": list(subject.changed_files),
        "enqueued_at": iso(subject.enqueued_at),
        "state_detail": _encode_state(request),
    }


def enqueue_summary(outcome: EnqueueOutcome) -> dict[str, Any]:
    """What an approval resolution tells its caller about the queue.

    Both members answer with the same three fields, and `already_queued` is what
    distinguishes them, because a caller reporting to an operator wants to say
    "queued as X" or "already queued as X" and not to match on a type to find
    out which.
    """

    request = outcome.request
    return {
        "integration_request_id": request.subject.request_id,
        "integration_state": state_of(request).value,
        "already_queued": isinstance(outcome, AlreadyQueued),
    }


__all__ = [
    "AlreadyQueued",
    "EnqueueAdmission",
    "EnqueueAdmitted",
    "EnqueueOutcome",
    "EnqueueRefused",
    "IntegrationEnqueued",
    "admit_code_merge_approval",
    "apply_integration_transition",
    "claim_requests_for_attempt",
    "describe_integration_request",
    "enqueue_summary",
    "integration_request_from_row",
    "list_integration_requests",
    "read_integration_requests",
    "record_bisected_out",
    "record_integrated",
    "record_queued_request",
    "recover_in_flight_requests",
    "return_requests_to_queue",
]
