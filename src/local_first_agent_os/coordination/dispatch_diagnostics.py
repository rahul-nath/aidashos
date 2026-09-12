# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Retained ingress diagnostics owned by the dispatch admission transaction."""

from __future__ import annotations

import json
import uuid

from ..contracts import LedgerEventStatus
from ..dispatch_contracts import DispatchContractEvent, InvalidDispatchReport
from .store import ConnectionLike, now

DISPATCH_CONTRACT_VIOLATION_EVENT = "dispatch_contract_violation"
_DIAGNOSTIC_NAMESPACE = uuid.UUID("6d1d89fc-d1aa-4aa1-8459-79260a5e2a41")


class DispatchDiagnosticPersistenceError(RuntimeError):
    """Failure to retain diagnostic evidence cannot authorize the rejected command."""

    def __init__(self) -> None:
        super().__init__("dispatch contract diagnostic persistence unavailable")


def record_dispatch_contract_violation(
    connection: ConnectionLike,
    *,
    intent_id: str,
    diagnostic: InvalidDispatchReport,
) -> str:
    """Append a safe observation in the caller's transaction, without terminal effects.

    The intent ID must come from the admission owner's ledger row, not from the
    rejected payload. Identical rejected inputs share an event to bound replay
    noise. The ordinary public ledger-event reader exposes this pending event
    even when optional external outbox delivery is disabled.
    """

    if not intent_id.strip():
        raise ValueError("dispatch diagnostic requires its ledger-owned intent ID")
    identity = json.dumps([intent_id, diagnostic.code.value, diagnostic.payload_sha256])
    event_id = str(uuid.uuid5(_DIAGNOSTIC_NAMESPACE, identity))
    payload = DispatchContractEvent(
        intent_id=intent_id,
        code=diagnostic.code,
        payload_sha256=diagnostic.payload_sha256,
    )
    try:
        connection.execute(
            """
            INSERT INTO ledger_events(
                event_id, event_type, aggregate_type, aggregate_id,
                payload_json, status, attempts, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            (
                event_id,
                DISPATCH_CONTRACT_VIOLATION_EVENT,
                "dispatch_intent",
                intent_id,
                payload.model_dump_json(),
                LedgerEventStatus.PENDING.value,
                now(),
            ),
        )
    except Exception:
        raise DispatchDiagnosticPersistenceError() from None
    return event_id
