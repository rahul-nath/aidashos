#!/usr/bin/env bash
set -euo pipefail

# Fetch the junior-tier model: gemma-4-E4B-it (Q4_K_M + vision projector).
#
# This is the model the system will not run without: the junior tier makes the
# cheap judgment calls about the frontier agents. The pin lives in
# scripts/download-models.sh; this wrapper adds the idempotence check.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODELS_DIR="${LOCAL_AGENT_LLAMA_MODELS_DIR:-$HOME/models}"
dir="$MODELS_DIR/gemma4"

FORCE=false
if [ "${1:-}" = "--force" ]; then FORCE=true; fi

if [ "$FORCE" = false ] && [ -s "$dir/model.gguf" ] && [ -s "$dir/mmproj.gguf" ]; then
  echo "gemma4 already present: $dir"
  exit 0
fi

echo "Fetching gemma4 into $dir (model weights plus its projector)"
"$ROOT/scripts/download-models.sh" gemma4
