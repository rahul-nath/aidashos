#!/usr/bin/env bash
set -euo pipefail

wait_for_app_db() {
  python - <<'PY'
import time

from sqlalchemy.exc import OperationalError

from local_first_agent_os.runtime import get_runtime

deadline = time.time() + 60
last_error: Exception | None = None
while time.time() < deadline:
    try:
        get_runtime().initialize()
        raise SystemExit(0)
    except OperationalError as exc:
        last_error = exc
        time.sleep(2)

raise SystemExit(f"database initialization failed: {last_error}")
PY
}

bootstrap_vector_store() {
  python - <<'PY'
import os
import sys
from pathlib import Path

from local_first_agent_os.contracts import SourceType
from local_first_agent_os.ingress import normalize_scheduled_event
from local_first_agent_os.runtime import get_runtime
from local_first_agent_os.vector_store_io import restore_vector_store
from local_first_agent_os.workflow import WorkflowEngine

dump_env = os.environ.get("LOCAL_AGENT_VECTOR_STORE_DUMP", "").strip()
store_dir = os.environ.get("LOCAL_AGENT_BOOTSTRAP_STORE_DIR", "").strip()
require = os.environ.get("LOCAL_AGENT_REQUIRE_VECTOR_STORE", "false").strip().lower() in {
    "1",
    "true",
    "yes",
}

runtime = get_runtime()
runtime.initialize()

existing = runtime.repository.list_embedding_chunks(None)
if existing:
    print(f"vector_store_bootstrap: {len(existing)} chunks already present, skipping.")
    raise SystemExit(0)

if dump_env:
    dump_path = Path(dump_env)
    if dump_path.exists():
        summary = restore_vector_store(runtime, dump_path)
        print(
            "vector_store_bootstrap: restored "
            f"{summary.chunks_restored} chunks, "
            f"{summary.artifacts_restored} artifacts from {dump_path}"
        )
        raise SystemExit(0)
    print(f"vector_store_bootstrap: requested dump not found at {dump_path}", file=sys.stderr)

if store_dir:
    path = Path(store_dir)
    if not path.exists():
        print(f"vector_store_bootstrap: bootstrap store dir missing: {path}", file=sys.stderr)
        if require:
            raise SystemExit(2)
        raise SystemExit(0)
    event = normalize_scheduled_event(
        source_type=SourceType.MANUAL,
        workspace_id="general",
        event_type="pi.directive",
        payload={"directive": f"/store {path}"},
    )
    result = WorkflowEngine(runtime).directory_embedding(event)
    print(
        f"vector_store_bootstrap: directory_embedding workflow {result.workflow_id} "
        f"finished with status {result.status.value}"
    )
    raise SystemExit(0)

if require:
    print(
        "vector_store_bootstrap: LOCAL_AGENT_REQUIRE_VECTOR_STORE=true and no dump or "
        "bootstrap directory was provided.",
        file=sys.stderr,
    )
    raise SystemExit(2)
print("vector_store_bootstrap: no dump or bootstrap directory configured; continuing empty.")
PY
}

if [ "${LOCAL_AGENT_INIT_DB:-true}" = "true" ] && [ "${1:-}" = "local-agent" ] && [ "${2:-}" = "serve" ]; then
  wait_for_app_db
  if [ "${LOCAL_AGENT_BOOTSTRAP_VECTOR_STORE:-true}" = "true" ]; then
    bootstrap_vector_store
  fi
fi

exec "$@"
