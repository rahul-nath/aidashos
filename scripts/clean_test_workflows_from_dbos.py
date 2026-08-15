# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Remove test-fixture workflow trees from the durable DBOS system database.

The 2026-08-09 verification-gate incident (docs/completed/verification_gate_environment_design.md)
left the durable execution history holding work-unit workflows that no
coordination row explains: a gate's suite, inheriting the dispatcher's
environment, ran fixture WorkUnits against the production DBOS database.

The rule for condemnation is ledger membership, nothing else: a workflow tree
is removed only when the coordination ledger does not know its work-unit id.
Timestamps and statuses are not consulted, because a real run may fail and a
test may succeed.

Dry run by default; `--apply` deletes, and every deleted row is first written,
whole, to a backup JSON file so the decision is reversible by hand.

Usage, from the repo root so `.env` supplies the URLs:

    uv run python scripts/clean_test_workflows_from_dbos.py            # report
    uv run python scripts/clean_test_workflows_from_dbos.py --apply    # delete
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import psycopg

WORK_UNIT_UUID = re.compile(r"^work-unit:(?P<work_unit_id>[0-9a-f]{32})")

# Workflow names that carry a WorkUnit execution; a bare-UUID workflow under one
# of these names was started by something other than the enqueue outbox, which
# only production uses, so ledger membership decides it like everything else.
WORK_UNIT_WORKFLOW_NAMES = frozenset(
    {"execute_work_unit", "execute_milestone_workflow", "run_phase_workflow"}
)


def _normalized(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _default_url(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return _normalized(value)
    return None


def _real_work_unit_ids(coordination_url: str) -> set[str]:
    with psycopg.connect(coordination_url) as connection:
        return {row[0] for row in connection.execute("SELECT work_unit_id FROM public.work_units")}


def _condemned_workflow_uuids(dbos_url: str, real_ids: set[str]) -> dict[str, str]:
    """Workflow uuid -> name for every tree the ledger does not explain."""

    with psycopg.connect(dbos_url) as connection:
        rows = connection.execute("SELECT workflow_uuid, name FROM dbos.workflow_status").fetchall()
    condemned: dict[str, str] = {}
    for uuid, name in rows:
        match = WORK_UNIT_UUID.match(uuid)
        if match is not None:
            if match.group("work_unit_id") not in real_ids:
                condemned[uuid] = name
        elif name in WORK_UNIT_WORKFLOW_NAMES:
            condemned[uuid] = name
    # Prefix closure: a child enqueued under a condemned root goes with it,
    # whatever its own uuid shape turned out to be.
    roots = tuple(condemned)
    for uuid, name in rows:
        if uuid not in condemned and any(uuid.startswith(f"{root}:") for root in roots):
            condemned[uuid] = name
    return condemned


def _workflow_reference_columns(connection: psycopg.Connection) -> dict[str, list[str]]:
    """dbos table -> columns that hold a workflow uuid, discovered not assumed."""

    rows = connection.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'dbos' "
        "AND column_name IN ('workflow_uuid', 'destination_uuid', 'workflow_id')"
    ).fetchall()
    references: dict[str, list[str]] = {}
    for table, column in rows:
        references.setdefault(table, []).append(column)
    return references


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dbos-url",
        default=_default_url("LOCAL_AGENT_DBOS_SYSTEM_DATABASE_URL", "DBOS_SYSTEM_DATABASE_URL"),
        help="DBOS system database URL (defaults from the environment).",
    )
    parser.add_argument(
        "--coordination-url",
        default=_default_url(
            "AGENT_COORDINATION_DATABASE_URL",
            "LOCAL_AGENT_COORDINATION_DATABASE_URL",
            "LOCAL_AGENT_DATABASE_URL",
        ),
        help="Coordination ledger URL (defaults from the environment).",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path.home() / ".local-agent" / "backups",
        help="Where deleted rows are written before deletion.",
    )
    parser.add_argument("--apply", action="store_true", help="Delete; default is a dry run.")
    args = parser.parse_args()
    if not args.dbos_url or not args.coordination_url:
        parser.error("both --dbos-url and --coordination-url are required, by flag or environment")

    real_ids = _real_work_unit_ids(args.coordination_url)
    condemned = _condemned_workflow_uuids(args.dbos_url, real_ids)
    print(f"ledger knows {len(real_ids)} work units; {len(condemned)} workflow rows condemned")
    by_name: dict[str, int] = {}
    for name in condemned.values():
        by_name[name] = by_name.get(name, 0) + 1
    for name, count in sorted(by_name.items(), key=lambda item: -item[1]):
        print(f"  {name}: {count}")
    if not condemned:
        print("nothing to do")
        return 0
    if not args.apply:
        for uuid in sorted(condemned):
            print(f"  would delete {uuid}")
        print("dry run; pass --apply to delete")
        return 0

    uuids = sorted(condemned)
    backup: dict[str, list[dict[str, object]]] = {}
    with psycopg.connect(args.dbos_url) as connection:
        references = _workflow_reference_columns(connection)
        with connection.transaction():
            for table, columns in sorted(references.items()):
                for column in columns:
                    cursor = connection.execute(
                        f'SELECT * FROM dbos."{table}" WHERE "{column}" = ANY(%s)', (uuids,)
                    )
                    names = [description.name for description in cursor.description or ()]
                    rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
                    if rows:
                        backup.setdefault(table, []).extend(rows)
            args.backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            backup_path = args.backup_dir / f"local_agent_dbos-test-workflows-{stamp}.json"
            backup_path.write_text(
                json.dumps({"condemned": condemned, "rows": backup}, default=str, indent=1),
                encoding="utf-8",
            )
            deleted: dict[str, int] = {}
            # workflow_status last: every other table references it.
            for table, columns in sorted(
                references.items(), key=lambda item: item[0] == "workflow_status"
            ):
                for column in columns:
                    result = connection.execute(
                        f'DELETE FROM dbos."{table}" WHERE "{column}" = ANY(%s)', (uuids,)
                    )
                    deleted[table] = deleted.get(table, 0) + (result.rowcount or 0)
    print(f"backup: {backup_path}")
    for table, count in sorted(deleted.items()):
        print(f"deleted {count} rows from dbos.{table}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
