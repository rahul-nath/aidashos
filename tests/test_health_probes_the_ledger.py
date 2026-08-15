# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`/health` must be able to say no.

It reported the database it was *configured* with and called that ok, so on
2026-08-11 it answered `{"status": "ok"}` for the whole of a Postgres outage
while every ledger query timed out. The operator watching the cockpit saw
green, and a scripted wait-for-healthy took the same green as proof the stack
had come back and moved on.

That is worse than having no check. An endpoint nobody trusts is ignored; one
that cannot fail is believed. These pin that it now answers from a probe, and
that the failure reaches a caller reading only the status line.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from local_first_agent_os.api import create_app


@pytest.fixture()
def served_app(runtime, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr("local_first_agent_os.api.get_runtime", lambda: runtime)
    monkeypatch.setattr("local_first_agent_os.api.launch_dbos", lambda: None)
    return TestClient(create_app(), raise_server_exceptions=False)


def test_health_is_ok_when_the_ledger_answers(served_app: TestClient) -> None:
    response = served_app.get("/health")
    body = response.json()

    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["ledger"] == {"reachable": True, "error": None}


def test_health_is_degraded_when_the_ledger_is_unreachable(
    served_app: TestClient, runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case that went unreported for the length of an outage.

    The connection is broken at the engine rather than by stopping a server, so
    the test states the condition it cares about - the ledger does not answer -
    without depending on which failure a particular host produces for it.
    """

    def refuse() -> None:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(runtime.database.engine, "connect", lambda: refuse())

    response = served_app.get("/health")
    body = response.json()

    assert body["status"] == "degraded"
    assert body["ledger"]["reachable"] is False
    assert "connection refused" in body["ledger"]["error"]
    # The status line carries it too: `curl -f`, a Compose healthcheck and a
    # Kubernetes probe never read the body, and the outage they were told to
    # watch for is exactly this one.
    assert response.status_code == 503


def test_a_degraded_health_still_names_the_database_and_posture(
    served_app: TestClient, runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Degraded is when an operator most needs to know which database this is.

    Reporting only the failure would answer "it is broken" while withholding
    "and here is the one it was pointed at", which is the first thing anyone
    asks next.
    """

    def refuse() -> None:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    monkeypatch.setattr(runtime.database.engine, "connect", lambda: refuse())

    body = served_app.get("/health").json()

    assert body["database"]["database"]
    assert body["access_posture"]
