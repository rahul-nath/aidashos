#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DIRECTORY="${1:-${LOCAL_AGENT_STORE_DIRECTORY:-}}"
if [ -z "$DIRECTORY" ]; then
  echo "No vector-store dump or local directory was provided; refusing to initialize an empty store." >&2
  echo "Usage: scripts/init_vector_store.sh /path/to/directory" >&2
  exit 2
fi

if [ ! -d "$DIRECTORY" ]; then
  echo "Directory does not exist: $DIRECTORY" >&2
  exit 2
fi

uv run local-agent pi "/start /store $DIRECTORY"
