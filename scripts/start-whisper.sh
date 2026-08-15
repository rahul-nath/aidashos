#!/usr/bin/env bash
set -euo pipefail

# whisper.cpp server (ASR backend). Runs in the foreground and execs the
# binary, so launchd/KeepAlive can supervise it directly. The active ASR model
# comes from configs/model_registry.toml when an `asr` role is present.
#
# Pass --supervised when launchd owns the process: it skips the "already
# running" early-exit, which would otherwise make KeepAlive hot-loop.

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REGISTRY_PATH="${LOCAL_AGENT_MODEL_REGISTRY_PATH:-${LOCAL_AGENT_CONFIG_DIR:-$PROJECT_ROOT/configs}/model_registry.toml}"

SUPERVISED=false
if [ "${1:-}" = "--supervised" ]; then
  SUPERVISED=true
fi

PYTHON_RUNNER=(python3)
if command -v uv >/dev/null 2>&1; then
  PYTHON_RUNNER=(uv run python)
fi

if [ -f "$REGISTRY_PATH" ]; then
  eval "$(
    cd "$PROJECT_ROOT"
    "${PYTHON_RUNNER[@]}" scripts/whisper_registry_env.py "$REGISTRY_PATH"
  )"
fi

PROJECTS_ROOT="${LOCAL_AGENT_PROJECTS_ROOT:-$HOME/ai_projects}"
PROJECTS_ROOT="${PROJECTS_ROOT/#\~/$HOME}"
WHISPER_CPP_DIR="${LOCAL_AGENT_WHISPER_CPP_DIR:-${WHISPER_CPP_DIR:-$PROJECTS_ROOT/whisper.cpp}}"
WHISPER_CPP_DIR="${WHISPER_CPP_DIR/#\~/$HOME}"
export LOCAL_AGENT_WHISPER_HOST="${LOCAL_AGENT_WHISPER_HOST:-127.0.0.1}"
export LOCAL_AGENT_WHISPER_PORT="${LOCAL_AGENT_WHISPER_PORT:-${REGISTRY_WHISPER_PORT:-8090}}"
export LOCAL_AGENT_WHISPER_BIN_PATH="${LOCAL_AGENT_WHISPER_BIN_PATH:-$WHISPER_CPP_DIR/build/bin/whisper-server}"
export LOCAL_AGENT_WHISPER_MODELS_DIR="${LOCAL_AGENT_WHISPER_MODELS_DIR:-$WHISPER_CPP_DIR/models}"
export LOCAL_AGENT_WHISPER_IDLE_MODEL="${LOCAL_AGENT_WHISPER_IDLE_MODEL:-ggml-base.en.bin}"
export LOCAL_AGENT_WHISPER_THREADS="${LOCAL_AGENT_WHISPER_THREADS:-${REGISTRY_WHISPER_THREADS:-8}}"
export LOCAL_AGENT_WHISPER_BACKEND="${LOCAL_AGENT_WHISPER_BACKEND:-${REGISTRY_WHISPER_BACKEND:-metal}}"
export LOCAL_AGENT_WHISPER_LANGUAGE="${LOCAL_AGENT_WHISPER_LANGUAGE:-${REGISTRY_WHISPER_LANGUAGE:-auto}}"
export LOCAL_AGENT_WHISPER_TRANSLATE="${LOCAL_AGENT_WHISPER_TRANSLATE:-${REGISTRY_WHISPER_TRANSLATE:-false}}"
export LOCAL_AGENT_WHISPER_FLASH_ATTN="${LOCAL_AGENT_WHISPER_FLASH_ATTN:-true}"

MODEL_PATH="${LOCAL_AGENT_WHISPER_MODEL_PATH:-${REGISTRY_WHISPER_MODEL_PATH:-$LOCAL_AGENT_WHISPER_MODELS_DIR/ggml-large-v3-turbo.bin}}"
COREML_PATH="${LOCAL_AGENT_WHISPER_COREML_PATH:-${REGISTRY_WHISPER_COREML_PATH:-}}"
WHISPER_URL="${LOCAL_AGENT_WHISPER_BASE_URL:-${REGISTRY_WHISPER_SERVER_URL:-http://${LOCAL_AGENT_WHISPER_HOST}:${LOCAL_AGENT_WHISPER_PORT}}}"

if [ -z "$COREML_PATH" ] && [[ "$LOCAL_AGENT_WHISPER_BACKEND" == *coreml* ]] && [[ "$MODEL_PATH" == *.bin ]]; then
  COREML_PATH="${MODEL_PATH%.bin}-encoder.mlmodelc"
fi

if [ "$SUPERVISED" = false ] &&
  (curl --connect-timeout 2 --max-time 5 -fsS "$WHISPER_URL/health" >/dev/null 2>&1 || curl --connect-timeout 2 --max-time 5 -fsS "$WHISPER_URL/" >/dev/null 2>&1); then
  echo "whisper-server already running at $WHISPER_URL"
  exit 0
fi

if [ ! -x "$LOCAL_AGENT_WHISPER_BIN_PATH" ]; then
  echo "whisper-server binary not found at $LOCAL_AGENT_WHISPER_BIN_PATH" >&2
  echo "  Build with: (cd $WHISPER_CPP_DIR && cmake -B build -DWHISPER_COREML=1 -DWHISPER_COREML_ALLOW_FALLBACK=1 -DGGML_METAL=ON && cmake --build build -j --config Release)" >&2
  exit 1
fi

CMAKE_CACHE="$WHISPER_CPP_DIR/build/CMakeCache.txt"
if [[ "$LOCAL_AGENT_WHISPER_BACKEND" == *coreml* ]] &&
  [ -f "$CMAKE_CACHE" ] &&
  ! grep -Eq '^WHISPER_COREML:BOOL=(ON|1|TRUE)$' "$CMAKE_CACHE"; then
  echo "whisper.cpp build does not have WHISPER_COREML enabled: $CMAKE_CACHE" >&2
  echo "  Rebuild with: (cd $WHISPER_CPP_DIR && cmake -B build -DWHISPER_COREML=1 -DWHISPER_COREML_ALLOW_FALLBACK=1 -DGGML_METAL=ON && cmake --build build -j --config Release)" >&2
  exit 1
fi

if [[ "$LOCAL_AGENT_WHISPER_BACKEND" == *metal* ]] &&
  [ -f "$CMAKE_CACHE" ] &&
  ! grep -Eq '^GGML_METAL:BOOL=(ON|1|TRUE)$' "$CMAKE_CACHE"; then
  echo "whisper.cpp build does not have GGML_METAL enabled: $CMAKE_CACHE" >&2
  echo "  Rebuild with: (cd $WHISPER_CPP_DIR && cmake -B build -DWHISPER_COREML=1 -DWHISPER_COREML_ALLOW_FALLBACK=1 -DGGML_METAL=ON && cmake --build build -j --config Release)" >&2
  exit 1
fi

if [ ! -f "$MODEL_PATH" ]; then
  echo "Whisper model missing: $MODEL_PATH" >&2
  echo "  Download it with whisper.cpp's download script or set LOCAL_AGENT_WHISPER_MODEL_PATH." >&2
  exit 1
fi

if [[ "$LOCAL_AGENT_WHISPER_BACKEND" == *coreml* ]] && [ ! -d "$COREML_PATH" ]; then
  coreml_name="$(basename "$COREML_PATH")"
  model_name="${coreml_name#ggml-}"
  model_name="${model_name%-encoder.mlmodelc}"
  echo "Core ML encoder sidecar missing: $COREML_PATH" >&2
  echo "  Generate it with: (cd $WHISPER_CPP_DIR && ./models/generate-coreml-model.sh $model_name)" >&2
  exit 1
fi

cmd=(
  "$LOCAL_AGENT_WHISPER_BIN_PATH"
  --model "$MODEL_PATH"
  --host "$LOCAL_AGENT_WHISPER_HOST"
  --port "$LOCAL_AGENT_WHISPER_PORT"
  --threads "$LOCAL_AGENT_WHISPER_THREADS"
  --language "$LOCAL_AGENT_WHISPER_LANGUAGE"
  --print-progress
)

if [ "$LOCAL_AGENT_WHISPER_FLASH_ATTN" = "true" ]; then
  cmd+=(-fa)
else
  cmd+=(-nfa)
fi

if [ "$LOCAL_AGENT_WHISPER_TRANSLATE" = "true" ]; then
  cmd+=(--translate)
fi

exec "${cmd[@]}"
