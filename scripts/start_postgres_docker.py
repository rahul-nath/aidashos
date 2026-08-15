# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bring up the coordination Postgres, and do nothing if it is already up.

The name says "start", and an operator running it twice means "make sure it is
running", not "create a second one". It used to `docker run` unconditionally, so
the second run died on `Conflict. The container name "/local-agent-postgres" is
already in use` with a `CalledProcessError` traceback - a scary failure for a
condition that is actually success, in a script the cockpit runbook opens with.

Three states, three answers: running is a no-op, stopped is a start, absent is a
run. Readiness is checked in all three, because "the container exists" and
"Postgres accepts connections" are different claims and only the second one is
what a caller is about to rely on.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

CONTAINER = "local-agent-postgres"
IMAGE = "pgvector/pgvector:pg16"
READY_TIMEOUT_SECONDS = 30


def _container_state() -> str | None:
    """`running`, `exited`, another docker state, or None when absent."""

    probe = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", CONTAINER],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if probe.returncode != 0:
        return None
    return probe.stdout.strip() or None


def _wait_until_ready(port: str) -> None:
    for _ in range(READY_TIMEOUT_SECONDS):
        result = subprocess.run(
            ["docker", "exec", CONTAINER, "pg_isready", "-U", "postgres"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            print("Postgres ready on port", port)
            return
        time.sleep(1)
    raise SystemExit(
        f"{CONTAINER} did not accept connections within {READY_TIMEOUT_SECONDS} seconds. "
        f"Its logs are: docker logs {CONTAINER}"
    )


def main() -> None:
    port = sys.argv[sys.argv.index("--port") + 1] if "--port" in sys.argv else "5432"
    password = os.environ.get("PGPASSWORD", "postgres")

    state = _container_state()
    if state == "running":
        # Deliberately still checked for readiness rather than returning here: a
        # container can be up while Postgres inside it is not yet accepting
        # connections, and every caller of this script is about to connect.
        print(f"{CONTAINER} is already running.")
    elif state is not None:
        print(f"{CONTAINER} exists and is {state}; starting it.")
        subprocess.run(["docker", "start", CONTAINER], timeout=120, check=True)
    else:
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                f"--name={CONTAINER}",
                f"--env=POSTGRES_PASSWORD={password}",
                "--env=POSTGRES_DB=local_agent",
                "-p",
                f"{port}:5432",
                "-d",
                IMAGE,
            ],
            timeout=120,
            check=True,
        )

    _wait_until_ready(port)


if __name__ == "__main__":
    main()
