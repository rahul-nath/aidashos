#!/usr/bin/env bash
set -euo pipefail

# Refresh the Workflowy vector store as a durable DBOS workflow:
#   1. sync   - fetch the full account (or --input export) and semantic-chunk it
#   2. import - embed every chunk and store it for `pi` retrieval
# The two steps are checkpointed: if the import fails (e.g. the embedder isn't
# loaded yet) DBOS retries it with backoff, and the sync is never re-run. If the
# retries are exhausted, load the embedder and re-run this script (or just
# `local-agent workflowy-import-chunks data/seed/workflowy_chunks_with_meta.jsonl`).
#
# Requires Postgres up (scripts/start-docker-compose-infra.sh postgres) and llama-server up
# (scripts/start-llama.sh). Set WF_API_KEY to fetch from the API, or pass
# --input <export.json> to chunk a snapshot you already downloaded.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

INPUT=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --input)
      INPUT="${2:?--input needs a path}"
      shift 2
      ;;
    *)
      echo "Usage: refresh-workflowy.sh [--input export.json]" >&2
      exit 2
      ;;
  esac
done

LLAMA_URL="${LOCAL_AGENT_LLAMA_BASE_URL:-http://127.0.0.1:8080}"

# --- preflight -------------------------------------------------------------
if ! docker exec local-agent-postgres pg_isready -U postgres >/dev/null 2>&1; then
  echo "postgres is not running. Start it with: scripts/start-docker-compose-infra.sh postgres" >&2
  exit 1
fi
if ! curl --connect-timeout 2 --max-time 5 -fsS "$LLAMA_URL/models" >/dev/null 2>&1; then
  echo "llama-server not reachable at $LLAMA_URL. Start it with: scripts/start-llama.sh" >&2
  exit 1
fi
if [ -z "$INPUT" ] && [ -z "${WF_API_KEY:-}" ]; then
  echo "Set WF_API_KEY to fetch from the Workflowy API, or pass --input <export.json>." >&2
  exit 1
fi

echo "==> ensuring database schema"
uv run local-agent init-db

echo "==> running durable Workflowy refresh (sync -> import)"
if [ -n "$INPUT" ]; then
  uv run local-agent workflowy-refresh --input "$INPUT"
else
  uv run local-agent workflowy-refresh
fi
