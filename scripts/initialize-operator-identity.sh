#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Base installation already supplied the toolchain. Provision local identity
# without dependency resolution, downloads, or exporting it to this shell.
exec uv run --offline --no-sync python -m local_first_agent_os.operator_credentials initialize
