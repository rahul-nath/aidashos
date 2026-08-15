# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from datetime import datetime
from typing import Any

from ._dbos_runtime import DBOS, SetWorkflowID, dbos_workflow
from .runtime import AppRuntime, get_runtime


@dbos_workflow()
def durable_session_item_entrypoint(payload: dict[str, Any]) -> dict[str, Any]:
    runtime = get_runtime()
    return _append_item(runtime, payload)


def persist_session_item(runtime: AppRuntime, payload: dict[str, Any]) -> dict[str, Any]:
    """Persist one immutable session item, using DBOS when it is active.

    The item ID is also the DBOS workflow ID. If DBOS retries after the domain
    transaction commits but before its step checkpoint, the primary key makes
    the write idempotent.
    """

    if runtime.settings.use_dbos and DBOS is not None and SetWorkflowID is not None:
        from .dbos_app import is_dbos_active, launch_dbos

        launch_dbos()
        if is_dbos_active():
            with SetWorkflowID(f"session-item:{payload['item_id']}"):
                return durable_session_item_entrypoint(payload)
    return _append_item(runtime, payload)


def _append_item(runtime: AppRuntime, payload: dict[str, Any]) -> dict[str, Any]:
    return runtime.repository.append_session_item(
        item_id=str(payload["item_id"]),
        turn_id=str(payload["turn_id"]),
        session_id=str(payload["session_id"]),
        model_id=str(payload["model_id"]),
        ordinal=int(payload["ordinal"]),
        item_type=str(payload["item_type"]),
        role=str(payload["role"]),
        content=str(payload["content"]),
        metadata=dict(payload.get("metadata") or {}),
        created_at=datetime.fromisoformat(str(payload["created_at"])),
    )
