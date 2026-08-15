# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bounded local lifecycle maintenance for leases, daemon logs, and retention.

This is deliberately a janitor, not a recovery worker.  It may terminalize
expired ownership facts, bound local logs, and sweep audit evidence past the
configured retention window, but it never claims dispatch intents, resumes
DBOS workflows, merges Git branches, or deletes worktrees.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .settings import Settings, get_settings


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def bound_log_files(
    log_dir: Path,
    *,
    max_bytes: int,
    retained_tail_bytes: int,
) -> list[dict[str, Any]]:
    """Truncate oversized ``*.log`` files in place and retain a bounded tail.

    In-place truncation matters for launchd: a resident process can keep an
    open descriptor to the same inode.  Renaming that file would make the
    process continue writing to the renamed generation until it restarts.
    """

    if retained_tail_bytes > max_bytes:
        raise ValueError("retained_tail_bytes must not exceed max_bytes")
    if not log_dir.exists():
        return []

    bounded: list[dict[str, Any]] = []
    for path in sorted(log_dir.glob("*.log")):
        if not path.is_file() or path.is_symlink():
            continue
        original_bytes = path.stat().st_size
        if original_bytes <= max_bytes:
            continue
        tail = b""
        if retained_tail_bytes:
            with path.open("rb") as source:
                source.seek(max(0, original_bytes - retained_tail_bytes))
                tail = source.read(retained_tail_bytes)
        with path.open("r+b") as target:
            target.seek(0)
            target.write(tail)
            target.truncate(len(tail))
        bounded.append(
            {
                "path": str(path),
                "original_bytes": original_bytes,
                "retained_bytes": len(tail),
                "reclaimed_bytes": original_bytes - len(tail),
            }
        )
    return bounded


def sweep_session_artifacts(
    export_root: Path,
    *,
    delete: bool,
) -> list[dict[str, Any]]:
    """Report, and optionally reclaim, blobs no live transcript still references.

    The content-addressed store deduplicates, so it is bounded in the number of
    distinct images an operator pastes and unbounded in time: compacting a
    transcript rewrites away the reference and nothing has ever removed the
    blob. This is the same janitorial shape as the log bounding above and the
    ledger sweep below, and it belongs here rather than inside ``gc_ledger``
    because the roots are transcripts on disk rather than rows in a
    transaction.

    Reporting is the default and deleting is opt-in through
    ``LOCAL_AGENT_LIFECYCLE_SWEEP_SESSION_ARTIFACTS``, because a scheduled job
    that silently removes an operator's pasted screenshots should be a decision
    rather than a default. A directory that cannot be swept degrades that one
    entry instead of the run: this is a janitor, and one unreadable session is
    not a reason to skip bounding everything else.
    """

    if not export_root.is_dir():
        return []

    from .session_handoff import sweep_unreferenced_artifacts

    sweeps: list[dict[str, Any]] = []
    for session_dir in sorted(path for path in export_root.iterdir() if path.is_dir()):
        if not (session_dir / "artifacts").is_dir():
            continue
        try:
            result = sweep_unreferenced_artifacts(session_dir, delete=delete)
        except Exception as exc:  # one bad session must not stop the janitor
            sweeps.append(
                {
                    "session_dir": str(session_dir),
                    "status": "DEGRADED",
                    "error_type": type(exc).__name__,
                }
            )
            continue
        sweeps.append(result.model_dump(mode="json"))
    return sweeps


def _write_latest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_lifecycle_maintenance(
    settings: Settings | None = None,
    *,
    gc: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Bound logs, sweep expired leases, and replace one latest-status record.

    Applies ``settings.lifecycle_retention_seconds`` as the retention window,
    so collectable audit evidence older than that window is swept on schedule
    rather than only when an operator remembers to pass ``--retention-seconds``
    by hand.  ``None`` keeps that evidence forever.

    Database outages are represented by one overwritten status file rather
    than an unbounded traceback stream.  The next scheduled run retries.
    """

    settings = settings or get_settings()
    logs = bound_log_files(
        settings.lifecycle_log_dir,
        max_bytes=settings.lifecycle_log_max_bytes,
        retained_tail_bytes=settings.lifecycle_log_retained_tail_bytes,
    )
    if gc is None:
        from .coordination.execution import gc_ledger

        gc = gc_ledger

    report: dict[str, Any] = {
        "schema_version": "lifecycle_maintenance_result.v1",
        "ran_at": _utc_now(),
        "status": "COMPLETED",
        "bounded_logs": logs,
        "retention_seconds": settings.lifecycle_retention_seconds,
        "ledger": None,
        "artifact_sweeps": sweep_session_artifacts(
            settings.session_context_export_dir,
            delete=settings.lifecycle_sweep_session_artifacts,
        ),
    }
    try:
        # The window is the operator's, not this function's.  A janitor that
        # decided its own retention would be a second source of truth for a
        # decision that already has one in Settings.
        report["ledger"] = gc(retention_seconds=settings.lifecycle_retention_seconds)
    except Exception as exc:  # the status record is the bounded failure surface
        report["status"] = "DEGRADED"
        report["ledger"] = {
            "ok": False,
            "error_type": type(exc).__name__,
            "message": "coordination ledger unavailable; retry scheduled",
        }
    _write_latest(settings.lifecycle_maintenance_state_path, report)
    return report
