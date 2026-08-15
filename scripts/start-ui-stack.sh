#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ -s "$HOME/.nvm/nvm.sh" ]; then
  # shellcheck disable=SC1091
  . "$HOME/.nvm/nvm.sh"
  nvm use 22
fi

if [ ! -d web/node_modules ]; then
  npm --prefix web install
fi

pids=()
cleanup() {
  for pid in "${pids[@]}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done
  wait >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

uv run local-agent serve --host 127.0.0.1 --port 8000 &
pids+=("$!")

npm --prefix web run dev -- --host 127.0.0.1 &
pids+=("$!")

echo "FastAPI: http://127.0.0.1:8000"
echo "React:   http://127.0.0.1:5173"
echo "Requires the agent runtime and its dependencies to be running."
wait
