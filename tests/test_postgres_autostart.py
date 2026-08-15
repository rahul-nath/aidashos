# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What happens to a test run when the database is not up.

The suite starts its own container, so the interesting cases are the ones where
it cannot: no Docker, a service that refuses to start, a service that starts and
never listens, and a server this repository does not run. Each of those ends the
run, and each of them has to say which one it was, because they are four
different things to go fix.

Which server the suite may start is a property of where the URL came from, not of
the host it names. A developer's own Postgres on 127.0.0.1 is not ours to start,
and `ExternalPostgres` is how that is said.

None of these drive Docker. The subprocess and the connection attempt are the two
seams, and both are substituted, so the file asserts the decision logic rather
than the local machine's container state.
"""

from __future__ import annotations

import subprocess
from typing import Any

import postgres_server
import pytest

_MANAGED = postgres_server.ManagedPostgres(
    url="postgresql://postgres:postgres@127.0.0.1:5433/local_agent",
    compose_service="postgres-test",
)
_EXTERNAL = postgres_server.ExternalPostgres(
    url="postgresql://postgres:postgres@db.internal:5432/local_agent"
)


class _Attempts:
    """Connection attempts that fail a fixed number of times, then succeed."""

    def __init__(self, failures: int) -> None:
        self._remaining = failures
        self.count = 0

    def __call__(self, url: str) -> str | None:
        self.count += 1
        if self._remaining <= 0:
            return None
        self._remaining -= 1
        return "connection refused"


@pytest.fixture()
def never_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(postgres_server.time, "sleep", lambda _seconds: None)


def test_a_reachable_database_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Starting is a repair, so a healthy server must not provoke one."""

    started: list[str] = []
    monkeypatch.setattr(postgres_server, "_connection_error", lambda _url: None)
    monkeypatch.setattr(
        postgres_server, "_start_compose_service", lambda service: started.append(service)
    )

    postgres_server.ensure_running(_MANAGED)

    assert started == []


def test_a_stopped_managed_database_is_started_and_waited_for(
    monkeypatch: pytest.MonkeyPatch, never_sleeps: None
) -> None:
    started: list[str] = []
    attempts = _Attempts(failures=3)
    monkeypatch.setattr(postgres_server, "_connection_error", attempts)
    monkeypatch.setattr(
        postgres_server, "_start_compose_service", lambda service: started.append(service)
    )

    postgres_server.ensure_running(_MANAGED)

    # The service that gets started is the one the source names, so the suite
    # cannot bring up the durable server by starting "postgres" out of habit.
    assert started == ["postgres-test"]
    # One probe to find it down, then polling until it answered.
    assert attempts.count == 4


def test_a_server_this_repository_does_not_run_is_reported_not_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Starting a container would not help, and would look like it might.

    It would bind a local port while the run kept failing against the configured
    server, so the message names the URL instead.
    """

    started: list[str] = []
    monkeypatch.setattr(postgres_server, "_connection_error", lambda _url: "no route to host")
    monkeypatch.setattr(
        postgres_server, "_start_compose_service", lambda service: started.append(service)
    )

    with pytest.raises(postgres_server.PostgresUnavailable) as excinfo:
        postgres_server.ensure_running(_EXTERNAL)

    assert started == []
    assert str(excinfo.value).startswith("Postgres is down")
    assert "db.internal" in str(excinfo.value)


def test_an_external_server_on_this_machine_is_still_not_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old rule read the host and got this case wrong.

    A developer who points LOCAL_AGENT_TEST_DATABASE_URL at their own Postgres on
    127.0.0.1 owns it. Starting the repository's container because the host looked
    local would bind a different port and change nothing about the failure.
    """

    started: list[str] = []
    monkeypatch.setattr(postgres_server, "_connection_error", lambda _url: "connection refused")
    monkeypatch.setattr(
        postgres_server, "_start_compose_service", lambda service: started.append(service)
    )

    with pytest.raises(postgres_server.PostgresUnavailable):
        postgres_server.ensure_running(
            postgres_server.ExternalPostgres(url="postgresql://postgres@127.0.0.1:5555/mine")
        )

    assert started == []


def test_a_machine_without_docker_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(postgres_server, "_connection_error", lambda _url: "connection refused")
    monkeypatch.setattr(postgres_server.shutil, "which", lambda _name: None)

    with pytest.raises(postgres_server.PostgresUnavailable) as excinfo:
        postgres_server.ensure_running(_MANAGED)

    message = str(excinfo.value)
    assert message.startswith("Postgres is down")
    assert "Docker is not installed" in message


def test_a_service_that_refuses_to_start_reports_the_container_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(postgres_server, "_connection_error", lambda _url: "connection refused")
    monkeypatch.setattr(postgres_server.shutil, "which", lambda _name: "/usr/bin/docker")

    def refuse(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Error response from daemon: port is in use"
        )

    monkeypatch.setattr(postgres_server.subprocess, "run", refuse)

    with pytest.raises(postgres_server.PostgresUnavailable) as excinfo:
        postgres_server.ensure_running(_MANAGED)

    message = str(excinfo.value)
    assert message.startswith("Postgres is down")
    assert "port is in use" in message


def test_a_service_that_never_listens_times_out_with_the_last_cause(
    monkeypatch: pytest.MonkeyPatch, never_sleeps: None
) -> None:
    """A container that comes up broken must not hang the run indefinitely."""

    monkeypatch.setattr(postgres_server, "_start_compose_service", lambda _service: None)
    monkeypatch.setattr(
        postgres_server, "_connection_error", lambda _url: "the database system is starting up"
    )
    monkeypatch.setattr(postgres_server, "_STARTUP_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(postgres_server.PostgresUnavailable) as excinfo:
        postgres_server.ensure_running(_MANAGED)

    message = str(excinfo.value)
    assert message.startswith("Postgres is down")
    assert "the database system is starting up" in message
    assert "docker compose logs postgres-test" in message
