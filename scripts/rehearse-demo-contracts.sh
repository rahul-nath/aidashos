#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/uv-cache}" \
  uv run pytest -q \
  tests/test_pairing_assignment.py \
  tests/test_pow_wow_executor.py::test_governed_pairing_never_swaps_one_provider_mid_attempt \
  tests/test_approval_revocation.py \
  tests/test_refinery_loop.py::test_refinery_rechecks_live_approval_before_fast_forward \
  tests/test_integration_settlement.py \
  tests/test_interrupted_recovery.py \
  tests/test_operator_commands.py \
  tests/test_process_containment.py

echo "Demo contract rehearsal passed: pairing affinity, identity, revocation, landing settlement, interrupted-worktree recovery, and containment."
