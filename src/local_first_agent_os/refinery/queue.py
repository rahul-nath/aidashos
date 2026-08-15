# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Which approved branches form the next stack, and in what order.

The selection rule is small, and all of its content is in what it refuses.

**Order.** ``(enqueued_at, request_id)``, a total order. The bisect rule splits a
batch repeatedly and every split preserves this order, which is what makes "who
must adapt" a rule rather than a judgment: when two diffs disagree, the one that
was approved later is the one parked. A partial order would leave that to
whichever row the database returned first, and the same queue would settle
differently on replay.

**Scope.** One ``target_project_id``. A batch spanning projects would build a
stack out of commits from two repositories, which is not a thing.

**Emptiness.** `SelectedBatch` cannot be empty, by construction. This is the one
place the implementation departs from the design doc, which checks for zero at
the top of the recursion instead. An empty stack is trivially green: the naive
path fast-forwards the integrated branch to the base it started from, succeeds,
and writes a durable record of an integration that integrated nothing, which a
later reader cannot tell from one that did. A refusal at the top of `integrate`
prevents that only for callers who go through `integrate`; a type that cannot
hold zero prevents it for everyone, and `NothingToIntegrate` then has to be
handled because it is not a batch.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .requests import (
    InFlight,
    IntegrationRequest,
    IntegrationRequestId,
    IntegrationSubject,
    Queued,
    state_of,
)


@dataclass(frozen=True)
class NothingToIntegrate:
    """This project has no queued request, so no refinery run happens at all.

    Its own answer rather than an empty batch: no integration worktree is
    allocated, no verification command runs, no fast-forward is attempted, and no
    batch row is written. A caller cannot reach any of that from here without
    first noticing it did not get a batch.
    """

    target_project_id: str


@dataclass(frozen=True)
class SelectedBatch:
    """A non-empty run of queued requests for one project, in enqueue order.

    Every invariant the bisect rule relies on is established here, once, at
    construction: non-empty, one project, unique identities, sorted. The rule
    preserves order through its splits, but it cannot establish an order it was
    not given, and a batch assembled by hand in a test or by a future caller that
    does not go through `select_next_batch` would otherwise be able to hand it a
    queue that was never FIFO.
    """

    target_project_id: str
    requests: tuple[Queued, ...]

    def __post_init__(self) -> None:
        if not self.requests:
            raise ValueError(
                f"a batch for {self.target_project_id} must hold at least one request; "
                "an empty selection is NothingToIntegrate, not a batch of zero"
            )
        seen: set[IntegrationRequestId] = set()
        for request in self.requests:
            subject = request.subject
            if subject.target_project_id != self.target_project_id:
                raise ValueError(
                    f"request {subject.request_id} targets {subject.target_project_id}, "
                    f"not {self.target_project_id}; a stack spans one repository"
                )
            if subject.request_id in seen:
                raise ValueError(f"request {subject.request_id} appears twice in one batch")
            seen.add(subject.request_id)
        if list(self.requests) != sorted(self.requests, key=_queue_order):
            raise ValueError(
                f"batch for {self.target_project_id} is not in (enqueued_at, request_id) order"
            )

    @property
    def request_ids(self) -> tuple[IntegrationRequestId, ...]:
        return tuple(request.subject.request_id for request in self.requests)

    def subject_for(self, request_id: IntegrationRequestId) -> IntegrationSubject:
        """The commit, branch, and approval behind one id.

        The bisect rule works in ids alone so that nothing about git can reach
        it. This is where the driver gets the rest back.
        """

        for request in self.requests:
            if request.subject.request_id == request_id:
                return request.subject
        raise KeyError(f"{request_id} is not a member of this batch")


type BatchSelection = NothingToIntegrate | SelectedBatch


def _queue_order(request: Queued) -> tuple[float, str]:
    return (request.subject.enqueued_at, request.subject.request_id)


def select_next_batch(
    requests: Sequence[IntegrationRequest],
    *,
    target_project_id: str,
) -> BatchSelection:
    """Take every queued request for one project as the next batch, in enqueue order.

    Pure: the caller passes the rows it already read, so the decision can be
    tested without a ledger and cannot reach anything the caller did not hand it.

    There is no batch size cap. A batch of N costs one gate run when it is green
    and at most ``2N - 1`` when nothing in it can land, and capping it would trade
    the good case, which is the common one, against a bad case that was not going
    to land anything either way.

    Duplicate ``commit_sha`` is not checked here. Enqueue refuses a second
    non-terminal request for a sha that already has one, which is what makes
    enqueue idempotent under replay, and re-deciding it here would give one rule
    two homes that can disagree.

    An `InFlight` request for this project is a programmer error and crashes.
    Only one refinery may run per project, held under an advisory lock, and
    recovery returns `InFlight` rows to `Queued` before selection is asked
    anything. Reaching selection with one outstanding means either the lock was
    not held or recovery was skipped. Skipping the row instead would build a
    second stack on a base the first refinery was about to invalidate, and one of
    the two would silently lose its batch.
    """

    mine = [
        request for request in requests if request.subject.target_project_id == target_project_id
    ]
    seen: set[IntegrationRequestId] = set()
    for request in mine:
        request_id = request.subject.request_id
        if request_id in seen:
            raise ValueError(
                f"two rows claim integration request {request_id} for {target_project_id}"
            )
        seen.add(request_id)
        if isinstance(request, InFlight):
            raise ValueError(
                f"integration request {request_id} is still {state_of(request)} on batch "
                f"{request.batch_id}; recover outstanding attempts before selecting a batch "
                f"for {target_project_id}"
            )
    queued = sorted(
        (request for request in mine if isinstance(request, Queued)),
        key=_queue_order,
    )
    if not queued:
        return NothingToIntegrate(target_project_id=target_project_id)
    return SelectedBatch(target_project_id=target_project_id, requests=tuple(queued))


__all__ = [
    "BatchSelection",
    "NothingToIntegrate",
    "SelectedBatch",
    "select_next_batch",
]
