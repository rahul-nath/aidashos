# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What an approved agent branch is while it waits to land, at each point in its life.

`_run_batch` runs independent milestones concurrently and each dispatched code
intent allocates its worktree from the target project's current ``HEAD``, so N
milestones produce N branches from one base with no integration order and no
combination ever tested. Nothing performs
``DispatchPromotionState.MERGE_APPROVED -> MERGED``; integration today is a
``git merge --ff-only`` string that `workflow/engine.py` prints for a human. This
module owns the datatype that queue has to be made of.

The shape is one immutable subject plus a sum over the five things a request can
be, rather than one row with nullable outcome columns. A landed request has an
integration commit and no cause, a bisected one has a cause and no integration
commit, and a queued one has neither and no batch either. Those fields are not
optional versions of each other, and a single record carrying all of them would
make "queued" and "lost" indistinguishable to a reader holding a null batch id.

The nearest existing row was rejected deliberately. ``approval_requests`` already
carries ``branch``, ``base_sha``, and ``commit_sha``, but an approval is a
statement by a person about one diff while a queue entry is a statement by the
system about ordering and outcome, and ``_APPROVAL_STATUS_TRANSITIONS`` is a
closed ``PENDING -> {APPROVED, DENIED}`` / ``APPROVED -> {REVOKED}`` shared with
PURCHASE, EXTERNAL_COMMS, MODEL_ESCALATION, and GENERAL approvals, with nowhere
to put "bisected out" and no way to add one without changing what those mean.

One deviation from the design doc: ``request_id`` lives on `IntegrationSubject`
rather than on each state. The queue's total order is ``(enqueued_at,
request_id)`` and every state needs the identity, so putting it on the subject
gives it one home that no transition can drop, and repeating it across five
variants would give five chances to disagree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import NewType, assert_never

IntegrationRequestId = NewType("IntegrationRequestId", str)
"""Identity of one request to land one commit. Equality and generation only."""

IntegrationBatchId = NewType("IntegrationBatchId", str)
"""Identity of one refinery run over one project's queue."""

IntegrationAttemptId = NewType("IntegrationAttemptId", str)
"""Identity of one stack built inside one batch.

A batch that bisects builds several stacks, so an attempt is not a batch. The
distinction is what lets an `InFlight` row say which stack died with it.
"""


_COMMIT_SHA = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
"""A full object name, lowercase, sha1 or sha256.

Full rather than abbreviated because the refinery merges by sha and
``require_staff_review_provenance`` bound the approval to a sha: an abbreviation
is a prefix query whose answer can change as the repository grows objects. Both
lengths because the object format is a repository property and refusing sha256
here would be an assumption about git that this module has no reason to make.
"""


def is_full_commit_sha(value: str) -> bool:
    """Whether a string is a full object name, without deciding what to do about it.

    The predicate is public and the raiser below is not, because the two callers
    want opposite things from the same rule. A dataclass is constructed from
    values this process computed, so a bad one is a programmer error and crashes.
    Enqueue is handed an approval payload, which is operator input arriving at a
    boundary, and a boundary answers a malformed input with a refusal a person
    can read rather than with a traceback.
    """

    return _COMMIT_SHA.fullmatch(value) is not None


def _require_commit_sha(value: str, *, field: str) -> str:
    """Reject a sha that is not a full object name, at construction.

    A boundary contract violation is a programmer error, so it crashes here
    rather than travelling into a ``git merge`` argument, where the same mistake
    becomes an error message about a revision instead of about a field.
    """

    if not _COMMIT_SHA.fullmatch(value):
        raise ValueError(f"{field} must be a full lowercase object name, got {value!r}")
    return value


def _require_present(value: str, *, field: str) -> str:
    if not value.strip():
        raise ValueError(f"{field} must be a non-empty identifier")
    return value


@dataclass(frozen=True)
class IntegrationSubject:
    """The exact thing asked to land, and the approval that authorises it.

    ``commit_sha`` rather than ``branch_name`` is what the refinery merges. The
    branch is a name that can move after an operator approved it, and
    ``require_staff_review_provenance`` bound the approval to a sha, not to a
    name. Merging the name would let a later commit ride in under an approval
    nobody gave it. The branch is carried anyway because a parked request has to
    be findable by a human, and ``agent/...`` refs outlive the run: the default
    ``cleanup_policy = "remove"`` removes the worktree directory and leaves the
    branch.

    Immutable for the request's whole life. Every state below embeds one, and
    `require_integration_transition` refuses a transition that changes it, so
    "what is being landed" cannot drift as the request moves.
    """

    request_id: IntegrationRequestId
    target_project_id: str
    branch_name: str
    base_head_sha: str
    commit_sha: str
    approval_id: str
    intent_id: str
    pow_wow_id: str
    milestone_key: str | None
    """``None`` when the code intent was not dispatched from a WorkUnit milestone."""
    changed_files: tuple[str, ...]
    enqueued_at: float

    def __post_init__(self) -> None:
        _require_present(self.request_id, field="request_id")
        _require_present(self.target_project_id, field="target_project_id")
        _require_present(self.branch_name, field="branch_name")
        _require_present(self.approval_id, field="approval_id")
        _require_commit_sha(self.base_head_sha, field="base_head_sha")
        _require_commit_sha(self.commit_sha, field="commit_sha")
        if self.commit_sha == self.base_head_sha:
            raise ValueError(
                f"request {self.request_id} asks to land {self.commit_sha}, which is its own "
                "base; there is nothing to integrate"
            )


@dataclass(frozen=True)
class MergeConflict:
    """git could not apply this commit onto the prefix that applied cleanly."""

    conflicted_paths: tuple[str, ...]

    def describe(self) -> str:
        paths = ", ".join(self.conflicted_paths) or "unreported paths"
        return f"merge conflict on {paths}; a rebase onto the new base is usually mechanical"


@dataclass(frozen=True)
class GateFailed:
    """It applied, and the combination did not pass the project's own gate."""

    command: str
    exit_code: int
    output_excerpt: str

    def describe(self) -> str:
        return (
            f"verification command {self.command!r} exited {self.exit_code} on the combination; "
            "this is a semantic disagreement between diffs, not a conflict"
        )


type BisectCause = MergeConflict | GateFailed
"""Why a request was taken out of a stack.

A sum because the two are diagnosed at different points and carry different
evidence: a conflict is attributable in O(1) during the merge and names paths, a
red gate is attributable only by bisection and names a command. Both park the
request; they differ only in what the operator is told.
"""


class WithdrawalReason(StrEnum):
    """Why a request left the queue without the queue judging it."""

    APPROVAL_REVOKED = "APPROVAL_REVOKED"
    MILESTONE_CANCELLED = "MILESTONE_CANCELLED"
    PROJECT_UNLINKED = "PROJECT_UNLINKED"
    COMMIT_UNREACHABLE = "COMMIT_UNREACHABLE"


@dataclass(frozen=True)
class Queued:
    """Waiting for a batch.

    Carries nothing but the subject, because nothing else is known yet. A
    ``batch_id`` field here would have to be null, and a null batch id is how a
    reader ends up asking whether an unbatched request is queued or lost.
    """

    subject: IntegrationSubject


@dataclass(frozen=True)
class InFlight:
    """A member of an attempt that has not returned.

    Exists so a refinery that dies mid-batch is recoverable rather than
    ambiguous. On restart, every `InFlight` row for a project is returned to
    `Queued` and its integration worktree is removed, because the integrated
    branch is never advanced except by a stack that already went green, so an
    unfinished attempt can always be redone from scratch.
    """

    subject: IntegrationSubject
    batch_id: IntegrationBatchId
    attempt_id: IntegrationAttemptId


@dataclass(frozen=True)
class Integrated:
    """Landed. The only state that may drive ``MERGE_APPROVED -> MERGED``."""

    subject: IntegrationSubject
    batch_id: IntegrationBatchId
    integration_commit_sha: str
    integrated_at: float

    def __post_init__(self) -> None:
        _require_commit_sha(self.integration_commit_sha, field="integration_commit_sha")


@dataclass(frozen=True)
class BisectedOut:
    """Isolated as the reason a stack was not green, and parked for a human.

    ``stack_beneath`` is not decoration. A request bisected out under one
    combination may integrate cleanly under another, and an operator deciding
    what to do needs to know which combination refused it, not merely that one
    did. It is the request ids that were already integrated when this one
    failed, in the order they landed; the driver holds the sha they add up to.
    """

    subject: IntegrationSubject
    batch_id: IntegrationBatchId
    cause: BisectCause
    stack_beneath: tuple[IntegrationRequestId, ...]
    stack_base_sha: str
    evidence_artifact_id: str
    bisected_at: float

    def __post_init__(self) -> None:
        _require_commit_sha(self.stack_base_sha, field="stack_base_sha")


@dataclass(frozen=True)
class Withdrawn:
    """Removed without an integration verdict.

    Separate from `BisectedOut` because the queue never judged it. Collapsing the
    two would let a revoked approval read as a diff that failed to merge, which
    is the difference between "nobody wants this any more" and "this and some
    other milestone disagree".
    """

    subject: IntegrationSubject
    reason: WithdrawalReason
    withdrawn_at: float


type IntegrationRequest = Queued | InFlight | Integrated | BisectedOut | Withdrawn


class IntegrationRequestState(StrEnum):
    """The discriminator for the request sum.

    Exists so the lifecycle can be written as a table the way
    ``_DISPATCH_PROMOTION_TRANSITIONS`` is, and so the durable row has one
    spelling for its state column. Deriving it with `state_of` rather than
    storing it on each variant keeps the two from disagreeing.
    """

    QUEUED = "QUEUED"
    IN_FLIGHT = "IN_FLIGHT"
    INTEGRATED = "INTEGRATED"
    BISECTED_OUT = "BISECTED_OUT"
    WITHDRAWN = "WITHDRAWN"


def state_of(request: IntegrationRequest) -> IntegrationRequestState:
    match request:
        case Queued():
            return IntegrationRequestState.QUEUED
        case InFlight():
            return IntegrationRequestState.IN_FLIGHT
        case Integrated():
            return IntegrationRequestState.INTEGRATED
        case BisectedOut():
            return IntegrationRequestState.BISECTED_OUT
        case Withdrawn():
            return IntegrationRequestState.WITHDRAWN
    assert_never(request)


_INTEGRATION_REQUEST_TRANSITIONS: dict[
    IntegrationRequestState, frozenset[IntegrationRequestState]
] = {
    IntegrationRequestState.QUEUED: frozenset(
        {
            IntegrationRequestState.IN_FLIGHT,
            IntegrationRequestState.WITHDRAWN,
        }
    ),
    # Back to QUEUED covers both recovery from a dead refinery and a fast-forward
    # the target repository refused, neither of which says anything about the
    # diff. INTEGRATED and BISECTED_OUT are the two verdicts a stack can produce.
    IntegrationRequestState.IN_FLIGHT: frozenset(
        {
            IntegrationRequestState.QUEUED,
            IntegrationRequestState.INTEGRATED,
            IntegrationRequestState.BISECTED_OUT,
            IntegrationRequestState.WITHDRAWN,
        }
    ),
    IntegrationRequestState.INTEGRATED: frozenset(),
    IntegrationRequestState.BISECTED_OUT: frozenset(),
    IntegrationRequestState.WITHDRAWN: frozenset(),
}


def next_integration_request_states(
    current: IntegrationRequestState,
) -> frozenset[IntegrationRequestState]:
    return _INTEGRATION_REQUEST_TRANSITIONS[current]


def require_integration_transition(
    current: IntegrationRequest,
    target: IntegrationRequest,
) -> None:
    """Fail closed when a request skips a queue boundary or changes what it lands.

    Two failures, not one. The state check is the lifecycle: ``QUEUED ->
    INTEGRATED`` without an attempt in between would be a merge nobody attempted,
    and any transition out of a terminal state would be a landed commit unlanded
    or a parked request quietly re-judged. The subject check is the harder one to
    notice: a transition that rewrites ``commit_sha`` would move an operator's
    approval onto a commit it was never given, which is the same hole that
    merging by branch name would open, arriving through the ledger instead of
    through git.
    """

    if current.subject != target.subject:
        raise ValueError(
            f"integration request {current.subject.request_id} may not change its subject "
            f"during a transition ({state_of(current)} -> {state_of(target)})"
        )
    current_state = state_of(current)
    target_state = state_of(target)
    if target_state not in _INTEGRATION_REQUEST_TRANSITIONS[current_state]:
        raise ValueError(
            f"invalid integration request transition: {current_state} -> {target_state}"
        )


__all__ = [
    "BisectCause",
    "BisectedOut",
    "GateFailed",
    "InFlight",
    "IntegrationAttemptId",
    "IntegrationBatchId",
    "IntegrationRequest",
    "IntegrationRequestId",
    "IntegrationRequestState",
    "IntegrationSubject",
    "Integrated",
    "MergeConflict",
    "Queued",
    "WithdrawalReason",
    "Withdrawn",
    "is_full_commit_sha",
    "next_integration_request_states",
    "require_integration_transition",
    "state_of",
]
