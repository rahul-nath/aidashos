# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The cockpit's lifecycle-event lane.

Current state and event history are separate lanes with separate contracts.
These tests pin the part that makes the history lane safe to poll forever:
it is bounded, it never restates history the caller already has, and it tells
the caller when the position it was holding no longer belongs to the lease that
is running.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from local_first_agent_os.project_action import (
    ProjectActionKind,
    ProjectActionSnapshot,
    ProjectFacts,
    RuntimeFacts,
)
from local_first_agent_os.project_activity import (
    MAX_ACTIVITY_LIMIT,
    PROJECT_ACTIVITY_SCHEMA_VERSION,
    ActivityCursor,
    ProjectActivityPage,
    build_project_activity_page,
)
from local_first_agent_os.settings import Settings

_PROJECT_ID = "pest_site_factory"
_LEASE = "lease-current"
_OLD_LEASE = "lease-previous"


def _isolated_api_module(runtime, monkeypatch):
    monkeypatch.setenv("LOCAL_AGENT_DATABASE_URL", runtime.settings.database_url)
    monkeypatch.setenv("LOCAL_AGENT_USE_DBOS", "false")
    from local_first_agent_os import api as api_module

    monkeypatch.setattr(api_module, "get_runtime", lambda: runtime)
    monkeypatch.setattr(api_module, "get_settings", lambda: runtime.settings)
    return api_module


def _snapshot(lease_id: str | None) -> ProjectActionSnapshot:
    """The authoritative answer to which lease is current, as the cockpit reads it."""

    return ProjectActionSnapshot(
        generated_at=datetime(2026, 7, 28, 20, 0, tzinfo=UTC),
        freshness_seconds=0,
        action=ProjectActionKind.WORKING,
        summary="Execution is active in the isolated worktree.",
        runtime=RuntimeFacts(
            status="ok",
            application_version="test",
            coordination_backend="postgres",
        ),
        project=ProjectFacts(id=_PROJECT_ID),
        source_ids={"lease_ids": [lease_id] if lease_id else []},
    )


class _RecordingEventSource:
    """A ledger event stream that records exactly what the page asked it for."""

    def __init__(self, events_by_lease: Mapping[str, Sequence[dict[str, Any]]]):
        self.events_by_lease = events_by_lease
        self.requests: list[tuple[str, int, int]] = []

    def read_execution_events(
        self, lease_id: str, *, after_sequence: int, limit: int
    ) -> Sequence[Mapping[str, Any]]:
        self.requests.append((lease_id, after_sequence, limit))
        stream = self.events_by_lease.get(lease_id, ())
        return [event for event in stream if event["sequence"] > after_sequence][:limit]


def _event(sequence: int, *, kind: str = "agent_stdout") -> dict[str, Any]:
    """A row exactly as `execution_event_to_dict` returns it.

    The ledger row is wider than the timeline row. Trimming it here would let
    the page pass on a shape the real ledger never produces.
    """

    return {
        "event_id": f"event-{sequence}",
        "lease_id": _LEASE,
        "sequence": sequence,
        "occurred_at": f"2026-07-28T20:0{sequence % 10}:00+00:00",
        "created_at": f"2026-07-28T20:0{sequence % 10}:01+00:00",
        "source": "supervisor",
        "kind": kind,
        "payload": {"line": f"line {sequence}"},
        "payload_sha256": f"{sequence:064x}",
    }


def _build(
    *,
    lease_id: str | None = _LEASE,
    cursor: ActivityCursor | None = None,
    limit: int = 50,
    events: _RecordingEventSource | None = None,
) -> tuple[ProjectActivityPage, _RecordingEventSource]:
    source = events or _RecordingEventSource({_LEASE: [_event(n) for n in range(1, 4)]})
    page = build_project_activity_page(
        _PROJECT_ID,
        cursor=cursor,
        limit=limit,
        settings=Settings.model_validate({"mock_models": True, "use_dbos": False}),
        snapshot=_snapshot(lease_id),
        events=source,
        generated_at=datetime(2026, 7, 28, 20, 5, tzinfo=UTC),
    )
    return page, source


def test_first_page_starts_at_the_beginning_of_the_current_lease() -> None:
    page, source = _build()

    assert page.schema_version == PROJECT_ACTIVITY_SCHEMA_VERSION
    assert page.lease_id == _LEASE
    assert page.cursor_reset is False
    assert [event.sequence for event in page.events] == [1, 2, 3]
    assert source.requests == [(_LEASE, 0, 51)]


def test_the_returned_cursor_resumes_where_the_page_ended() -> None:
    first, _ = _build()
    assert first.next_cursor == ActivityCursor(lease_id=_LEASE, after_sequence=3)

    source = _RecordingEventSource({_LEASE: [_event(n) for n in range(1, 6)]})
    second, _ = _build(cursor=first.next_cursor, events=source)

    assert [event.sequence for event in second.events] == [4, 5]
    assert second.cursor_reset is False


def test_a_page_is_bounded_and_reports_that_more_remains() -> None:
    source = _RecordingEventSource({_LEASE: [_event(n) for n in range(1, 21)]})

    page, _ = _build(limit=5, events=source)

    assert [event.sequence for event in page.events] == [1, 2, 3, 4, 5]
    assert page.has_more is True
    # One extra row is what makes has_more knowable without reading the rest.
    assert source.requests == [(_LEASE, 0, 6)]


def test_the_last_page_reports_that_nothing_remains() -> None:
    source = _RecordingEventSource({_LEASE: [_event(n) for n in range(1, 4)]})

    page, _ = _build(limit=5, events=source)

    assert page.has_more is False


def test_a_cursor_from_another_lease_is_reset_rather_than_honoured() -> None:
    """A sequence from a previous lease would silently skip the new lease's start."""

    source = _RecordingEventSource({_LEASE: [_event(n) for n in range(1, 4)]})

    page, _ = _build(cursor=ActivityCursor(lease_id=_OLD_LEASE, after_sequence=99), events=source)

    assert page.cursor_reset is True
    assert page.lease_id == _LEASE
    assert [event.sequence for event in page.events] == [1, 2, 3]
    assert source.requests == [(_LEASE, 0, 51)]


def test_no_current_lease_yields_an_empty_page_and_no_ledger_read() -> None:
    source = _RecordingEventSource({_LEASE: [_event(1)]})

    page, _ = _build(
        lease_id=None, cursor=ActivityCursor(lease_id=_LEASE, after_sequence=1), events=source
    )

    assert page.lease_id is None
    assert page.events == []
    assert page.has_more is False
    assert page.next_cursor is None
    assert page.cursor_reset is True
    assert source.requests == []


def test_an_empty_page_keeps_the_caller_where_it_was() -> None:
    """Polling a quiet lease must not rewind the cursor to the start."""

    source = _RecordingEventSource({_LEASE: [_event(1), _event(2)]})

    page, _ = _build(cursor=ActivityCursor(lease_id=_LEASE, after_sequence=2), events=source)

    assert page.events == []
    assert page.next_cursor == ActivityCursor(lease_id=_LEASE, after_sequence=2)


def test_an_out_of_range_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="activity limit"):
        _build(limit=MAX_ACTIVITY_LIMIT + 1)


# ---------------------------------------------------------------------------
# HTTP boundary
# ---------------------------------------------------------------------------


def test_the_activity_endpoint_is_separate_from_the_action_snapshot(runtime, monkeypatch) -> None:
    api_module = _isolated_api_module(runtime, monkeypatch)

    paths = {
        route.path
        for route in api_module.create_app().routes
        if isinstance(route, APIRoute) and route.path.startswith("/projects/{project_id}")
    }

    assert {"/projects/{project_id}/action", "/projects/{project_id}/activity"} <= paths


def test_the_activity_endpoint_rejects_an_unbounded_limit(runtime, monkeypatch) -> None:
    """The bound is enforced by the route, so no caller can ask for all history."""

    api_module = _isolated_api_module(runtime, monkeypatch)
    monkeypatch.setattr(api_module, "launch_dbos", lambda: None)

    with TestClient(api_module.create_app()) as client:
        too_many = client.get(
            f"/projects/{_PROJECT_ID}/activity", params={"limit": MAX_ACTIVITY_LIMIT + 1}
        )
        negative = client.get(f"/projects/{_PROJECT_ID}/activity", params={"after_sequence": -1})

    assert too_many.status_code == 422
    assert negative.status_code == 422
