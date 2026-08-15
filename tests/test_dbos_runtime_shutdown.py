# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A process that launches DBOS must be able to exit.

The runtime's worker threads are non-daemon, so a launched runtime nobody
destroys leaves the interpreter in `Py_Finalize` joining them forever. Observed
live on 2026-08-10: a one-poll enqueue drainer finished its work, printed its
result, and sat in `wait_for_thread_shutdown` until it was sampled and killed -
the 26-minute shape from docs/completed/verification_gate_environment_design.md, seen
from inside. `main` in `coordination/cli.py` owns the stop because it is the
process boundary; this runs that boundary for real.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg
from psycopg import sql

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_a_finite_dispatcher_run_exits_instead_of_joining_dbos_forever(tmp_path: Path) -> None:
    """`run_ledger_dispatcher --max-polls 1` launches DBOS, polls, and must exit.

    The child runs against a scratch DBOS database on the suite's tmpfs server,
    so the launch is real - schema applied, heartbeat and listener threads
    started - and nothing durable is touched. Before the fix this child printed
    its JSON result and never exited; the subprocess timeout below was the
    26-minute hang in miniature.
    """

    admin_url = os.environ["AGENT_COORDINATION_DATABASE_URL"]
    scratch = f"dbos_shutdown_probe_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(scratch)))
    scratch_url = admin_url.rsplit("/", 1)[0] + f"/{scratch}"
    try:
        environment = {
            **os.environ,
            "LOCAL_AGENT_USE_DBOS": "true",
            "LOCAL_AGENT_DBOS_SYSTEM_DATABASE_URL": scratch_url,
            "DBOS_SYSTEM_DATABASE_URL": scratch_url,
        }
        started = time.monotonic()
        proc = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "agent_coordination_mcp.py"),
                "--root",
                str(tmp_path),
                "run_ledger_dispatcher",
                "--max-polls",
                "1",
                "--interval-seconds",
                "0",
            ],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=120,
        )
        elapsed = time.monotonic() - started

        assert proc.returncode == 0, f"dispatcher failed:\n{proc.stdout}{proc.stderr}"
        assert elapsed < 90, f"the dispatcher took {elapsed:.0f}s for one empty poll"
        with psycopg.connect(scratch_url) as connection:
            launched = connection.execute(
                "SELECT count(*) FROM information_schema.schemata WHERE schema_name = 'dbos'"
            ).fetchone()
        assert launched is not None and launched[0] == 1, (
            "DBOS never launched in the child, so this exit proves nothing"
        )
    finally:
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(scratch))
            )


def test_the_boundary_hard_exits_past_a_thread_destroy_cannot_stop() -> None:
    """`exit_code_after_runtime_shutdown` must not join what destroy left alive.

    The crash reconciler's launch recovered parked WorkUnit workflows, and the
    threads hosting them survived `DBOS.destroy`; the process printed its
    result and hung in finalization anyway. The immortal thread here stands in
    for those: a child that calls the boundary must come back with the code the
    boundary was given, immediately, output intact.
    """

    snippet = (
        "import threading, time\n"
        "from local_first_agent_os.dbos_app import exit_code_after_runtime_shutdown\n"
        "threading.Thread(target=lambda: time.sleep(600)).start()\n"
        "print('work finished')\n"
        "raise SystemExit(exit_code_after_runtime_shutdown(3))\n"
    )
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=REPO_ROOT,
        env={**os.environ, "LOCAL_AGENT_USE_DBOS": "false"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    elapsed = time.monotonic() - started

    assert proc.returncode == 3, f"boundary lost the exit code:\n{proc.stdout}{proc.stderr}"
    assert elapsed < 60, f"the boundary joined an immortal thread for {elapsed:.0f}s"
    assert "work finished" in proc.stdout, "output must be flushed before the hard exit"


def test_a_ctrl_cd_server_that_launched_dbos_exits(tmp_path: Path) -> None:
    """`local-agent serve` under SIGINT must exit once uvicorn has shut down.

    The API lifespan launches DBOS and destroys it after yield; the boundary
    after `uvicorn.run` is what guarantees the interpreter still exits when
    destroy leaves a recovered workflow's thread alive. This drives the real
    server: launch against a scratch DBOS database, wait for `/health`, SIGINT,
    and require both a prompt exit and proof the launch actually happened.
    """

    import signal
    import socket
    import urllib.request

    admin_url = os.environ["AGENT_COORDINATION_DATABASE_URL"]
    scratch = f"dbos_serve_probe_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(scratch)))
    scratch_url = admin_url.rsplit("/", 1)[0] + f"/{scratch}"
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = subprocess.Popen(
        [
            str(Path(sys.executable).parent / "local-agent"),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "LOCAL_AGENT_USE_DBOS": "true",
            "LOCAL_AGENT_DBOS_SYSTEM_DATABASE_URL": scratch_url,
            "DBOS_SYSTEM_DATABASE_URL": scratch_url,
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
                    break
            except OSError:
                if server.poll() is not None or time.monotonic() > deadline:
                    server.kill()
                    log = server.stdout.read() if server.stdout else ""
                    raise AssertionError(f"server did not become healthy:\n{log}") from None
                time.sleep(0.5)

        server.send_signal(signal.SIGINT)
        try:
            server.wait(timeout=45)
        except subprocess.TimeoutExpired:
            server.kill()
            raise AssertionError("the server printed its shutdown and never exited") from None
        assert server.returncode == 0, f"server exited {server.returncode}"

        with psycopg.connect(scratch_url) as connection:
            launched = connection.execute(
                "SELECT count(*) FROM information_schema.schemata WHERE schema_name = 'dbos'"
            ).fetchone()
        assert launched is not None and launched[0] == 1, (
            "DBOS never launched in the server, so this exit proves nothing"
        )
    finally:
        if server.poll() is None:
            server.kill()
        with psycopg.connect(admin_url, autocommit=True) as connection:
            connection.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(scratch))
            )
