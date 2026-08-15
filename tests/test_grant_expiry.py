# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A grant that stops granting.

`tool_permission_requests` had `status`, `granted_by`, and `resolved_at` and no
expiry, so a GRANTED row was forever. A grant is a statement about a piece of
work and work ends, which makes a surviving grant an authorization nobody
remembers making and nobody will think to remove.

One decision variable per test: whether the row has an expiry, whether that
expiry has passed, what the operator asked for, and which reader is asking.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from local_first_agent_os.capabilities import Capability
from local_first_agent_os.capability_gate import granted_violations_for
from local_first_agent_os.coordination.collaboration import register_agent
from local_first_agent_os.coordination.pow_wows import (
    DEFAULT_GRANT_TTL_SECONDS,
    create_pow_wow,
    grant_tool_permission,
    list_tool_permission_requests,
    request_tool_permission,
)
from local_first_agent_os.coordination.projects import create_saga
from local_first_agent_os.coordination.store import now, tx

AGENT = "claude"
SESSION = "session-expiry-1"
GRANTED_CAPABILITY = Capability.WRITE_REPOSITORY
CLEARED_VIOLATION = "no_file_edit_without_claim"


@pytest.fixture()
def pow_wow(work_unit_ledger: Path) -> str:
    # A real session, because `request_tool_permission` resolves the agent name
    # through one and refuses an id it has never seen.
    register_agent(agent_name=AGENT, session_id=SESSION)
    saga = create_saga(goal="expiry test", budget_tokens=1000, budget_seconds=60)
    return str(
        create_pow_wow(
            saga_id=saga["saga_id"],
            stage="IMPLEMENTATION",
            goal="expiry scope",
            exit_criteria="none",
        )["pow_wow_id"]
    )


def _granted(pow_wow_id: str, *, ttl_seconds: int = DEFAULT_GRANT_TTL_SECONDS) -> str:
    """Request and grant one capability, the way an operator would."""

    requested = request_tool_permission(
        session_id=SESSION,
        tool_name=GRANTED_CAPABILITY.value,
        reason="needed to land the change",
        pow_wow_id=pow_wow_id,
    )
    assert requested["ok"] is True, requested
    granted = grant_tool_permission(requested["request_id"], "operator", ttl_seconds=ttl_seconds)
    assert granted["ok"] is True, granted
    return str(requested["request_id"])


def _expire(request_id: str) -> None:
    """Move the expiry into the past, rather than sleeping through a real TTL."""

    with tx() as c:
        c.execute(
            "UPDATE tool_permission_requests SET expires_at = ? WHERE request_id = ?",
            (now() - 1.0, request_id),
        )


def _agent_name(request_id: str) -> str:
    with tx() as c:
        row = c.execute(
            "SELECT agent_name FROM tool_permission_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    return str(dict(row)["agent_name"])


# Variable 1: whether the grant has expired.
def test_a_live_grant_still_clears_its_violation(pow_wow: str) -> None:
    request_id = _granted(pow_wow)

    assert CLEARED_VIOLATION in granted_violations_for(_agent_name(request_id), pow_wow)


def test_an_expired_grant_clears_nothing(pow_wow: str) -> None:
    """The defect, as an assertion. A GRANTED row used to be forever."""

    request_id = _granted(pow_wow)
    _expire(request_id)

    assert CLEARED_VIOLATION not in granted_violations_for(_agent_name(request_id), pow_wow)


def test_the_audit_list_keeps_a_grant_after_it_expires(pow_wow: str) -> None:
    """Expiry is not deletion.

    The row is the record that an operator once said yes, and erasing it would
    erase the decision along with its effect. What lapses is the authorization,
    which is why the filter is on `expires_at` and not on `status`.
    """

    request_id = _granted(pow_wow)
    _expire(request_id)

    listed = list_tool_permission_requests(status_filter="GRANTED", pow_wow_id=pow_wow)
    assert listed["ok"] is True, listed
    row = next(request for request in listed["requests"] if request["request_id"] == request_id)
    assert row["status"] == "GRANTED"
    assert row["granted_by"] == "operator"


# Variable 2: what the operator asked for.
def test_a_grant_expires_by_default(pow_wow: str) -> None:
    """Forever was never chosen; it was what happened when the column was absent."""

    request_id = _granted(pow_wow)

    with tx() as c:
        row = dict(
            c.execute(
                "SELECT expires_at, created_at FROM tool_permission_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        )
    assert row["expires_at"] is not None
    assert row["expires_at"] - row["created_at"] == pytest.approx(DEFAULT_GRANT_TTL_SECONDS, abs=5)


def test_a_standing_grant_is_available_but_deliberate(pow_wow: str) -> None:
    """`ttl_seconds=0` still means forever, and now says so on purpose."""

    request_id = _granted(pow_wow, ttl_seconds=0)

    with tx() as c:
        row = dict(
            c.execute(
                "SELECT expires_at FROM tool_permission_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        )
    assert row["expires_at"] is None
    assert CLEARED_VIOLATION in granted_violations_for(_agent_name(request_id), pow_wow)


def test_a_negative_ttl_cannot_accidentally_create_a_standing_grant(pow_wow: str) -> None:
    requested = request_tool_permission(
        session_id=SESSION,
        tool_name=GRANTED_CAPABILITY.value,
        reason="needed to land the change",
        pow_wow_id=pow_wow,
    )

    with pytest.raises(ValueError, match="ttl_seconds must be non-negative"):
        grant_tool_permission(requested["request_id"], "operator", ttl_seconds=-1)

    listed = list_tool_permission_requests(pow_wow_id=pow_wow)
    row = next(
        request
        for request in listed["requests"]
        if request["request_id"] == requested["request_id"]
    )
    assert row["status"] == "PENDING"


def test_the_grant_reports_its_own_expiry(pow_wow: str) -> None:
    """Said when the grant is made, not discovered when it lapses."""

    requested = request_tool_permission(
        session_id=SESSION,
        tool_name=GRANTED_CAPABILITY.value,
        reason="needed to land the change",
        pow_wow_id=pow_wow,
    )
    granted = grant_tool_permission(requested["request_id"], "operator")

    assert granted["expires_at"] is not None


def test_a_standing_grant_reports_no_expiry(pow_wow: str) -> None:
    requested = request_tool_permission(
        session_id=SESSION,
        tool_name=GRANTED_CAPABILITY.value,
        reason="needed to land the change",
        pow_wow_id=pow_wow,
    )
    granted = grant_tool_permission(requested["request_id"], "operator", ttl_seconds=0)

    assert granted["expires_at"] is None


# Variable 3: which reader is asking.
def test_a_row_written_before_the_column_existed_still_grants(pow_wow: str) -> None:
    """NULL is a standing grant, which is what every pre-migration row meant.

    An upgrade that silently revoked every existing grant would be a migration
    that changed behaviour while claiming to add a column.
    """

    request_id = _granted(pow_wow)
    with tx() as c:
        c.execute(
            "UPDATE tool_permission_requests SET expires_at = NULL WHERE request_id = ?",
            (request_id,),
        )

    assert CLEARED_VIOLATION in granted_violations_for(_agent_name(request_id), pow_wow)
