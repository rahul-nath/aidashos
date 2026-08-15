# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bring the development Postgres up so a test run does not have to be told to.

The suite has no SQLite fallback any more, so a stopped database is no longer a
degraded run: it is no run at all. Starting the container the repository already
declares is cheaper than making every developer remember a command, and it is
safe to do unconditionally because the compose service is idempotent.

Every failure here ends the run, so every message says the same thing first -
Postgres is down - and then the part that differs: whether Docker is missing,
whether the service refused to start, or whether it started and never began
accepting connections. Those are three different things to go fix, which is why
they are three different messages rather than one generic one.

Auto-start only applies to a database this repository declares. A URL an operator
supplied describes infrastructure this repository does not own, so the failure
there is reported and not acted on.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import psycopg

REPO_ROOT = Path(__file__).resolve().parent.parent

_CONNECT_TIMEOUT_SECONDS = 5
_STARTUP_TIMEOUT_SECONDS = 90.0
_POLL_INTERVAL_SECONDS = 0.5
_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql", "postgresql+psycopg"})


class PostgresUnavailable(Exception):
    """The suite cannot reach a database, with the reason an operator can act on."""


@dataclass(frozen=True)
class ManagedPostgres:
    """A server this repository declares in compose, and may therefore start."""

    url: str
    compose_service: str


@dataclass(frozen=True)
class ExternalPostgres:
    """A server someone else runs. Reported when it is down, never started."""

    url: str


# Which of the two a URL is depends on where it came from, not on what host it
# names. `127.0.0.1` is the address of both the repository's own container and a
# Postgres a developer installed themselves, and starting a container to satisfy
# the second one produces a timeout rather than the truth. The caller knows which
# it chose; this module does not have to guess.
PostgresSource = ManagedPostgres | ExternalPostgres


def normalized_postgres_url(url: str) -> str:
    """`url` in the spelling psycopg accepts, or a refusal naming what it got.

    The repository runs several databases - the coordination ledger, the DBOS
    system database, the vector store - and only the first of them is required to
    be Postgres. Pointing the suite at one of the others produces a psycopg
    "missing = in connection info string" a long way from the mistake, so the
    scheme is checked where the URL is chosen.
    """

    scheme = urlsplit(url).scheme
    if scheme not in _POSTGRES_SCHEMES:
        raise PostgresUnavailable(
            "The test suite's coordination ledger must be Postgres, and the "
            f"configured URL is not.\n"
            f"  scheme: {scheme or '(none)'}\n"
            "  fix:    set LOCAL_AGENT_TEST_DATABASE_URL to a postgresql:// URL, "
            "or unset it to use the repository's own container"
        )
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def ensure_running(source: PostgresSource) -> None:
    """Return once `source` accepts connections, starting it if this repo owns it.

    Raises `PostgresUnavailable` with a message naming the actual obstacle.
    """

    reachable = _connection_error(source.url)
    if reachable is None:
        return
    if isinstance(source, ExternalPostgres):
        raise PostgresUnavailable(
            "Postgres is down, and LOCAL_AGENT_TEST_DATABASE_URL points at a server "
            "this repository does not run, so the test suite cannot start it.\n"
            f"  url:   {source.url}\n"
            f"  cause: {reachable}\n"
            "  fix:   start that server, or unset the variable to use the "
            "repository's own test container"
        )
    _start_compose_service(source.compose_service)
    _wait_until_accepting(source)


def _connection_error(url: str) -> str | None:
    """The first line of why the server refused, or None when it did not."""

    try:
        with psycopg.connect(url, connect_timeout=_CONNECT_TIMEOUT_SECONDS):
            return None
    except psycopg.OperationalError as exc:
        return str(exc).strip().splitlines()[0] or "connection refused"


def _start_compose_service(service: str) -> None:
    if shutil.which("docker") is None:
        raise PostgresUnavailable(
            "Postgres is down and Docker is not installed, so the test suite cannot "
            "start it.\n"
            "  fix: install Docker, or start a Postgres yourself and point "
            "LOCAL_AGENT_TEST_DATABASE_URL at it"
        )
    completed = subprocess.run(
        ["docker", "compose", "up", "--detach", service],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise PostgresUnavailable(
            "Postgres is down and the container would not start.\n"
            f"  command: docker compose up --detach {service}\n"
            f"  cause:   {_last_line(completed.stderr) or _last_line(completed.stdout)}"
        )


def _wait_until_accepting(source: ManagedPostgres) -> None:
    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
    last_error = "connection refused"
    while time.monotonic() < deadline:
        error = _connection_error(source.url)
        if error is None:
            return
        last_error = error
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise PostgresUnavailable(
        "Postgres is down: the container started but never began accepting "
        f"connections within {_STARTUP_TIMEOUT_SECONDS:.0f}s.\n"
        f"  cause: {last_error}\n"
        f"  logs:  docker compose logs {source.compose_service}"
    )


def _last_line(output: str) -> str:
    lines = [line for line in output.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""
