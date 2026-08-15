# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What every status token means, and what the operator does about it.

The cockpit used to render RUNNING, BLOCKED, and PENDING as bare tokens, and the
operator repeatedly had to ask an assistant what a status meant and whether it
was theirs to act on. BLOCKED in particular is a correctable failure parked for
the operator - fix the cause, then resume or supersede - and nothing on screen
said so. This module is that missing sentence, once per status, next to the
enums whose members it describes.

The legend is total by construction: ``legend_entries`` refuses a mapping that
misses a member, and the module-level ``STATUS_LEGEND`` is built at import, so
an enum gaining a member without gaining a legend entry stops every process
that imports this package rather than shipping a bare token back to the UI.

It is served over HTTP (``GET /status-legend``) rather than checked into the
web client, because the legend describes the statuses *this server* emits. A
generated TypeScript copy would be correct until the first deploy where server
and bundle differ, and a new status arriving without its explanation is exactly
the regression this module exists to prevent.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from enum import StrEnum
from typing import Final, Literal

from ..contracts import TERMINAL_DISPATCH_INTENT_STATUSES, DispatchIntentStatus
from .lifecycle import (
    TERMINAL_MILESTONE_STATUSES,
    TERMINAL_PHASE_STATUSES,
    TERMINAL_WORK_UNIT_STATUSES,
    MilestoneExecutionStatus,
    PhaseStatus,
    WorkUnitStatus,
)
from .projection import OperatorContract

SCHEMA_VERSION_STATUS_LEGEND: Final = "status_legend.v1"


class IncompleteStatusLegend(RuntimeError):
    """A status enum and its legend disagree about the member set.

    A programmer error, never a runtime condition, so it crashes the import: a
    silently partial legend would render the new member as a bare token, which
    is the exact operator experience this module exists to end.
    """


class StatusLegendEntry(OperatorContract):
    """One status: the token, what it means, and whose move it is."""

    status: str
    meaning: str
    operator_action: str
    terminal: bool


class StatusLegendView(OperatorContract):
    """The whole legend, one section per status vocabulary the cockpit renders."""

    schema_version: Literal["status_legend.v1"] = SCHEMA_VERSION_STATUS_LEGEND
    work_unit: tuple[StatusLegendEntry, ...]
    phase: tuple[StatusLegendEntry, ...]
    milestone: tuple[StatusLegendEntry, ...]
    dispatch: tuple[StatusLegendEntry, ...]


def legend_entries[E: StrEnum](
    enum: type[E],
    entries: Mapping[E, tuple[str, str]],
    *,
    terminal: Collection[E],
) -> tuple[StatusLegendEntry, ...]:
    """Entries for every member of ``enum``, in declaration order, or a refusal.

    Both directions are checked. A missing member is the obvious failure; a
    foreign key is the copy-paste between two of the four mappings below that a
    keyed-by-member dict would otherwise absorb without complaint.
    """

    members = frozenset(enum)
    missing = [member.value for member in enum if member not in entries]
    foreign = [key.value for key in entries if key not in members]
    if missing or foreign:
        raise IncompleteStatusLegend(
            f"legend for {enum.__name__} is not the enum: "
            f"missing={missing or None} foreign={foreign or None}"
        )
    return tuple(
        StatusLegendEntry(
            status=member.value,
            meaning=entries[member][0],
            operator_action=entries[member][1],
            terminal=member in terminal,
        )
        for member in enum
    )


_WORK_UNIT: Final[dict[WorkUnitStatus, tuple[str, str]]] = {
    WorkUnitStatus.DRAFT: (
        "The DesignDoc exists but has not been compiled into an executable plan.",
        "Compile the document, then start the WorkUnit.",
    ),
    WorkUnitStatus.COMPILED: (
        "The plan is compiled and hashed but has not been started.",
        "Start the WorkUnit when you want it to run.",
    ),
    WorkUnitStatus.QUEUED: (
        "Accepted for execution and waiting for the runtime to pick it up.",
        "No action; the runtime starts it. If it sits here, check that the runtime is up.",
    ),
    WorkUnitStatus.RUNNING: (
        "The root workflow is executing milestones.",
        "No action; watch the milestones for BLOCKED or WAITING_FOR_OPERATOR.",
    ),
    WorkUnitStatus.WAITING_FOR_OPERATOR: (
        "Execution is paused on a decision only you can make.",
        "Answer the pending decision in the 'Waiting on you' panel.",
    ),
    WorkUnitStatus.BLOCKED: (
        "A correctable failure parked the work for you; nothing moves until you act.",
        "Fix the recorded cause, then resume the WorkUnit - or supersede it with a new one.",
    ),
    WorkUnitStatus.CANCELLING: (
        "Cancellation was requested and the stop cascade has not finished.",
        "Wait for CANCELLED; if it lingers, check the cancel result for refused stop targets.",
    ),
    WorkUnitStatus.SUCCEEDED: (
        "Every phase completed and the required evidence was recorded.",
        "None; the work is done.",
    ),
    WorkUnitStatus.FAILED: (
        "The work ended without completing and will not retry itself.",
        "Read the failure summary; supersede with a new WorkUnit if the work is still wanted.",
    ),
    WorkUnitStatus.CANCELLED: (
        "An operator stopped the work before it finished.",
        "None; supersede with a new WorkUnit to try again.",
    ),
    WorkUnitStatus.SUPERSEDED: (
        "A newer WorkUnit replaced this one; it is kept as history.",
        "None; follow the WorkUnit that replaced it.",
    ),
}

_PHASE: Final[dict[PhaseStatus, tuple[str, str]]] = {
    PhaseStatus.PENDING: (
        "The lifecycle has not reached this phase yet.",
        "No action; earlier phases run first.",
    ),
    PhaseStatus.RUNNING: (
        "This is the current phase; its milestones are executing.",
        "No action; watch its milestones.",
    ),
    PhaseStatus.SUCCEEDED: (
        "Every milestone in this phase completed.",
        "None.",
    ),
    PhaseStatus.SKIPPED: (
        "The document put no milestones in this phase; it holds its place in the fixed lifecycle.",
        "None; an empty phase is normal, not an error.",
    ),
    PhaseStatus.BLOCKED: (
        "A milestone in this phase stopped on a correctable failure.",
        "Find the BLOCKED milestone in the table and clear it.",
    ),
    PhaseStatus.FAILED: (
        "A milestone in this phase failed, so the phase cannot complete.",
        "Read that milestone's failure; supersede the WorkUnit if the work is still wanted.",
    ),
    PhaseStatus.CANCELLED: (
        "Cancellation stopped this phase before it could finish.",
        "None.",
    ),
}

_MILESTONE: Final[dict[MilestoneExecutionStatus, tuple[str, str]]] = {
    MilestoneExecutionStatus.PENDING: (
        "Waiting for its dependencies; not yet eligible to run.",
        "No action; it becomes READY when its dependencies finish.",
    ),
    MilestoneExecutionStatus.READY: (
        "Dependencies are satisfied; the scheduler will start it.",
        "No action; if it sits here, the WorkUnit itself is probably parked.",
    ),
    MilestoneExecutionStatus.RUNNING: (
        "An executor owns it. The dispatch state says whether an agent is actually "
        "working or the dispatch is still waiting for a claimant.",
        "No action while the dispatch is active; if its intent stays PENDING, "
        "check that a dispatcher is running.",
    ),
    MilestoneExecutionStatus.WAITING_FOR_OPERATOR: (
        "Paused on an approval or answer only you can give.",
        "Answer the decision in the 'Waiting on you' panel.",
    ),
    MilestoneExecutionStatus.BLOCKED: (
        "A correctable failure parked this milestone for you: the run stopped, and "
        "the cause and evidence are recorded on this row.",
        "Read the failure and evidence, fix the cause, then resume the WorkUnit - or supersede it.",
    ),
    MilestoneExecutionStatus.SUCCEEDED: (
        "Completed with its required evidence recorded.",
        "None.",
    ),
    MilestoneExecutionStatus.FAILED: (
        "Ended without succeeding and will not be retried in this WorkUnit.",
        "Supersede the WorkUnit if the work is still wanted.",
    ),
    MilestoneExecutionStatus.SKIPPED: (
        "The compiled plan did not require this milestone in this run.",
        "None.",
    ),
    MilestoneExecutionStatus.CANCELLED: (
        "Cancellation stopped it before it finished.",
        "None.",
    ),
}

_DISPATCH: Final[dict[DispatchIntentStatus, tuple[str, str]]] = {
    DispatchIntentStatus.PENDING: (
        "Queued for dispatch; no agent has claimed it yet.",
        "If it stays PENDING, nothing is draining the queue; start or check the dispatcher.",
    ),
    DispatchIntentStatus.CLAIMED: (
        "A dispatcher claimed it and is preparing the agent.",
        "No action.",
    ),
    DispatchIntentStatus.IN_PROGRESS: (
        "An agent is actively working on it.",
        "No action; wait for it to settle.",
    ),
    DispatchIntentStatus.CHECKPOINT_REVIEW: (
        "The agent stopped at a checkpoint and waits for review before continuing.",
        "Review the checkpoint, then let it continue or fail it.",
    ),
    DispatchIntentStatus.PAUSED: (
        "Deliberately paused; it will not move again until resumed.",
        "Resume the intent when you want the work to continue.",
    ),
    DispatchIntentStatus.DONE: (
        "The agent finished and reported its result.",
        "None; the milestone reads the result.",
    ),
    DispatchIntentStatus.FAILED: (
        "The run failed; what the agent reported is kept on the intent and as failure evidence.",
        "Read the failure evidence, fix the cause, then resume the WorkUnit.",
    ),
    DispatchIntentStatus.CANCELED: (
        "The intent was cancelled before completion.",
        "None.",
    ),
    DispatchIntentStatus.SUPERSEDED: (
        "A newer intent replaced this one.",
        "None; follow the replacement.",
    ),
}

STATUS_LEGEND: Final[StatusLegendView] = StatusLegendView(
    work_unit=legend_entries(WorkUnitStatus, _WORK_UNIT, terminal=TERMINAL_WORK_UNIT_STATUSES),
    phase=legend_entries(PhaseStatus, _PHASE, terminal=TERMINAL_PHASE_STATUSES),
    milestone=legend_entries(
        MilestoneExecutionStatus, _MILESTONE, terminal=TERMINAL_MILESTONE_STATUSES
    ),
    dispatch=legend_entries(
        DispatchIntentStatus, _DISPATCH, terminal=TERMINAL_DISPATCH_INTENT_STATUSES
    ),
)

__all__ = [
    "SCHEMA_VERSION_STATUS_LEGEND",
    "STATUS_LEGEND",
    "IncompleteStatusLegend",
    "StatusLegendEntry",
    "StatusLegendView",
    "legend_entries",
]
