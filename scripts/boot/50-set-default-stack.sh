#!/usr/bin/env bash
set -euo pipefail

# Apply the default stack and projects config, and report what it resolves to.
#
# The stack itself is checked in: configs/staffing.toml seats the tiers,
# configs/model_registry.toml maps roles to local models, and
# configs/linked_projects.toml registers target projects. This script wires the
# last mile on a fresh machine: it materializes .env, verifies which registry
# models are actually installed, and writes the one guard the checked-in config
# cannot carry, the llama router's resident-model cap.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODELS_DIR="${LOCAL_AGENT_LLAMA_MODELS_DIR:-$HOME/models}"

if [ ! -f "$ROOT/.env" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "Created .env from .env.example."
else
  echo ".env already exists; left unchanged."
fi

if [ ! -f "$ROOT/configs/linked_projects.toml" ]; then
  echo "blocked: configs/linked_projects.toml is missing." >&2
  echo "The runtime fails closed without it. Restore it from git before dispatching." >&2
  exit 1
fi

echo
echo "Seats (configs/staffing.toml):"
uv run --project "$ROOT" python - "$ROOT/configs/staffing.toml" <<'PY'
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    staffing = tomllib.load(handle)
for tier, slot in staffing.get("bench", {}).items():
    print(f"  {tier:7} -> {slot.get('harness')}:{slot.get('model')} (capacity {slot.get('capacity')})")
PY

echo
echo "Local models (configs/model_registry.toml vs $MODELS_DIR):"
uv run --project "$ROOT" python - "$ROOT/configs/model_registry.toml" <<'PY'
import os
import sys
import tomllib

with open(sys.argv[1], "rb") as handle:
    registry = tomllib.load(handle)
missing_required = []
for alias, spec in registry.get("models", {}).items():
    role = spec.get("role", "?")
    path_key = "gguf_path" if "gguf_path" in spec else "ggml_path"
    raw_path = spec.get(path_key, "")
    path = os.path.expanduser(raw_path)
    present = os.path.isfile(path) and os.path.getsize(path) > 0
    marker = "ok     " if present else "missing"
    print(f"  {marker} {role:16} {spec.get('server_model_name', alias):24} {raw_path}")
    if role == "general" and not present:
        missing_required.append(role)
if missing_required:
    print("\nThe general role has no installed model; the junior tier cannot run.")
    print("Fix: ./scripts/boot/31-fetch-model-gemma4.sh")
    sys.exit(1)
PY

# One llama router loading two roughly 20GB models is the failure the 2026-08-15
# handoff records: glimmer plus qwen3.8 exceed a 36GB machine. Until model
# residency is a scheduled resource, cap the router at one resident model when
# both heavyweights are installed. start-agent-runtime.sh exports .env through
# its dotenv loader, so this line reaches scripts/start-llama.sh.
if [ -s "$MODELS_DIR/glimmer/model.gguf" ] && [ -s "$MODELS_DIR/qwen3.8-27b-mtp/model.gguf" ]; then
  if grep -q '^LOCAL_AGENT_LLAMA_MODELS_MAX=' "$ROOT/.env"; then
    echo
    echo "Resident-model cap already set in .env:"
    grep '^LOCAL_AGENT_LLAMA_MODELS_MAX=' "$ROOT/.env"
  else
    {
      echo ""
      echo "# Both glimmer and qwen3.8-27b are installed (about 20 GB resident each)."
      echo "# Cap the llama router at one loaded model until residency scheduling exists."
      echo "LOCAL_AGENT_LLAMA_MODELS_MAX=1"
    } >> "$ROOT/.env"
    echo
    echo "Wrote LOCAL_AGENT_LLAMA_MODELS_MAX=1 to .env (both heavyweight models are installed)."
  fi
fi

echo
echo "Default stack is in place. Next: ./scripts/boot/60-verify-boot.sh"
