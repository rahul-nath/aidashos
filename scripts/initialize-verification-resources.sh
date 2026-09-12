#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

"$ROOT/scripts/start-verification-database.sh"
exec uv run --offline --no-sync python -m local_first_agent_os.local_verification_setup initialize "$@"
