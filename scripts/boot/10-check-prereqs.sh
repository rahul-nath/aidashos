#!/usr/bin/env bash
set -euo pipefail

# Read-only readiness report for the boot sequence.
#
# bootstrap --check-only answers "are the system dependencies installed"; this
# adds the two machine facts the model downloads depend on, disk and memory,
# and fails only when the boot sequence cannot proceed at all.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

echo "System dependencies:"
"$ROOT/scripts/bootstrap.sh" --check-only

os="$(uname -s)"

# Free disk under $HOME, against what the pins actually weigh.
#
# The size used to be prose here, in the prompts, in the walkthrough, on the
# site, and in the diagram: five copies of a number derived from nothing, which
# is why swapping the qwen quant changed the filename everywhere and the size
# nowhere. Ask Hugging Face what the pinned files weigh instead. Offline, say
# so rather than quote a number that was true for a different quant.
avail_gb="$(df -Pk "$HOME" | awk 'NR==2 {printf "%d", $4 / 1024 / 1024}')"
echo
echo "Disk free under \$HOME: ${avail_gb} GB"

# Only what is still missing, because the question an operator has is how much
# more space they need, not what the full set weighs on a machine that already
# has it.
MODELS_DIR="${LOCAL_AGENT_LLAMA_MODELS_DIR:-$HOME/models}"
still_needed=""
[ -s "$MODELS_DIR/gemma4/model.gguf" ] || still_needed="$still_needed gemma4"
[ -s "$MODELS_DIR/qwen3.8-27b-mtp/model.gguf" ] || still_needed="$still_needed qwen38"

if [ -z "$still_needed" ]; then
  echo "Default models are already downloaded; no further space needed for them."
elif required_gb="$("$ROOT/scripts/model-pin-size.sh" $still_needed 2>/dev/null)" \
  && [ -n "$required_gb" ]; then
  echo "Still to download (${still_needed# }): ${required_gb} GB, measured from the pinned files."
  if [ "$avail_gb" -lt "$required_gb" ]; then
    echo "warning: not enough free space under \$HOME for that."
  fi
else
  echo "Could not reach Hugging Face to size the pinned models; skipping the disk comparison."
fi

if [ "$os" = "Darwin" ]; then
  ram_gb="$(( $(sysctl -n hw.memsize) / 1024 / 1024 / 1024 ))"
else
  ram_gb="$(awk '/MemTotal/ {printf "%d", $2 / 1024 / 1024}' /proc/meminfo)"
fi
echo "Physical memory: ${ram_gb} GB (qwen3.8-27b wants about 20 GB resident; glimmer about the same)"
if [ "$ram_gb" -lt 24 ]; then
  echo "warning: below 24 GB, staff the local tiers on gemma4 only and skip the 27B/30B models."
fi

blocked=0
for core in git uv; do
  if ! command -v "$core" >/dev/null 2>&1; then
    echo "blocked: $core is missing. Run \`make\` first." >&2
    blocked=1
  fi
done
exit "$blocked"
