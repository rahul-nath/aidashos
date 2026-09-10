# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Disposable release rehearsal worker; never uses configured production state."""

import hashlib
import json
import os
import sys
from pathlib import Path

from local_first_agent_os.coordination.checkpoints import (
    append_execution_event,
    list_execution_events,
)
from local_first_agent_os.coordination.execution import open_execution_lease
from local_first_agent_os.coordination.store import migrate_postgres_schema, set_root
from local_first_agent_os.runtime import build_runtime
from local_first_agent_os.session_memory import SessionMemoryStore
from local_first_agent_os.settings import Settings

mode, config_path, state_dir = sys.argv[1:]
root = Path(state_dir)
set_root(str(root))
runtime = build_runtime(
    Settings.model_validate(
        {
            "database_url": os.environ["REHEARSAL_APP_URL"],
            "config_dir": Path(config_path),
            "artifact_root": root / "artifacts",
            "spool_dir": root / "spool",
            "session_context_export_dir": root / "sessions",
            "mock_models": True,
            "use_dbos": False,
        }
    )
)
memory = SessionMemoryStore(runtime)
if mode == "write":
    memory.append_turn(
        session_id="release-session",
        model_id="release-model",
        user_text="retain this question",
        answer="retain this answer",
        turn_id="release-turn",
    )
    lease = open_execution_lease(
        "release-interrupted-attempt", "release-test-worker", timeout_seconds=30
    )
    lease_id = lease["lease"]["lease_id"]
    (root / "lease_id").write_text(lease_id)
else:
    lease_id = (root / "lease_id").read_text()
    migration = migrate_postgres_schema()
    repeated = migrate_postgres_schema()
    assert repeated["migrated"] is False
payload = {"thread_id": "release-thread"}
result = append_execution_event(
    lease_id,
    1,
    1788480000.0,
    "stdout",
    "thread.started",
    payload,
    hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
)
assert result["ok"], result
items = runtime.repository.list_session_items("release-session", "release-model")
context = memory.get_context("release-session", "release-model").context
assert len(items) == 2
assert "retain this question" in context and "retain this answer" in context
events = list_execution_events(lease_id)
snapshot = {"items": items, "events": events, "context": context}
encoded = json.dumps(snapshot, sort_keys=True, default=str)
if mode == "write":
    (root / "baseline-state.json").write_text(encoded)
else:
    assert encoded == (root / "baseline-state.json").read_text(), (
        "retained state differs from baseline"
    )
    print(
        json.dumps(
            {
                "phase": mode,
                "conversation_items": len(items),
                "event_replay": "idempotent",
                "state_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
                "schema_version": repeated["version"],
                "repeated_migration": "no-op",
            }
        )
    )
