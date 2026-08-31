# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One immutable model pairing for one WorkUnit milestone attempt.

The quality lattice orders candidates, while completed execution leases answer
whether a model has a live quota hypothesis.  The selected value is written in
the same transaction as its dispatch intent as a ledger event whose id is
derived from that intent.  That gives an attempt one assignment without adding
a second mutable availability store or a nonce request.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from .coordination.store import ConnectionLike, connect, now, rowdict, tx
from .harness_availability import USAGE_LIMIT_COOLDOWN, parse_quota_reset
from .ids import sha256_text
from .pairing_lattice import (
    NoPairingAnswered,
    Pairing,
    PairingSelected,
    ProbeCache,
    QualityChart,
    ScoredModel,
    load_quality_chart,
    select_live_pairing,
)
from .staffing import Bench, BenchSlot, Harness
from .vocabulary import DispatchTier

PAIRING_ASSIGNMENT_EVENT: Final = "pairing_assignment"
PAIRING_INVALIDATED_EVENT: Final = "pairing_assignment_invalidated"
PAIRING_RELEASED_EVENT: Final = "pairing_assignment_released"
PAIRING_ASSIGNMENT_SCHEMA: Final = "pairing_assignment.v1"
_USAGE_LIMIT: Final = "USAGE_LIMIT"
_PROBE_CACHE = ProbeCache()


class NoLivePairing(RuntimeError):
    """No legal pairing is eligible under the recent real dispatch evidence."""

    def __init__(self, outcome: NoPairingAnswered) -> None:
        self.outcome = outcome
        super().__init__("no live pairing: " + "; ".join(outcome.refusals))


@dataclass(frozen=True)
class PairingAssignment:
    assignment_id: str
    work_unit_id: str
    milestone_key: str
    attempt: int
    chart_hash: str
    pairing: Pairing
    probed: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": PAIRING_ASSIGNMENT_SCHEMA,
            "state": "ASSIGNED",
            "assignment_id": self.assignment_id,
            "work_unit_id": self.work_unit_id,
            "milestone_key": self.milestone_key,
            "attempt": self.attempt,
            "chart_hash": self.chart_hash,
            "score": self.pairing.score,
            "cross_vendor": self.pairing.cross_vendor,
            "senior": _model_payload(self.pairing.senior),
            "staff": _model_payload(self.pairing.staff),
            "selection_evidence": {
                "kind": "recent_real_dispatch_outcomes",
                "probed": list(self.probed),
            },
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> PairingAssignment:
        if payload.get("schema_version") != PAIRING_ASSIGNMENT_SCHEMA:
            raise ValueError("unsupported pairing assignment schema")
        senior = _model_from_payload(_object(payload, "senior"))
        staff = _model_from_payload(_object(payload, "staff"))
        evidence = _object(payload, "selection_evidence")
        pairing = Pairing(
            senior=senior,
            staff=staff,
            score=int(payload["score"]),
            cross_vendor=bool(payload["cross_vendor"]),
        )
        return cls(
            assignment_id=str(payload["assignment_id"]),
            work_unit_id=str(payload["work_unit_id"]),
            milestone_key=str(payload["milestone_key"]),
            attempt=int(payload["attempt"]),
            chart_hash=str(payload["chart_hash"]),
            pairing=pairing,
            probed=tuple(str(item) for item in evidence.get("probed", ())),
        )


def _object(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"pairing assignment {key} must be an object")
    return value


def _model_payload(model: ScoredModel) -> dict[str, Any]:
    return {
        "harness": model.harness.value,
        "model": model.model,
        "quality": model.quality,
        "vendor": model.vendor,
        "reasoning_effort": model.reasoning_effort,
        "anchor": model.anchor,
    }


def _model_from_payload(payload: Mapping[str, Any]) -> ScoredModel:
    return ScoredModel(
        harness=Harness(str(payload["harness"])),
        model=str(payload["model"]),
        quality=int(payload["quality"]),
        vendor=str(payload["vendor"]),
        reasoning_effort=(
            str(payload["reasoning_effort"])
            if payload.get("reasoning_effort") is not None
            else None
        ),
        anchor=bool(payload.get("anchor", False)),
    )


def _failure_text(row: Mapping[str, Any]) -> str:
    values = [str(row.get("error") or "")]
    try:
        result = json.loads(str(row.get("result_json") or "{}"))
    except ValueError:
        result = {}
    if isinstance(result, Mapping):
        capture = result.get("command_capture")
        if isinstance(capture, Mapping):
            values.extend(str(capture.get(key) or "") for key in ("stdout", "stderr"))
    return "\n".join(value for value in values if value)


def recent_dispatch_probe(
    harness: Harness,
    model: str,
    *,
    moment: datetime | None = None,
) -> tuple[bool, str | None]:
    """Treat the latest real dispatch as the model's availability evidence."""

    if harness is Harness.PI:
        return True, "local model has no provider quota"
    with connect(checkout_timeout_seconds=2.0) as c:
        raw = c.execute(
            "SELECT * FROM agent_execution_leases "
            "WHERE agent_name=? AND model=? AND completed_at IS NOT NULL "
            "ORDER BY completed_at DESC LIMIT 1",
            (harness.value, model),
        ).fetchone()
    if raw is None:
        return True, "no recent quota failure"
    row = rowdict(raw)
    if str(row.get("agent_failure") or "") != _USAGE_LIMIT:
        return True, "latest real dispatch was not usage-limited"
    completed_at = float(row.get("completed_at") or 0.0)
    failed_at = datetime.fromtimestamp(completed_at, tz=UTC)
    reset = parse_quota_reset(_failure_text(row), now=failed_at)
    eligible_at = reset or (failed_at + USAGE_LIMIT_COOLDOWN)
    current = moment or datetime.now(UTC)
    if current >= eligible_at:
        return True, f"quota evidence expired at {eligible_at.isoformat()}"
    return False, f"usage limited until {eligible_at.isoformat()}"


def select_assignment(
    *,
    work_unit_id: str,
    milestone_key: str,
    attempt: int,
    chart_path: Path,
    moment: datetime | None = None,
    cache: ProbeCache = _PROBE_CACHE,
) -> PairingAssignment:
    """Select the highest-ranked pair not excluded by real dispatch evidence."""

    chart_text = chart_path.read_text(encoding="utf-8")
    chart: QualityChart = load_quality_chart(chart_path)
    current = moment or datetime.now(UTC)
    selected = select_live_pairing(
        chart,
        lambda harness, model: recent_dispatch_probe(harness, model, moment=current),
        cache=cache,
        now=current.timestamp(),
    )
    if isinstance(selected, NoPairingAnswered):
        raise NoLivePairing(selected)
    assert isinstance(selected, PairingSelected)
    chart_hash = sha256_text(chart_text)
    identity = f"{work_unit_id}:{milestone_key}:{attempt}:{chart_hash}:{selected.pairing.label}"
    return PairingAssignment(
        assignment_id=f"pa_{sha256_text(identity)[:24]}",
        work_unit_id=work_unit_id,
        milestone_key=milestone_key,
        attempt=attempt,
        chart_hash=chart_hash,
        pairing=selected.pairing,
        probed=selected.probed,
    )


def assignment_for_idempotency_key(idempotency_key: str) -> PairingAssignment | None:
    """Return the incumbent assignment before replay chooses again."""

    with tx() as c:
        raw = c.execute(
            "SELECT e.payload_json FROM dispatch_intents d "
            "JOIN ledger_events e ON e.aggregate_id=d.intent_id "
            "WHERE d.idempotency_key=? AND e.event_type=?",
            (idempotency_key, PAIRING_ASSIGNMENT_EVENT),
        ).fetchone()
    if raw is None:
        return None
    return PairingAssignment.from_payload(json.loads(str(rowdict(raw)["payload_json"])))


def record_assignment(
    c: ConnectionLike,
    intent_id: str,
    assignment: PairingAssignment,
    *,
    recorded_at: float,
) -> None:
    """Persist or verify the one assignment attached to this intent."""

    event_id = f"pairing-assignment:{intent_id}"
    encoded = json.dumps(assignment.to_payload(), sort_keys=True)
    c.execute(
        "INSERT INTO ledger_events(event_id, event_type, aggregate_type, aggregate_id, "
        "payload_json, status, attempts, created_at) "
        "VALUES (?, ?, 'dispatch_intent', ?, ?, 'PROCESSED', 0, ?) "
        "ON CONFLICT (event_id) DO NOTHING",
        (event_id, PAIRING_ASSIGNMENT_EVENT, intent_id, encoded, recorded_at),
    )
    raw = c.execute(
        "SELECT payload_json FROM ledger_events WHERE event_id=?", (event_id,)
    ).fetchone()
    if raw is None or str(rowdict(raw)["payload_json"]) != encoded:
        raise RuntimeError(
            f"dispatch intent {intent_id} already carries a different pairing assignment"
        )


def assignment_for_intent(intent_id: str) -> PairingAssignment | None:
    with tx() as c:
        raw = c.execute(
            "SELECT payload_json FROM ledger_events WHERE event_id=? AND event_type=?",
            (f"pairing-assignment:{intent_id}", PAIRING_ASSIGNMENT_EVENT),
        ).fetchone()
    if raw is None:
        return None
    return PairingAssignment.from_payload(json.loads(str(rowdict(raw)["payload_json"])))


def bench_for_assignment(configured: Bench, assignment: PairingAssignment) -> Bench:
    """Bind both judgment seats exactly while preserving every solo seat."""

    resolved = dict(configured)
    for tier, model in (
        (DispatchTier.SENIOR, assignment.pairing.senior),
        (DispatchTier.STAFF, assignment.pairing.staff),
    ):
        incumbent = configured[tier]
        resolved[tier] = BenchSlot(
            harness=model.harness,
            model=model.model,
            capacity=incumbent.capacity,
            reasoning_effort=model.reasoning_effort,
        )
    return resolved


def invalidate_assignment(
    assignment: PairingAssignment,
    *,
    harness: Harness,
    model: str | None,
    reason: str,
) -> None:
    """Append the quota invalidation that permits only the next attempt to re-seat."""

    _PROBE_CACHE.invalidate(harness, model or "")
    payload = {
        "schema_version": "pairing_assignment_transition.v1",
        "state": "INVALIDATED_BY_QUOTA_FAILURE",
        "assignment_id": assignment.assignment_id,
        "harness": harness.value,
        "model": model,
        "reason": reason,
    }
    with tx() as c:
        c.execute(
            "INSERT INTO ledger_events(event_id, event_type, aggregate_type, aggregate_id, "
            "payload_json, status, attempts, created_at) "
            "VALUES (?, ?, 'pairing_assignment', ?, ?, 'PROCESSED', 0, ?) "
            "ON CONFLICT (event_id) DO NOTHING",
            (
                f"pairing-invalidated:{assignment.assignment_id}",
                PAIRING_INVALIDATED_EVENT,
                assignment.assignment_id,
                json.dumps(payload, sort_keys=True),
                now(),
            ),
        )


__all__ = [
    "NoLivePairing",
    "PAIRING_ASSIGNMENT_EVENT",
    "PairingAssignment",
    "assignment_for_idempotency_key",
    "assignment_for_intent",
    "bench_for_assignment",
    "invalidate_assignment",
    "recent_dispatch_probe",
    "record_assignment",
    "select_assignment",
]
