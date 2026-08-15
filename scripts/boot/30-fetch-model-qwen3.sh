#!/usr/bin/env bash
set -euo pipefail

# Fetch the default heavyweight local model: Qwen3.8-27B (Q4_K_M + MTP draft).
#
# The pin lives in scripts/download-models.sh, the single source for model
# sources and file names; this wrapper adds the qwen3.x default choice and an
# idempotence check. New qwen3.x variants get added there first, then named
# here.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODELS_DIR="${LOCAL_AGENT_LLAMA_MODELS_DIR:-$HOME/models}"

VARIANT="27b"
FORCE=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --variant) VARIANT="${2:?--variant needs a value}"; shift ;;
    --force) FORCE=true ;;
    -h|--help)
      echo "Usage: $0 [--variant 27b] [--force]"
      exit 0
      ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

case "$VARIANT" in
  27b) name="qwen38"; dir="$MODELS_DIR/qwen3.8-27b-mtp" ;;
  *)
    echo "Unknown qwen3.x variant: $VARIANT" >&2
    echo "Known variants are pinned in scripts/download-models.sh; add one there first." >&2
    exit 2
    ;;
esac

if [ "$FORCE" = false ] && [ -s "$dir/model.gguf" ] && [ -s "$dir/draft.gguf" ]; then
  echo "qwen3.8-$VARIANT already present: $dir"
  exit 0
fi

echo "Fetching qwen3.8-$VARIANT into $dir (model weights; this is a large download)"
"$ROOT/scripts/download-models.sh" "$name"
