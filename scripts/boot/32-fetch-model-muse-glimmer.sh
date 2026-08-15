#!/usr/bin/env bash
set -euo pipefail

# Fetch the optional deliberator model: Muse-Glimmer-30B (+ DFlash draft).
#
# Optional because it is a second roughly 20GB-resident model. On a 36 GB
# machine it must not be loaded at the same time as qwen3.8-27b, so
# 50-set-default-stack.sh writes a LOCAL_AGENT_LLAMA_MODELS_MAX=1 guard into
# .env when both are installed. The pin lives in scripts/download-models.sh.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODELS_DIR="${LOCAL_AGENT_LLAMA_MODELS_DIR:-$HOME/models}"
dir="$MODELS_DIR/glimmer"

FORCE=false
if [ "${1:-}" = "--force" ]; then FORCE=true; fi

if [ "$FORCE" = false ] && [ -s "$dir/model.gguf" ] && [ -s "$dir/draft.gguf" ]; then
  echo "muse-glimmer already present: $dir"
  exit 0
fi

echo "Fetching muse-glimmer into $dir (model weights plus its DFlash draft; this is a large download)"
"$ROOT/scripts/download-models.sh" glimmer
echo
echo "Note: glimmer and qwen3.8-27b are each about 20 GB resident."
echo "50-set-default-stack.sh caps the llama router at one heavyweight model at a time."
