#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

MODELS_DIR="${LOCAL_AGENT_LLAMA_MODELS_DIR:-$HOME/models}"
MODELS_DIR="${MODELS_DIR/#\~/$HOME}"
HOST="${LOCAL_AGENT_LLAMA_HOST:-127.0.0.1}"
# The port comes from LOCAL_AGENT_LLAMA_BASE_URL when it is set, because that is
# the one the application connects to. Setting only the model's URL used to move
# the client while leaving the scripts starting and stopping 8080.
_llama_port_from_base_url() {
  local url="${LOCAL_AGENT_LLAMA_BASE_URL:-}"
  [ -n "$url" ] || return 1
  local tail="${url##*:}"
  local port="${tail%%/*}"
  [[ "$port" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "$port"
}
PORT="${LOCAL_AGENT_LLAMA_PORT:-$(_llama_port_from_base_url || echo 8080)}"
MODELS_MAX="${LOCAL_AGENT_LLAMA_MODELS_MAX:-}"
GPU_LAYERS="${LOCAL_AGENT_LLAMA_N_GPU_LAYERS:-999}"
CACHE_TYPE_K="${LOCAL_AGENT_LLAMA_CACHE_TYPE_K:-q8_0}"
CACHE_TYPE_V="${LOCAL_AGENT_LLAMA_CACHE_TYPE_V:-q8_0}"
REGISTRY_PATH="${LOCAL_AGENT_MODEL_REGISTRY_PATH:-${LOCAL_AGENT_CONFIG_DIR:-$PROJECT_ROOT/configs}/model_registry.toml}"
PRESET_PATH="${LOCAL_AGENT_LLAMA_PRESET_PATH:-$PROJECT_ROOT/.local_agent/run/llama_presets.ini}"

if ! command -v llama-server >/dev/null 2>&1; then
  echo "llama-server not found on PATH. Install llama.cpp first." >&2
  exit 127
fi

if [ ! -d "$MODELS_DIR" ]; then
  echo "Model directory does not exist: $MODELS_DIR" >&2
  exit 2
fi

mkdir -p "$(dirname "$PRESET_PATH")"
( cd "$PROJECT_ROOT" && uv run python scripts/gen_llama_presets.py "$REGISTRY_PATH" "$PRESET_PATH" )

existing_pids=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)
if [ -n "$existing_pids" ]; then
  for pid in $existing_pids; do
    name=$(ps -o comm= -p "$pid" 2>/dev/null || echo "?")
    if [[ "$name" == *llama-server* ]]; then
      echo "Killing stale llama-server (pid $pid) bound to $HOST:$PORT"
      kill "$pid" 2>/dev/null || true
    else
      echo "Port $PORT is in use by $name (pid $pid). Refusing to kill non-llama-server process." >&2
      exit 3
    fi
  done
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    sleep 0.3
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1 || break
  done
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "Port $PORT still in use after SIGTERM; sending SIGKILL." >&2
    kill -9 $existing_pids 2>/dev/null || true
    sleep 0.5
  fi
fi

cmd=(
  llama-server
  --host "$HOST" \
  --port "$PORT" \
  --models-dir "$MODELS_DIR" \
  --models-preset "$PRESET_PATH" \
  --no-webui \
  --cache-type-k "$CACHE_TYPE_K" \
  --cache-type-v "$CACHE_TYPE_V" \
  --flash-attn on \
  --threads 8 \
  --cont-batching \
  -ngl "$GPU_LAYERS"
)

# if [ -n "$MODELS_MAX" ]; then
#   cmd+=(--models-max "$MODELS_MAX")
# fi

exec "${cmd[@]}"
