#!/usr/bin/env bash
set -euo pipefail

# Install the local model runtimes when they are missing.
#
# scripts/install-model-runtimes.sh is the canonical installer (llama.cpp via
# brew or a source build, plus a whisper.cpp build for the ASR role). This
# wrapper only adds the idempotence check so boot.sh can re-run safely.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

FORCE=false
if [ "${1:-}" = "--force" ]; then FORCE=true; fi

if [ "$FORCE" = false ] && command -v llama-server >/dev/null 2>&1; then
  echo "llama-server already on PATH: $(command -v llama-server)"
  llama-server --version 2>&1 | head -1 || true
  whisper_bin="${LOCAL_AGENT_WHISPER_BIN_PATH:-${LOCAL_AGENT_PROJECTS_ROOT:-$HOME/ai_projects}/whisper.cpp/build/bin/whisper-server}"
  if [ -x "$whisper_bin" ]; then
    echo "whisper-server already built: $whisper_bin"
    exit 0
  fi
  echo "llama.cpp is present but whisper.cpp is not built yet; running the installer for it."
fi

"$ROOT/scripts/install-model-runtimes.sh"
