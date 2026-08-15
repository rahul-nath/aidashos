#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from local_first_agent_os.coordination.store import (
    ConnectionLike,
    connect,
    coordination_backend,
    set_root,
)
from local_first_agent_os.pow_wow.ledger import describe_coordination_ledger

DEFAULT_ROOT = Path.home() / ".local-agent" / "coordination" / "local_first_agent_os"
SCHEMA_VERSION = "ledger_inspection.v1"


def _root(value: str | None) -> Path:
    raw = value or os.environ.get("LOCAL_AGENT_COORDINATION_ROOT")
    return Path(raw).expanduser().resolve() if raw else DEFAULT_ROOT


def _dict(row: Any) -> dict[str, Any]:
    return dict(row)


def _json(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _rows(
    conn: ConnectionLike,
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    return [_dict(row) for row in conn.execute(sql, params).fetchall()]


def _in_clause(values: list[str]) -> tuple[str, tuple[str, ...]]:
    return ", ".join("?" for _ in values), tuple(values)


def inspect_ledger(
    *,
    root: Path,
    saga_id: str | None,
    pow_wow_id: str | None,
    limit: int,
) -> dict[str, Any]:
    set_root(str(root))
    backend = coordination_backend()
    with connect() as conn:
        if saga_id:
            sagas = _rows(
                conn,
                "SELECT * FROM sagas WHERE saga_id = ? ORDER BY created_at DESC",
                (saga_id,),
            )
        else:
            sagas = _rows(
                conn,
                "SELECT * FROM sagas ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )

        if pow_wow_id:
            pow_wows = _rows(
                conn,
                "SELECT * FROM pow_wows WHERE pow_wow_id = ? ORDER BY created_at DESC",
                (pow_wow_id,),
            )
        elif saga_id:
            pow_wows = _rows(
                conn,
                "SELECT * FROM pow_wows WHERE saga_id = ? ORDER BY created_at DESC LIMIT ?",
                (saga_id, limit),
            )
        else:
            pow_wows = _rows(
                conn,
                "SELECT * FROM pow_wows ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )

        pow_wow_ids = [str(row["pow_wow_id"]) for row in pow_wows]
        saga_ids = sorted(
            {str(row["saga_id"]) for row in (*sagas, *pow_wows) if row.get("saga_id")}
        )

        tasks_by_pow_wow: dict[str, list[dict[str, Any]]] = {}
        artifact_counts_by_pow_wow: dict[str, dict[str, int]] = {}
        recent_artifacts: list[dict[str, Any]] = []
        if pow_wow_ids:
            placeholders, params = _in_clause(pow_wow_ids)
            tasks = _rows(
                conn,
                (
                    "SELECT task_id, pow_wow_id, saga_id, task_name, status, "
                    "blocked_by_json, retry_count, completed_at "
                    f"FROM saga_tasks WHERE pow_wow_id IN ({placeholders}) "
                    "ORDER BY created_at"
                ),
                params,
            )
            artifacts = _rows(
                conn,
                (
                    "SELECT artifact_id, task_id, pow_wow_id, saga_id, artifact_type, "
                    "schema_version, size_bytes, evaluation_status, created_at "
                    f"FROM task_artifacts WHERE pow_wow_id IN ({placeholders}) "
                    "ORDER BY created_at DESC"
                ),
                params,
            )
            grouped_tasks: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for task in tasks:
                task["blocked_by"] = _json(str(task.pop("blocked_by_json") or "[]"))
                grouped_tasks[str(task["pow_wow_id"])].append(task)
            tasks_by_pow_wow = dict(grouped_tasks)

            counters: dict[str, Counter[str]] = defaultdict(Counter)
            for artifact in artifacts:
                counters[str(artifact["pow_wow_id"])][str(artifact["artifact_type"])] += 1
            artifact_counts_by_pow_wow = {
                pow_id: dict(counter) for pow_id, counter in counters.items()
            }
            recent_artifacts = artifacts[:limit]

        if saga_ids:
            placeholders, params = _in_clause(saga_ids)
            approvals = _rows(
                conn,
                (
                    "SELECT approval_id, saga_id, request_type, status, requested_by, "
                    "resolved_by, created_at, resolved_at, payload_json "
                    f"FROM approval_requests WHERE saga_id IN ({placeholders}) "
                    "ORDER BY created_at DESC LIMIT ?"
                ),
                (*params, limit),
            )
        else:
            approvals = _rows(
                conn,
                (
                    "SELECT approval_id, saga_id, request_type, status, requested_by, "
                    "resolved_by, created_at, resolved_at, payload_json "
                    "FROM approval_requests ORDER BY created_at DESC LIMIT ?"
                ),
                (limit,),
            )
        for approval in approvals:
            approval["payload"] = _json(str(approval.pop("payload_json") or "{}"))

        dispatch_intents = _rows(
            conn,
            (
                "SELECT intent_id, tier, kind, target_project_id, source, status, "
                "claimed_by, error, created_at, claimed_at, completed_at "
                "FROM dispatch_intents ORDER BY created_at DESC LIMIT ?"
            ),
            (limit,),
        )
        execution_leases = _rows(
            conn,
            (
                "SELECT lease_id, intent_id, agent_tier, agent_name, status, "
                "activity_status, last_meaningful_progress_at, "
                "last_meaningful_progress_sequence, progress_assessment_status, "
                "progress_assessment_decision_json, progress_assessment_error, "
                "heartbeat_at, created_at, completed_at "
                "FROM agent_execution_leases ORDER BY created_at DESC LIMIT ?"
            ),
            (limit,),
        )
        for lease in execution_leases:
            lease["progress_assessment_decision"] = _json(
                lease.pop("progress_assessment_decision_json", None)
            )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "root": str(root),
        "ledger_backend": backend,
        "ledger": describe_coordination_ledger(),
        "filters": {
            "saga_id": saga_id,
            "pow_wow_id": pow_wow_id,
            "limit": limit,
        },
        "sagas": sagas,
        "pow_wows": pow_wows,
        "tasks_by_pow_wow": tasks_by_pow_wow,
        "artifact_counts_by_pow_wow": artifact_counts_by_pow_wow,
        "recent_artifacts": recent_artifacts,
        "approval_requests": approvals,
        "dispatch_intents": dispatch_intents,
        "execution_leases": execution_leases,
    }
    payload["report"] = _report(payload)
    return payload


def _report(payload: dict[str, Any]) -> str:
    lines = [f"Ledger: {payload['root']}"]
    sagas = payload.get("sagas") or []
    lines.append(f"Sagas: {len(sagas)}")
    for saga in sagas[:5]:
        goal = str(saga.get("goal") or "").replace("\n", " ")[:96]
        lines.append(
            f"- {saga.get('saga_id')} status={saga.get('status')} "
            f"stage={saga.get('current_stage')} goal={goal}"
        )

    pow_wows = payload.get("pow_wows") or []
    lines.append(f"Pow-wows: {len(pow_wows)}")
    tasks_by_pow_wow = payload.get("tasks_by_pow_wow") or {}
    artifact_counts = payload.get("artifact_counts_by_pow_wow") or {}
    for pow_wow in pow_wows[:5]:
        pow_id = str(pow_wow.get("pow_wow_id"))
        task_counts = Counter(str(task.get("status")) for task in tasks_by_pow_wow.get(pow_id, []))
        lines.append(
            f"- {pow_id} stage={pow_wow.get('stage')} status={pow_wow.get('status')} "
            f"tasks={dict(task_counts)} artifacts={artifact_counts.get(pow_id, {})}"
        )

    approvals = payload.get("approval_requests") or []
    lines.append(f"Approvals: {len(approvals)}")
    for approval in approvals[:5]:
        lines.append(
            f"- {approval.get('approval_id')} type={approval.get('request_type')} "
            f"status={approval.get('status')}"
        )

    intents = payload.get("dispatch_intents") or []
    lines.append(f"Dispatch intents: {len(intents)}")
    for intent in intents[:5]:
        lines.append(
            f"- {intent.get('intent_id')} {intent.get('kind')}/{intent.get('tier')} "
            f"status={intent.get('status')} target={intent.get('target_project_id')}"
        )
    leases = payload.get("execution_leases") or []
    lines.append(f"Execution leases: {len(leases)}")
    for lease in leases[:5]:
        decision = lease.get("progress_assessment_decision") or {}
        recommendation = decision.get("recommendation") if isinstance(decision, dict) else None
        lines.append(
            f"- {lease.get('lease_id')} {lease.get('agent_tier')}/{lease.get('agent_name')} "
            f"ownership={lease.get('status')} activity={lease.get('activity_status')} "
            f"assessment={lease.get('progress_assessment_status')}"
            + (f" recommendation={recommendation}" if recommendation else "")
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect the local agent coordination ledger.")
    parser.add_argument(
        "--root",
        help="Coordination root (used only by the SQLite test adapter)",
    )
    parser.add_argument("--saga-id")
    parser.add_argument("--pow-wow-id")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a text report")
    args = parser.parse_args()
    limit = max(1, min(args.limit, 50))
    payload = inspect_ledger(
        root=_root(args.root),
        saga_id=args.saga_id,
        pow_wow_id=args.pow_wow_id,
        limit=limit,
    )
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(payload["report"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
