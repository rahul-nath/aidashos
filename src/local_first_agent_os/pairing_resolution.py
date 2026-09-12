# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Ordered pairing decisions, committed once per milestone attempt.

Resolution precedes dispatch. A terminal decision survives a crash between the
two transactions and is replayed before consulting mutable configuration.
Unavailable is terminal too; only a new attempt may try the policy again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from . import pairing_assignment as assignments
from .coordination.store import ConnectionLike, now, rowdict, tx
from .ids import sha256_text
from .pairing_assignment import NoLivePairing, PairingAssignment
from .pairing_lattice import NoPairingAnswered, Pairing, ProbeCache, ProbeResult, ScoredModel
from .staffing import AutoRanked, PairingSelection

_SCHEMA = "pairing_resolution.v1"
type _EventKind = Literal[
    "pairing_requested",
    "availability_rejected",
    "fallback_selected",
    "pairing_resolved",
    "pairing_unavailable",
]


def _pair_payload(pair: Pairing) -> dict[str, Any]:
    return {
        "senior": assignments.model_payload(pair.senior),
        "staff": assignments.model_payload(pair.staff),
        "score": pair.score,
        "cross_vendor": pair.cross_vendor,
    }


@dataclass
class _Journal:
    connection: ConnectionLike
    resolution_id: str
    work_unit_id: str
    milestone_key: str
    attempt: int
    sequence: int = 0

    def append(self, kind: _EventKind, **details: Any) -> None:
        self.sequence += 1
        payload = {
            "schema_version": _SCHEMA,
            "resolution_id": self.resolution_id,
            "work_unit_id": self.work_unit_id,
            "milestone_key": self.milestone_key,
            "attempt": self.attempt,
            "sequence": self.sequence,
            "kind": kind,
            **details,
        }
        self.connection.execute(
            "INSERT INTO ledger_events(event_id, event_type, aggregate_type, aggregate_id, "
            "payload_json, status, attempts, created_at) "
            "VALUES (?, ?, 'pairing_resolution', ?, ?, 'PROCESSED', 0, ?)",
            (
                f"{self.resolution_id}:{self.sequence:06d}",
                kind,
                self.resolution_id,
                json.dumps(payload, sort_keys=True),
                now(),
            ),
        )

    def requested(
        self, selection: PairingSelection, chart_hash: str, candidates: tuple[Pairing, ...]
    ) -> None:
        self.append(
            "pairing_requested",
            selection_policy=selection.to_payload(),
            chart_hash=chart_hash,
            # Preserve the actual search space, not mutable names in TOML.
            candidates=[_pair_payload(pair) for pair in candidates],
            preferred=None if isinstance(selection, AutoRanked) else _pair_payload(candidates[0]),
        )

    def rejected(self, model: ScoredModel, evidence: ProbeResult) -> None:
        self.append(
            "availability_rejected",
            harness=model.harness.value,
            model=model.model,
            reason=evidence.detail or "did not answer",
            evidence_expires_at=evidence.expires_at,
        )

    def fallback_selected(self, pair: Pairing) -> None:
        self.append("fallback_selected", **_pair_payload(pair))


def _terminal(c: ConnectionLike, resolution_id: str) -> PairingAssignment | NoLivePairing | None:
    raw = c.execute(
        "SELECT payload_json FROM ledger_events WHERE aggregate_type='pairing_resolution' "
        "AND aggregate_id=? AND event_type IN ('pairing_resolved', 'pairing_unavailable')",
        (resolution_id,),
    ).fetchall()
    if not raw:
        return None
    if len(raw) != 1:
        raise RuntimeError(f"pairing resolution {resolution_id} has conflicting terminal events")
    payload = json.loads(str(rowdict(raw[0])["payload_json"]))
    if payload.get("schema_version") != _SCHEMA or payload.get("resolution_id") != resolution_id:
        raise ValueError("unsupported or mismatched pairing resolution")
    if payload["kind"] == "pairing_resolved":
        assignment = PairingAssignment.from_payload(payload["assignment"])
        if assignment.resolution_id != resolution_id:
            raise ValueError("resolved assignment names a different resolution")
        return assignment
    if payload["kind"] == "pairing_unavailable":
        return NoLivePairing(
            NoPairingAnswered(tuple(payload["refusals"]), tuple(payload["probed"]))
        )
    raise ValueError("pairing resolution has an invalid terminal event")


def require_resolved_assignment(c: ConnectionLike, assignment: PairingAssignment) -> None:
    """An attachment is a reference to a resolution, not another actor decision."""

    resolved = _terminal(c, str(assignment.resolution_id))
    if not isinstance(resolved, PairingAssignment) or resolved != assignment:
        raise RuntimeError("dispatch assignment does not match its committed pairing resolution")


def resolve_assignment(
    *,
    work_unit_id: str,
    milestone_key: str,
    attempt: int,
    chart_path: Path,
    staffing_path: Path | None = None,
    moment: datetime | None = None,
    cache: ProbeCache | None = None,
) -> PairingAssignment:
    """Commit resolution success or refusal; never hold a lock across inference."""

    if not work_unit_id or not milestone_key or type(attempt) is not int or attempt < 1:
        raise ValueError("pairing resolution requires a WorkUnit, milestone, and positive attempt")
    identity = json.dumps([work_unit_id, milestone_key, attempt])
    digest = sha256_text("pairing-resolution:" + identity)
    resolution_id = f"pr_{digest[:24]}"
    with tx() as c:
        # Hash collisions only serialize unrelated resolutions; event ids and
        # payload identity still distinguish them. Missing schema never falls back.
        c.execute("SET LOCAL lock_timeout = '5s'")
        c.execute("SELECT pg_advisory_xact_lock(?)", (int(digest[:15], 16),))
        result = _terminal(c, resolution_id)
        if result is None:
            journal = _Journal(c, resolution_id, work_unit_id, milestone_key, attempt)
            try:
                selected = assignments.select_assignment(
                    work_unit_id=work_unit_id,
                    milestone_key=milestone_key,
                    attempt=attempt,
                    chart_path=chart_path,
                    staffing_path=staffing_path,
                    moment=moment,
                    cache=cache if cache is not None else ProbeCache(),
                    observer=journal,
                )
                result = replace(selected, resolution_id=resolution_id)
                journal.append("pairing_resolved", assignment=result.to_payload())
            except NoLivePairing as refused:
                result = refused
                journal.append(
                    "pairing_unavailable",
                    refusals=list(refused.outcome.refusals),
                    probed=list(refused.outcome.probed),
                )
    # Raising inside the transaction would erase the refusal we need to replay.
    if isinstance(result, NoLivePairing):
        raise result
    return result
