# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The commands an operator can run next, with this WorkUnit's ids already in them.

``status_legend`` says what a status means and what to do about it in a sentence.
This module is that sentence as argv. The gap between the two was the daily cost
of driving the system: every command prints a `work_unit_id`, a
`compiled_plan_revision_id`, and a `plan_hash`, and the operator retyped them
into the next command by hand.

Three properties make this worth a module rather than an f-string at each call
site.

**It never prints a command that cannot run.** A next action an operator can
copy, run, and watch fail is worse than no suggestion, because it spends their
attention proving the tool wrong. ``project_action`` already treats that as an
invariant violation (``LifecycleInvariantViolation.INVALID_VISIBLE_NEXT_ACTION``)
and refuses to guess when a status is unreadable. The same rule holds here: a
command carrying a ``<placeholder>`` is never ``READY``, enforced by the model
rather than by reviewer attention.

**It shows the refused ones too, with the code they would hit.** The recovery
verbs read alike and are not alike, and the near-miss is expensive:
``adopt_settled_work_unit_dispatch`` looks like the fix for a milestone that died
with ``dispatch_wait_elapsed`` and refuses with
``settled_adoption_dispatch_not_done``, because the intent is FAILED rather than
DONE. That distinction cost an operator an hour. Both discriminating facts -
``MilestoneView.dispatch_status`` and ``MilestoneView.produced_artifacts`` - are
already in the view the command just printed, so naming the refusal costs no
query at all.

**It is total over the enums it reads.** A new ``WorkUnitStatus`` or a new
blocking kind that nobody taught this module about stops the import, rather than
silently printing a shorter list. Same construction, and the same reason, as
``status_legend.legend_entries``.

Rendering lives in the CLI. This module returns typed values so the cockpit can
consume the same rules over HTTP later without a second copy of them.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Literal, get_args

from pydantic import model_validator

from ..contracts import DispatchIntentStatus
from .events import DecisionRequestKind, OperatorDecision
from .lifecycle import MilestoneExecutionStatus, WorkUnitStatus
from .projection import BlockingCondition, MilestoneView, OperatorContract, WorkUnitView

SCHEMA_VERSION_NEXT_COMMANDS: Final = "next_commands.v1"

CLI: Final = "agent-ledger"

# The marker that makes a command un-runnable as printed. Kept as a constant
# because the model validator and the builders have to agree on it exactly.
PLACEHOLDER_OPEN: Final = "<"


class IncompleteNextCommandRules(RuntimeError):
    """A rule table and the enum it is keyed by disagree about the member set.

    A programmer error rather than a runtime condition, so it crashes the import.
    A partial table would drop the commands for the new member and look like
    "there is nothing to do here", which is the most expensive wrong answer this
    module can give.
    """


class NextCommandStatus(StrEnum):
    """Whether the operator can run this now, and if not, why not."""

    READY = "READY"
    """Runnable as printed. Every id is substituted and every precondition holds."""

    REFUSED = "REFUSED"
    """The verb applies to this situation but a precondition provably fails.

    Printed anyway, with the refusal code, because the operator would otherwise
    reach for it and learn the same thing from a failed command.
    """

    UNPROVED = "UNPROVED"
    """Neither ready nor refused: the view does not settle it.

    A third status rather than folding into REFUSED, because they call for
    opposite things. REFUSED means stop. UNPROVED means the command needs a fact
    only the operator has - a commit sha, an acceptance rationale - or a check
    this view cannot make. Collapsing them would either hide usable recoveries or
    promise ones that fail.
    """


class NextCommand(OperatorContract):
    """One command an operator might run next, and what running it would mean."""

    command: str
    """The exact argv, shlex-joined, with every known id already substituted."""

    intent: str
    """What running it does, in the operator's terms rather than the code's."""

    status: NextCommandStatus
    precondition: str | None = None
    """What must hold for this to work. ``None`` when it always applies."""

    reason: str | None = None
    """Why it is REFUSED or UNPROVED, in terms of this WorkUnit's own evidence."""

    refusal_code: str | None = None
    """The code the verb would actually raise, so the refusal is greppable."""

    @model_validator(mode="after")
    def _runnable_as_printed(self) -> NextCommand:
        """A READY command must be copy-pasteable, and a refused one must say why.

        The placeholder check is the invariant this module exists to hold. It is
        enforced here rather than at each construction site because there are a
        dozen of those and one of them will eventually interpolate an id that is
        ``None``.
        """

        if self.status is NextCommandStatus.READY and PLACEHOLDER_OPEN in self.command:
            raise ValueError(
                f"a READY next command must be runnable as printed, but "
                f"{self.command!r} still carries a placeholder"
            )
        if self.status is not NextCommandStatus.READY and not self.reason:
            raise ValueError(f"a {self.status.value} next command must say why: {self.command!r}")
        return self


class NextCommandSet(OperatorContract):
    """Every next command for one printed result, plus the headline above them."""

    schema_version: Literal["next_commands.v1"] = SCHEMA_VERSION_NEXT_COMMANDS
    headline: str
    """One line naming the situation the commands below answer."""

    detail: str | None = None
    commands: tuple[NextCommand, ...] = ()

    @property
    def ready(self) -> tuple[NextCommand, ...]:
        return tuple(c for c in self.commands if c.status is NextCommandStatus.READY)


@dataclass(frozen=True)
class _StatusDisposition:
    """What a WorkUnit status implies about the operator's move.

    A declared disposition per status rather than a branch per status, so the
    totality check below is checking something real: adding a member to
    ``WorkUnitStatus`` forces a decision about whether it can be resumed,
    cancelled, or recovered, instead of falling through to whatever the last
    ``elif`` happened to be.
    """

    headline: str
    resumable: bool
    cancellable: bool
    recoverable: bool
    """Whether the adoption and review-recovery verbs are worth showing at all."""


def _require_total[E: StrEnum, V](enum: type[E], table: Mapping[E, V], label: str) -> None:
    """Refuse a table that is not exactly the enum, in both directions.

    A missing member is the obvious failure. A foreign key catches the copy-paste
    between two tables keyed by different enums, which a plain length check would
    absorb.
    """

    members = frozenset(enum)
    missing = sorted(member.value for member in enum if member not in table)
    foreign = sorted(str(key) for key in table if key not in members)
    if missing or foreign:
        raise IncompleteNextCommandRules(
            f"{label} is not {enum.__name__}: missing={missing or None} foreign={foreign or None}"
        )


_DISPOSITIONS: Final[dict[WorkUnitStatus, _StatusDisposition]] = {
    WorkUnitStatus.DRAFT: _StatusDisposition(
        headline="the document is not compiled into an executable plan yet",
        resumable=False,
        cancellable=False,
        recoverable=False,
    ),
    WorkUnitStatus.COMPILED: _StatusDisposition(
        headline="the plan is compiled and hashed but has not been started",
        resumable=False,
        cancellable=True,
        recoverable=False,
    ),
    WorkUnitStatus.QUEUED: _StatusDisposition(
        headline="accepted for execution and waiting for the runtime to pick it up",
        resumable=False,
        cancellable=True,
        recoverable=False,
    ),
    WorkUnitStatus.RUNNING: _StatusDisposition(
        headline="the root workflow is executing milestones",
        resumable=False,
        cancellable=True,
        recoverable=False,
    ),
    WorkUnitStatus.WAITING_FOR_OPERATOR: _StatusDisposition(
        headline="execution is paused on a decision only you can make",
        resumable=False,
        cancellable=True,
        recoverable=False,
    ),
    WorkUnitStatus.BLOCKED: _StatusDisposition(
        headline="a correctable failure parked this work for you",
        resumable=True,
        cancellable=True,
        recoverable=True,
    ),
    WorkUnitStatus.CANCELLING: _StatusDisposition(
        headline="cancellation was requested and the stop cascade has not finished",
        resumable=False,
        cancellable=False,
        recoverable=False,
    ),
    WorkUnitStatus.SUCCEEDED: _StatusDisposition(
        headline="every phase completed and the required evidence was recorded",
        resumable=False,
        cancellable=False,
        recoverable=False,
    ),
    WorkUnitStatus.FAILED: _StatusDisposition(
        headline="the work ended without completing and will not retry itself",
        resumable=False,
        cancellable=False,
        recoverable=True,
    ),
    WorkUnitStatus.CANCELLED: _StatusDisposition(
        headline="an operator stopped this work before it finished",
        resumable=False,
        cancellable=False,
        recoverable=False,
    ),
    WorkUnitStatus.SUPERSEDED: _StatusDisposition(
        headline="a newer WorkUnit replaced this one; it is kept as history",
        resumable=False,
        cancellable=False,
        recoverable=False,
    ),
}

_require_total(WorkUnitStatus, _DISPOSITIONS, "the next-command disposition table")


# The blocking kinds, read off the projection's own Literal rather than
# re-listed, so a kind added there and not here fails at import.
_BLOCKING_KINDS: Final[tuple[str, ...]] = get_args(
    BlockingCondition.model_fields["kind"].annotation
)

_BLOCKING_HEADLINES: Final[dict[str, str]] = {
    "NONE": "nothing is blocking this work",
    "OPERATOR_DECISION": "an operator decision is required before this work can continue",
    "BLOCKED_MILESTONE": "a milestone stopped without finishing and needs recovery",
    "FAILED_MILESTONE": "a milestone failed",
}

if set(_BLOCKING_HEADLINES) != set(_BLOCKING_KINDS):
    raise IncompleteNextCommandRules(
        f"the blocking headline table is not BlockingCondition.kind: "
        f"missing={sorted(set(_BLOCKING_KINDS) - set(_BLOCKING_HEADLINES)) or None} "
        f"foreign={sorted(set(_BLOCKING_HEADLINES) - set(_BLOCKING_KINDS)) or None}"
    )


# Which answers each request kind accepts. `events.decision_outcome` is the
# authority; this is the operator-facing projection of it, and
# `test_next_commands.py` proves the two agree by running every pair through the
# real resolver. Offering a decision the resolver rejects would print a READY
# command that cannot run, which is the one thing this module must not do.
_PERMITTED_DECISIONS: Final[dict[DecisionRequestKind, tuple[OperatorDecision, ...]]] = {
    DecisionRequestKind.APPROVAL: (OperatorDecision.APPROVED, OperatorDecision.DENIED),
    DecisionRequestKind.CLARIFICATION: (OperatorDecision.ANSWERED,),
    DecisionRequestKind.RETRY_BUDGET_OVERRIDE: (
        OperatorDecision.APPROVED,
        OperatorDecision.DENIED,
    ),
}

_require_total(DecisionRequestKind, _PERMITTED_DECISIONS, "the permitted-decision table")


def _cmd(*parts: str) -> str:
    return shlex.join([CLI, *parts])


def decision_idempotency_key(request_id: str, decision: OperatorDecision) -> str:
    """A globally unique key for one answer to one decision request.

    There is a partial unique index on
    ``work_unit_decision_requests.response_idempotency_key``, so any string
    reused anywhere in the system collides on its second use. Deriving it from
    the request id makes it unique by construction, and including the decision
    keeps APPROVED and DENIED on the same request from colliding with each other.

    Duplicate-submission protection does not use this string: ``events.py``
    derives its own key from the workflow, phase, milestone, attempt, and
    transition. This one only has to be unique.
    """

    return f"decision-{request_id}-{decision.value.lower()}"


def _decision_commands(view: WorkUnitView) -> list[NextCommand]:
    """The answer to every pending decision, one command per permitted verdict."""

    commands: list[NextCommand] = []
    for pending in view.pending_decisions:
        try:
            kind = DecisionRequestKind(pending.request_kind)
        except ValueError:
            # An unreadable kind is the `project_action` situation: the view
            # carries a value this code cannot reason about. Refusing to guess is
            # the whole point; offering APPROVED here could answer a
            # CLARIFICATION with a verdict the resolver rejects.
            commands.append(
                NextCommand(
                    command=_cmd(
                        "submit_work_unit_decision",
                        view.work_unit_id,
                        pending.request_id,
                        "<APPROVED|DENIED|ANSWERED>",
                        "<idempotency_key>",
                    ),
                    intent=f"answer the pending decision: {pending.prompt}",
                    status=NextCommandStatus.UNPROVED,
                    precondition="the request kind decides which verdicts are accepted",
                    reason=(
                        f"request kind {pending.request_kind!r} is not one this "
                        f"version knows, so the accepted verdicts cannot be named"
                    ),
                )
            )
            continue
        for decision in _PERMITTED_DECISIONS[kind]:
            commands.append(
                NextCommand(
                    command=_cmd(
                        "submit_work_unit_decision",
                        view.work_unit_id,
                        pending.request_id,
                        decision.value,
                        decision_idempotency_key(pending.request_id, decision),
                    ),
                    intent=f"{decision.value.lower()}: {pending.prompt}",
                    status=NextCommandStatus.READY,
                    precondition=None,
                )
            )
    return commands


def _blocked_milestones(view: WorkUnitView) -> tuple[MilestoneView, ...]:
    keys = frozenset(view.blocking.milestone_keys)
    return tuple(item for item in view.milestones if item.stable_key in keys)


def _settled_adoption(view: WorkUnitView, milestone: MilestoneView) -> NextCommand:
    """Credit a wait-elapsed milestone from its own dispatch, once that settled DONE.

    The refusal is decided here rather than left to the verb because this is the
    documented near-miss: the milestone died with ``dispatch_wait_elapsed``, which
    reads exactly like the condition this verb exists for, and the intent behind
    it is FAILED rather than DONE.
    """

    command = _cmd(
        "adopt_settled_work_unit_dispatch",
        view.work_unit_id,
        milestone.stable_key,
    )
    intent = "credit this milestone with a dispatch that finished after the wait elapsed"
    precondition = "the milestone's own dispatch intent settled DONE"
    if milestone.dispatch_status is DispatchIntentStatus.DONE:
        return NextCommand(
            command=command,
            intent=intent,
            status=NextCommandStatus.READY,
            precondition=precondition,
        )
    if milestone.dispatch_status is None:
        return NextCommand(
            command=command,
            intent=intent,
            status=NextCommandStatus.UNPROVED,
            precondition=precondition,
            reason=f"milestone {milestone.stable_key} has no dispatch intent recorded",
        )
    return NextCommand(
        command=command,
        intent=intent,
        status=NextCommandStatus.REFUSED,
        precondition=precondition,
        reason=(
            f"intent {milestone.dispatch_intent_id or '?'} is "
            f"{milestone.dispatch_status.value}, not DONE"
        ),
        refusal_code="settled_adoption_dispatch_not_done",
    )


def _review_recovery(milestone: MilestoneView) -> NextCommand | None:
    """Reparse a staff review that ran and came back unclassified.

    ``None`` when there is no intent to name, because the verb takes one and a
    command with a placeholder where the id goes is not worth printing next to
    two that are runnable.
    """

    if not milestone.dispatch_intent_id:
        return None
    command = _cmd("recover_unparsed_staff_review", milestone.dispatch_intent_id)
    intent = "reparse a staff review whose verdict came back UNCLASSIFIED"
    precondition = (
        "a FAILED code intent holding both a worktree_commit_checkpoint and a "
        "review_result artifact whose verdict is UNCLASSIFIED"
    )
    produced = frozenset(milestone.produced_artifacts)
    if "review_result" not in produced:
        # The handoff's rule - "a review that never ran has nothing to reparse" -
        # made automatic. The artifact list is already on this row, so the check
        # that used to mean reading `list_work_unit_artifacts` by hand is free.
        observed = ", ".join(sorted(produced)) or "nothing"
        return NextCommand(
            command=command,
            intent=intent,
            status=NextCommandStatus.REFUSED,
            precondition=precondition,
            reason=(
                f"milestone {milestone.stable_key} produced {observed}; "
                f"there is no staff review to reparse"
            ),
            refusal_code="staff_review_missing",
        )
    return NextCommand(
        command=command,
        intent=intent,
        status=NextCommandStatus.UNPROVED,
        precondition=precondition,
        reason="a review_result exists; whether its verdict is UNCLASSIFIED is not in this view",
    )


def _integrated_adoption(view: WorkUnitView, milestone: MilestoneView) -> NextCommand:
    """Attest that an already-integrated commit satisfies blocked work.

    Always UNPROVED. The commit sha and the acceptance rationale are facts the
    operator holds and this view does not, and the verb additionally refuses
    unless the attempt was provider-blocked.
    """

    return NextCommand(
        command=_cmd(
            "adopt_integrated_work_unit_milestone",
            view.work_unit_id,
            milestone.stable_key,
            "<commit_sha>",
            "--accepted-by",
            "<who>",
            "--acceptance-evidence",
            "<why this commit satisfies the milestone>",
        ),
        intent="attest that a commit already in the project satisfies this milestone",
        status=NextCommandStatus.UNPROVED,
        precondition=(
            "the attempt was provider-blocked, and the named commit is an "
            "integrated ancestor of the project HEAD"
        ),
        reason="the commit sha and the acceptance rationale are yours to supply",
        refusal_code="integrated_adoption_not_provider_blocked",
    )


def _recovery_commands(view: WorkUnitView) -> list[NextCommand]:
    """The adoption and review-recovery verbs, for each milestone that is stuck.

    Ordered so the decidable ones come first. An operator reading top-down should
    meet the verb that works, or the reason the obvious one does not, before the
    one that needs them to go find a commit sha.
    """

    commands: list[NextCommand] = []
    candidates = _blocked_milestones(view) or tuple(
        item
        for item in view.milestones
        if item.status in {MilestoneExecutionStatus.BLOCKED, MilestoneExecutionStatus.FAILED}
    )
    for milestone in candidates:
        commands.append(_settled_adoption(view, milestone))
        recovery = _review_recovery(milestone)
        if recovery is not None:
            commands.append(recovery)
        commands.append(_integrated_adoption(view, milestone))
    return commands


def _watch_commands(view: WorkUnitView) -> list[NextCommand]:
    """Reading the work, which is always allowed and is what a running unit wants."""

    return [
        NextCommand(
            command=_cmd("get_work_unit", view.work_unit_id),
            intent="re-read this view",
            status=NextCommandStatus.READY,
        ),
        NextCommand(
            command=_cmd("list_work_unit_events", view.work_unit_id),
            intent="read the event log, which says what happened and when",
            status=NextCommandStatus.READY,
        ),
        NextCommand(
            command=_cmd("list_work_unit_artifacts", view.work_unit_id),
            intent="read the evidence produced so far",
            status=NextCommandStatus.READY,
        ),
    ]


def next_commands_for_view(view: WorkUnitView) -> NextCommandSet:
    """Every command worth running against this WorkUnit right now.

    The order is the operator's priority order, and it is the same one
    ``projection._blocking_condition`` already established: answer the decision
    first, then clear the block, then the ordinary reads. Recovery verbs appear
    only for a status whose disposition says they could apply, so a SUCCEEDED
    WorkUnit does not offer adoption.
    """

    disposition = _DISPOSITIONS[view.status]
    commands: list[NextCommand] = []

    if view.blocking.kind == "OPERATOR_DECISION" or view.pending_decisions:
        commands.extend(_decision_commands(view))

    if disposition.resumable:
        commands.append(
            NextCommand(
                command=_cmd("resume_work_unit", view.work_unit_id),
                intent="re-drive the parked work; this spends another attempt from its budget",
                status=NextCommandStatus.READY,
                precondition="the staffed harnesses can act, and the ledger has connections free",
            )
        )

    if disposition.recoverable:
        commands.extend(_recovery_commands(view))

    if disposition.cancellable:
        commands.append(
            NextCommand(
                command=_cmd("cancel_work_unit", view.work_unit_id),
                # The tradeoff, not the mechanism. Cancelling is cheap to type and
                # expensive to undo: a replacement WorkUnit is built from the plan,
                # so it re-runs the milestones this one already paid for, and the
                # commits they produced are only credited back one at a time
                # through `adopt_integrated_work_unit_milestone`.
                intent=(
                    "stop for good; a replacement re-runs every milestone from the plan, "
                    "including the ones already finished here"
                ),
                status=NextCommandStatus.READY,
            )
        )

    commands.extend(_watch_commands(view))

    return NextCommandSet(
        headline=f"{view.status.value}  {disposition.headline}",
        detail=_blocking_detail(view),
        commands=tuple(commands),
    )


def _blocking_detail(view: WorkUnitView) -> str | None:
    """Name the milestones the commands above are about, when there are any."""

    if view.blocking.kind == "NONE":
        return None
    named = []
    keys = frozenset(view.blocking.milestone_keys)
    for item in view.milestones:
        if item.stable_key not in keys:
            continue
        failure = f" · {item.failure_code}" if item.failure_code else ""
        named.append(f'milestone {item.stable_key} "{item.title}"{failure}')
    detail = _BLOCKING_HEADLINES.get(view.blocking.kind, view.blocking.detail)
    return f"{detail}: {'; '.join(named)}" if named else detail


def _compile_commands(payload: Mapping[str, Any]) -> NextCommandSet:
    """Carry the three ids a compile produces into the command that uses them.

    This is the affordance's original case. A compile prints a
    ``compiled_plan_revision_id`` and a ``plan_hash``, and starting the run means
    retyping both.
    """

    revision = str(payload.get("compiled_plan_revision_id") or "")
    plan_hash = str(payload.get("plan_hash") or "")
    blockers = payload.get("execution_blockers") or []
    runnable = bool(payload.get("runnable"))
    status = str(payload.get("validation_status") or "UNKNOWN")

    if not revision:
        return NextCommandSet(
            headline=f"{status}  the document did not compile to a plan",
            detail="fix the diagnostics above, then compile again",
        )

    # No `--title` placeholder. The flag is optional and defaults to the first
    # milestone's title, so leaving it out keeps the command runnable exactly as
    # printed - which matters more here than anywhere else, because this is the
    # command the whole affordance exists for. Naming the flag in the intent
    # tells an operator who wants their own title how to say so.
    start = _cmd(
        "start_work_unit",
        revision,
        *(("--approved-plan-hash", plan_hash) if plan_hash else ()),
    )
    if runnable and not blockers:
        return NextCommandSet(
            headline=f"{status}  the plan is runnable",
            commands=(
                NextCommand(
                    command=start,
                    intent=(
                        "start this exact plan; the hash is what approval binds to. "
                        'Add --title "..." to name the run yourself'
                    ),
                    status=NextCommandStatus.READY,
                ),
            ),
        )
    listed = "; ".join(str(item) for item in blockers) or "the plan is not runnable"
    return NextCommandSet(
        headline=f"{status}  the plan cannot start yet",
        detail=listed,
        commands=(
            NextCommand(
                command=start,
                intent="start this plan",
                status=NextCommandStatus.REFUSED,
                precondition="no execution blockers",
                reason=listed,
                refusal_code="execution_blocked",
            ),
        ),
    )


def _started_commands(payload: Mapping[str, Any]) -> NextCommandSet:
    """After a start, the operator wants to watch it and knows nothing else yet."""

    work_unit_id = str(payload.get("work_unit_id") or "")
    if not work_unit_id:
        return NextCommandSet(headline="no WorkUnit was created")
    delivered = _delivery_reason(payload.get("dispatch"))
    commands = [
        NextCommand(
            command=_cmd("get_work_unit", work_unit_id),
            intent="watch the milestones; this is the command the watch loop wraps",
            status=NextCommandStatus.READY,
        ),
        NextCommand(
            command=_cmd("cancel_work_unit", work_unit_id),
            intent="stop it",
            status=NextCommandStatus.READY,
        ),
    ]
    if delivered is not None:
        # A start whose enqueue row is still pending is the "no active DBOS
        # runtime" case. It is not a failure, but it is the one state where the
        # operator has a command to run and no way to know it from the payload.
        commands.insert(
            0,
            NextCommand(
                command=_cmd("drain_work_unit_enqueues", "--limit", "1"),
                intent="deliver the pending enqueue row yourself",
                status=NextCommandStatus.READY,
                precondition="only needed while no resident drainer is running",
            ),
        )
    return NextCommandSet(
        headline=f"the WorkUnit was created: {work_unit_id}",
        detail=delivered,
        commands=tuple(commands),
    )


def _delivery_reason(dispatch: object) -> str | None:
    """The reason an enqueue was not delivered, when it was not."""

    if not isinstance(dispatch, Sequence) or isinstance(dispatch, str | bytes):
        return None
    for item in dispatch:
        if not isinstance(item, Mapping):
            continue
        if item.get("delivered") is False:
            return str(item.get("reason") or "the enqueue row is still pending")
    return None


def _work_unit_index_commands(payload: Mapping[str, Any]) -> NextCommandSet:
    """From a list, the useful next move is to open one, so name the live ones."""

    rows = payload.get("work_units")
    if not isinstance(rows, Sequence):
        return NextCommandSet(headline="no WorkUnits")
    live = [
        row
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("status") or "")
        in {
            WorkUnitStatus.RUNNING.value,
            WorkUnitStatus.QUEUED.value,
            WorkUnitStatus.BLOCKED.value,
            WorkUnitStatus.WAITING_FOR_OPERATOR.value,
            WorkUnitStatus.CANCELLING.value,
        }
    ]
    if not live:
        return NextCommandSet(
            headline="no WorkUnit is running, queued, blocked, or waiting on you",
        )
    return NextCommandSet(
        headline=f"{len(live)} WorkUnit(s) still need attention",
        commands=tuple(
            NextCommand(
                command=_cmd("get_work_unit", str(row.get("work_unit_id") or "")),
                intent=f"{row.get('status')}  {row.get('title')}",
                status=NextCommandStatus.READY,
            )
            for row in live
        ),
    )


def _followup_commands(payload: Mapping[str, Any]) -> NextCommandSet:
    """After a verb that moved a WorkUnit, say whether it actually took effect.

    Re-reading is not the whole move, which is what this used to assume. A
    `resume_work_unit` with no DBOS runtime to receive it returns `ok: true`,
    returns the milestones to READY, and delivers nothing: `delivered` is false
    and no outbox row is written, so nothing will ever pick the continuation up.
    That is the single most important fact in the payload and it was being
    dropped, leaving "re-read the WorkUnit" as the advice for a WorkUnit that had
    not moved.
    """

    work_unit_id = str(payload.get("work_unit_id") or "")
    if not work_unit_id:
        return NextCommandSet(headline="no WorkUnit to follow up on")

    # `resume_work_unit` reports delivery at the top level; `start_work_unit`
    # reports it per enqueue under `dispatch`. Both spellings are read here
    # because an operator does not know or care which verb used which shape.
    undelivered = _delivery_reason(payload.get("dispatch"))
    if undelivered is None and payload.get("delivered") is False:
        undelivered = str(payload.get("reason") or "nothing received the continuation")

    commands = [
        NextCommand(
            command=_cmd("get_work_unit", work_unit_id),
            intent="re-read the WorkUnit to see what the change did",
            status=NextCommandStatus.READY,
        )
    ]
    if undelivered is None:
        return NextCommandSet(
            headline=f"the WorkUnit was updated: {work_unit_id}",
            commands=tuple(commands),
        )
    commands.insert(
        0,
        NextCommand(
            command=_cmd("resume_work_unit", work_unit_id, "--inline"),
            intent="drive the lifecycle in this process, which needs no resident runtime",
            status=NextCommandStatus.READY,
            precondition="this spends attempts from the milestone budgets, and agent seats",
        ),
    )
    return NextCommandSet(
        headline=f"the WorkUnit changed but nothing is running it: {work_unit_id}",
        detail=f"{undelivered}. Until something delivers it, this WorkUnit is parked.",
        commands=tuple(commands),
    )


def _doctrine_stale_commands(payload: Mapping[str, Any]) -> NextCommandSet:
    """After the doctrine staleness scan, the moves that clear each stale review.

    The scan is a query on purpose - re-reviewing spends staff seats, so the
    spending stays behind commands an operator chooses to run. Three moves per
    stale row, in remedy order: open the owning WorkUnit (whose own affordance
    says how to re-drive it), request the checkpoint-keyed recovery staff
    review where one can actually run, and retire the pending approval the
    gate would refuse anyway. The recovery-review verb is printed REFUSED when
    the dispatch completed cleanly, because it only accepts a PAUSED or FAILED
    execution checkpoint - meeting that refusal here is cheaper than meeting it
    from the failed command.
    """

    stale = _stale_rows(payload)
    doctrine = payload.get("current_doctrine")
    version = str(doctrine.get("schema_version")) if isinstance(doctrine, Mapping) else "unknown"
    if not stale:
        merge_pending = payload.get("merge_pending")
        return NextCommandSet(
            headline=(
                f"every MERGE_PENDING review ({merge_pending} scanned) passes the "
                f"merge gate under {version}"
            ),
        )

    commands: list[NextCommand] = []
    seen_work_units: set[str] = set()
    for row in stale:
        intent_id = str(row.get("intent_id") or "unknown-intent")
        issue_code = str(row.get("issue_code") or "unknown")
        work_unit_id = str(row.get("work_unit_id") or "")
        if work_unit_id and work_unit_id not in seen_work_units:
            seen_work_units.add(work_unit_id)
            commands.append(
                NextCommand(
                    command=_cmd("get_work_unit", work_unit_id),
                    intent=(
                        f"open the WorkUnit that owns intent {intent_id}; its own "
                        "next commands say how to re-drive the milestone so a fresh "
                        "staff review runs under the current doctrine"
                    ),
                    status=NextCommandStatus.READY,
                )
            )
        recovery = row.get("recovery_review")
        precondition = (
            "a PAUSED or FAILED execution checkpoint whose base and saga agree "
            "with the retained commit"
        )
        if isinstance(recovery, Mapping):
            commands.append(
                NextCommand(
                    command=_cmd(
                        "request_recovery_staff_review",
                        str(recovery.get("checkpoint_id") or ""),
                        "--target-project-id",
                        str(recovery.get("target_project_id") or ""),
                        "--branch",
                        str(recovery.get("branch") or ""),
                        "--base-head-sha",
                        str(recovery.get("base_sha") or ""),
                        "--commit-sha",
                        str(recovery.get("commit_sha") or ""),
                    ),
                    intent=(
                        f"re-review intent {intent_id}'s exact retained commit under "
                        "the current doctrine; this spends a staff seat"
                    ),
                    status=NextCommandStatus.READY,
                    precondition=precondition,
                )
            )
        else:
            commands.append(
                NextCommand(
                    command=_cmd(
                        "request_recovery_staff_review",
                        "<checkpoint_id>",
                        "--target-project-id",
                        str(row.get("target_project_id") or "<target_project_id>"),
                        "--branch",
                        str(row.get("branch") or "<branch>"),
                        "--base-head-sha",
                        str(row.get("base_sha") or "<base_sha>"),
                        "--commit-sha",
                        str(row.get("commit_sha") or "<commit_sha>"),
                    ),
                    intent=(
                        f"re-review intent {intent_id}'s exact retained commit under "
                        "the current doctrine"
                    ),
                    status=NextCommandStatus.REFUSED,
                    precondition=precondition,
                    reason=(
                        f"intent {intent_id} holds no such checkpoint; a dispatch "
                        "that completed cleanly leaves none, so the milestone must "
                        "be re-driven instead"
                    ),
                    refusal_code="recovery_staff_review_requires_paused_checkpoint",
                )
            )
        approval_id = str(row.get("approval_id") or "")
        if approval_id:
            commands.append(
                NextCommand(
                    command=_cmd(
                        "resolve_approval_request",
                        approval_id,
                        "deny",
                        "--resolved-by",
                        "operator",
                    ),
                    intent=(
                        f"retire the pending approval for intent {intent_id}; the "
                        f"merge gate refuses it with {issue_code}"
                    ),
                    status=NextCommandStatus.READY,
                )
            )
    return NextCommandSet(
        headline=(
            f"{len(stale)} MERGE_PENDING review(s) cannot pass the merge gate under {version}"
        ),
        detail=(
            "; ".join(f"intent {row.get('intent_id')}: {row.get('issue_code')}" for row in stale)
            + ". The operator procedure is documented in docs/doctrine_bump_recovery.md."
        ),
        commands=tuple(commands),
    )


def _stale_rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = payload.get("stale")
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return []
    return [row for row in raw if isinstance(row, Mapping)]


# Which builder each command's payload feeds. A command absent from this table
# simply prints no suggestions, which is the right default: this is an
# affordance, and a command nobody has taught it about should stay silent rather
# than guess.
_BUILDERS: Final[dict[str, Any]] = {
    "compile_design_doc": _compile_commands,
    "start_work_unit": _started_commands,
    "list_work_units": _work_unit_index_commands,
    "resume_work_unit": _followup_commands,
    "cancel_work_unit": _followup_commands,
    "submit_work_unit_decision": _followup_commands,
    "adopt_recovered_work_unit_dispatch": _followup_commands,
    "adopt_settled_work_unit_dispatch": _followup_commands,
    "adopt_integrated_work_unit_milestone": _followup_commands,
    "list_doctrine_stale_reviews": _doctrine_stale_commands,
}


def next_commands_for(command: str, payload: Mapping[str, Any]) -> NextCommandSet | None:
    """The next commands for one CLI result, or ``None`` when there are none to give.

    ``None`` rather than an empty set, so the renderer can tell "this command has
    no affordance" from "this WorkUnit has nothing left to do", which read very
    differently on a terminal.

    A failed result gets nothing. The payload of a refusal holds an error code
    rather than the ids these builders read, and inventing a next step from a
    command that did not happen is how a suggestion becomes a lie.
    """

    if not payload.get("ok", False):
        return None
    if command == "get_work_unit":
        raw = payload.get("work_unit")
        if not isinstance(raw, Mapping):
            return None
        return next_commands_for_view(WorkUnitView.model_validate(raw))
    builder = _BUILDERS.get(command)
    if builder is None:
        return None
    return builder(payload)


__all__ = [
    "CLI",
    "SCHEMA_VERSION_NEXT_COMMANDS",
    "IncompleteNextCommandRules",
    "NextCommand",
    "NextCommandSet",
    "NextCommandStatus",
    "decision_idempotency_key",
    "next_commands_for",
    "next_commands_for_view",
]
