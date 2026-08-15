#!/usr/bin/env bash
set -euo pipefail

# The boot sequence: everything after `make`, in dependency order.
#
# `make` (scripts/bootstrap.sh) ends with a working uv environment, Docker
# Postgres, and schemas. This script takes the machine the rest of the way to a
# runnable stack: llama.cpp, model weights, frontier subscriptions, and the
# default stack config, then verifies the result with first-run-check.
#
# Every stage is its own script in scripts/boot/ and is idempotent, so this
# orchestrator is re-runnable and any stage can be run alone. An AI agent can
# drive the same sequence: docs/onboarding/BOOT_PROMPT.md is the prompt that
# tells one to.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BOOT="$ROOT/scripts/boot"

WITH_GLIMMER=false
SKIP_MODELS=false
SKIP_LOGINS=false
FORCE_MODELS=false

usage() {
  cat <<'EOF'
Usage: ./scripts/boot/boot.sh [options]

  --with-glimmer   Also fetch the optional muse-glimmer deliberator (about 21 GB).
  --skip-models    Skip model downloads (llama.cpp install still runs).
  --skip-logins    Skip the Anthropic and ChatGPT subscription sign-ins.
  --force-models   Re-download model files even when they already exist.
  -h, --help       Show this help.

Stages, in order:
  10-check-prereqs        20-install-llama-cpp
  30-fetch-model-qwen3    31-fetch-model-gemma4    [32-fetch-model-muse-glimmer]
  40-login-anthropic      41-login-chatgpt
  50-set-default-stack    60-verify-boot

Run `make` first. This script assumes bootstrap has already succeeded.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --with-glimmer) WITH_GLIMMER=true ;;
    --skip-models) SKIP_MODELS=true ;;
    --skip-logins) SKIP_LOGINS=true ;;
    --force-models) FORCE_MODELS=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

stage() {
  printf '\n\033[1m== boot %s ==\033[0m\n' "$1"
}

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is missing, which means bootstrap has not run. Run \`make\` first." >&2
  exit 1
fi

stage "10-check-prereqs"
"$BOOT/10-check-prereqs.sh"

stage "20-install-llama-cpp"
"$BOOT/20-install-llama-cpp.sh"

if [ "$SKIP_MODELS" = false ]; then
  force_flag=()
  if [ "$FORCE_MODELS" = true ]; then force_flag=(--force); fi
  stage "30-fetch-model-qwen3"
  "$BOOT/30-fetch-model-qwen3.sh" "${force_flag[@]}"
  stage "31-fetch-model-gemma4"
  "$BOOT/31-fetch-model-gemma4.sh" "${force_flag[@]}"
  if [ "$WITH_GLIMMER" = true ]; then
    stage "32-fetch-model-muse-glimmer"
    "$BOOT/32-fetch-model-muse-glimmer.sh" "${force_flag[@]}"
  fi
else
  echo "Skipping model downloads (--skip-models)."
fi

if [ "$SKIP_LOGINS" = false ]; then
  stage "40-login-anthropic"
  "$BOOT/40-login-anthropic.sh" --install
  stage "41-login-chatgpt"
  "$BOOT/41-login-chatgpt.sh" --install
else
  echo "Skipping subscription sign-ins (--skip-logins)."
fi

stage "50-set-default-stack"
"$BOOT/50-set-default-stack.sh"

stage "60-verify-boot"
"$BOOT/60-verify-boot.sh"
