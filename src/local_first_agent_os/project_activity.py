# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded lifecycle-event page for the operator cockpit.

The cockpit reads two independent lanes. `project_action.ProjectActionSnapshot`
answers "what is true now"; this module answers "what has happened since the
position I last saw". They are separate contracts on purpose: current state is
re-read whole every poll, while history only ever moves forward, and folding
history into the state snapshot would make every refresh carry the whole
execution transcript.

Which lease is current is not decided here. `project_action` already owns that
resolution, so this page reads it from the snapshot rather than growing a
second rule that could disagree.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .coordination.contracts import ListExecutionEvents
from .pow_wow.ledger import run_coordination_command
from .project_action import ProjectActionSnapshot, build_project_action_snapshot
from .settings import Settings, get_settings

PROJECT_ACTIVITY_SCHEMA_VERSION = "project_activity_page.v1"

DEFAULT_ACTIVITY_LIMIT = 50
# `list_execution_events` accepts up to 1000; the cockpit asks for one screen of
# history at a time, so a page stays small enough to render without windowing.
MAX_ACTIVITY_LIMIT = 200


class ActivityCursor(BaseModel):
    """A position in one lease's event stream.

    A sequence is meaningless without the lease it counts within, so the two
    travel together and "no position yet" is the absence of a cursor rather
    than a sequence of zero paired with a missing lease.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    lease_id: str
    after_sequence: int = Field(ge=0)


class ExecutionEventEntry(BaseModel):
    """One durable lifecycle event, as the cockpit renders it.

    A ledger row carries more than a timeline row shows, and it will carry more
    again. `from_ledger_row` names every column this view depends on, so the
    dependency is visible and a new ledger column cannot reach the cockpit by
    accident or break it on arrival.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str
    sequence: int
    occurred_at: datetime
    source: str
    kind: str
    # Required rather than defaulted: every construction supplies it, and a
    # default would publish this field as optional to clients that then have to
    # handle an absence the server never produces.
    payload: dict[str, Any]

    @classmethod
    def from_ledger_row(cls, row: Mapping[str, Any]) -> ExecutionEventEntry:
        return cls(
            event_id=row["event_id"],
            sequence=row["sequence"],
            occurred_at=row["occurred_at"],
            source=row["source"],
            kind=row["kind"],
            payload=row.get("payload") or {},
        )


class ProjectActivityPage(BaseModel):
    """One bounded step through the current lease's lifecycle events."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_serialization_defaults_required=True,
    )

    schema_version: Literal["project_activity_page.v1"] = PROJECT_ACTIVITY_SCHEMA_VERSION
    generated_at: datetime
    project_id: str
    lease_id: str | None
    cursor_reset: bool
    events: list[ExecutionEventEntry]
    has_more: bool
    next_cursor: ActivityCursor | None


class ExecutionEventSource(Protocol):
    def read_execution_events(
        self, lease_id: str, *, after_sequence: int, limit: int
    ) -> Sequence[Mapping[str, Any]]: ...


class LedgerExecutionEventSource:
    """Read the durable event stream through its existing cursor command."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def read_execution_events(
        self, lease_id: str, *, after_sequence: int, limit: int
    ) -> Sequence[Mapping[str, Any]]:
        payload = run_coordination_command(
            ListExecutionEvents(lease_id, after_sequence=after_sequence, limit=limit),
            timeout=15,
            settings=self.settings,
        )
        events = payload.get("events")
        if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
            return []
        return [event for event in events if isinstance(event, Mapping)]


def _current_lease_id(snapshot: ProjectActionSnapshot) -> str | None:
    lease_ids = snapshot.source_ids.get("lease_ids") or []
    return str(lease_ids[0]) if lease_ids else None


def _empty_page(
    project_id: str,
    *,
    generated_at: datetime,
    cursor_reset: bool,
) -> ProjectActivityPage:
    return ProjectActivityPage(
        generated_at=generated_at,
        project_id=project_id,
        lease_id=None,
        cursor_reset=cursor_reset,
        events=[],
        has_more=False,
        next_cursor=None,
    )


def build_project_activity_page(
    project_id: str,
    *,
    cursor: ActivityCursor | None = None,
    limit: int = DEFAULT_ACTIVITY_LIMIT,
    settings: Settings | None = None,
    snapshot: ProjectActionSnapshot | None = None,
    events: ExecutionEventSource | None = None,
    generated_at: datetime | None = None,
) -> ProjectActivityPage:
    if not 1 <= limit <= MAX_ACTIVITY_LIMIT:
        raise ValueError(f"activity limit must be within 1..{MAX_ACTIVITY_LIMIT}, got {limit}")
    settings = settings or get_settings()
    generated_at = generated_at or datetime.now(UTC)
    snapshot = snapshot or build_project_action_snapshot(project_id, settings=settings)
    events = events or LedgerExecutionEventSource(settings)

    lease_id = _current_lease_id(snapshot)
    if lease_id is None:
        # Nothing is executing, so any position the caller held is stale.
        return _empty_page(project_id, generated_at=generated_at, cursor_reset=cursor is not None)

    cursor_reset = cursor is not None and cursor.lease_id != lease_id
    after_sequence = cursor.after_sequence if cursor and not cursor_reset else 0

    # One extra row answers "is there more" without returning the whole history.
    rows = list(
        events.read_execution_events(lease_id, after_sequence=after_sequence, limit=limit + 1)
    )
    has_more = len(rows) > limit
    entries = [ExecutionEventEntry.from_ledger_row(row) for row in rows[:limit]]
    next_sequence = entries[-1].sequence if entries else after_sequence

    return ProjectActivityPage(
        generated_at=generated_at,
        project_id=project_id,
        lease_id=lease_id,
        cursor_reset=cursor_reset,
        events=entries,
        has_more=has_more,
        next_cursor=ActivityCursor(lease_id=lease_id, after_sequence=next_sequence),
    )


__all__ = [
    "DEFAULT_ACTIVITY_LIMIT",
    "MAX_ACTIVITY_LIMIT",
    "PROJECT_ACTIVITY_SCHEMA_VERSION",
    "ActivityCursor",
    "ExecutionEventEntry",
    "ExecutionEventSource",
    "LedgerExecutionEventSource",
    "ProjectActivityPage",
    "build_project_activity_page",
]
