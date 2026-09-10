# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Rehearse retained state against a baseline source and an installed candidate.

Only the repository's disposable localhost:5433 test server is accepted.
No model provider or DBOS workflow is invoked; DBOS recovery needs its own lane.
"""

import argparse
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--baseline-source", type=Path, required=True)
parser.add_argument("--baseline-python", type=Path, default=Path(sys.executable))
parser.add_argument("--candidate-python", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
url = "postgresql://postgres:postgres@127.0.0.1:5433/local_agent"
schema = "release_rehearsal_" + uuid.uuid4().hex[:12]
root = args.output.resolve() / schema
root.mkdir(parents=True, exist_ok=False)
with psycopg.connect(url, autocommit=True) as connection:
    public_tables = connection.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
    ).fetchone()
    assert public_tables is not None and public_tables[0] == 0
    connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
try:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LOCAL_AGENT_", "AGENT_COORDINATION_", "PYTHONPATH"))
    }
    env.update(
        LOCAL_AGENT_USE_DBOS="false",
        AGENT_COORDINATION_BACKEND="postgres",
        AGENT_COORDINATION_DATABASE_URL=url,
        AGENT_COORDINATION_SCHEMA=schema,
        AGENT_COORDINATION_ROOT=str(root),
        REHEARSAL_APP_URL=url.replace("postgresql:", "postgresql+psycopg:")
        + "?options="
        + quote("-csearch_path=" + schema + ",public"),
        OTEL_SDK_DISABLED="true",
    )
    baseline = str(args.baseline_source.resolve())
    for phase, python, source in [
        ("write", str(args.baseline_python.absolute()), baseline + "/src"),
        ("candidate", str(args.candidate_python.absolute()), None),
        ("rollback", str(args.baseline_python.absolute()), baseline + "/src"),
    ]:
        if source:
            env["PYTHONPATH"] = source
        else:
            env.pop("PYTHONPATH", None)
        run = subprocess.run(
            [
                python,
                str(Path(__file__).with_name("release_retained_state_worker.py").resolve()),
                phase,
                baseline + "/configs",
                str(root),
            ],
            env=env,
            cwd=root,
            text=True,
            capture_output=True,
            timeout=45,
        )
        (root / (phase + ".log")).write_text(run.stdout + run.stderr)
        if run.returncode:
            print(phase, run.stderr[-1400:])
            raise SystemExit(run.returncode)
        print(run.stdout.strip())
    print("Evidence:", root)
finally:
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
