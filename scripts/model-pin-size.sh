#!/usr/bin/env bash
set -euo pipefail

# What the pinned model files actually weigh, in whole GB, summed.
#
# Sizes used to be prose in five places and derived from nothing, so changing a
# quant renamed the file everywhere and corrected the size nowhere. There is
# exactly one place a model's repository and filename are written down,
# `scripts/download-models.sh`, so this reads the sizes from there by asking
# Hugging Face rather than restating a number somebody typed.
#
# Usage: ./scripts/model-pin-size.sh [name ...]     (default: gemma4 qwen38)
# Prints a single integer. Exits non-zero without printing when offline, so a
# caller can distinguish "no answer" from "zero".

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOWNLOADER="$ROOT/scripts/download-models.sh"

[ "$#" -gt 0 ] || set -- gemma4 qwen38

# `repo file` pairs for the requested names, taken from the hf_download calls in
# the one file that pins them. Parsing the downloader keeps this honest: a pin
# edited there is measured here without a second edit.
pairs="$(
  awk -v names="$*" '
    BEGIN { split(names, wanted, " "); for (i in wanted) want[wanted[i]] = 1 }
    /^[[:space:]]*[a-z0-9]+\)$/ {
      current = $0
      gsub(/[^a-z0-9]/, "", current)
      active = (current in want)
      next
    }
    active && $1 == "hf_download" { print $2, $3 }
  ' "$DOWNLOADER"
)"

[ -n "$pairs" ] || { echo "no pins matched: $*" >&2; exit 2; }

# Fetched with curl rather than from Python's urllib: the interpreter on a
# stock macOS python.org install has no CA bundle and fails every HTTPS call
# with CERTIFICATE_VERIFY_FAILED, while curl uses the system trust store. Python
# only parses here, which is also what `download-models.sh` and the replacement
# script do.
total_bytes=0
while read -r repo filename; do
  [ -n "$repo" ] || continue
  cache="/tmp/.model-pin-size.$(printf '%s' "$repo" | tr '/' '-').json"
  if [ ! -s "$cache" ]; then
    curl -fsSL --connect-timeout 10 --max-time 30 \
      "https://huggingface.co/api/models/$repo?blobs=true" > "$cache" || {
        rm -f "$cache"
        exit 1
      }
  fi
  size="$(python3 -c '
import json
import sys

with open(sys.argv[1]) as handle:
    payload = json.load(handle)
for sibling in payload.get("siblings", []):
    if sibling.get("rfilename") == sys.argv[2]:
        print(sibling.get("size") or 0)
        break
else:
    print(0)
' "$cache" "$filename")"
  # A pinned filename the repository no longer serves is a real problem, but
  # this script only sizes things; the fetch itself will report it properly.
  [ "$size" -gt 0 ] || exit 1
  total_bytes=$((total_bytes + size))
done <<EOF
$pairs
EOF

python3 -c "print(round($total_bytes / 1e9))"
