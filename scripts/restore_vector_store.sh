#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SRC="${1:-${LOCAL_AGENT_VECTOR_STORE_DUMP:-}}"
if [ -z "$SRC" ]; then
  echo "Usage: scripts/restore_vector_store.sh /path/to/vector_store_dump.tar.gz" >&2
  exit 2
fi
if [ ! -f "$SRC" ]; then
  echo "Vector-store dump not found: $SRC" >&2
  exit 2
fi
uv run local-agent vector-store-restore "$SRC"
