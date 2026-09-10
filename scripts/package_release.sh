#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Source and output are explicit so an operator checkout is never archived by default.
exec uv run python -m local_first_agent_os.release_packaging "$@"
