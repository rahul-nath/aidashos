#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OUT="${1:-${LOCAL_AGENT_VECTOR_STORE_DUMP:-.local_agent/vector_store_dump.tar.gz}}"
mkdir -p "$(dirname "$OUT")"
uv run local-agent vector-store-dump "$OUT"
echo "Wrote vector store dump to $OUT"
