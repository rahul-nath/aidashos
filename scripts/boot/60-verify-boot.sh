#!/usr/bin/env bash
set -uo pipefail

# The boot sequence's exit gate: scripts/first-run-check.sh is the verdict.
#
# first-run-check already knows every readiness fact this repo cares about and
# prints the fixing command for each miss, so this wrapper adds nothing to the
# checks. It exists so the boot sequence ends on the same command an operator
# would run by hand, and so success prints where to go next.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

"$ROOT/scripts/first-run-check.sh"
status=$?

echo
if [ "$status" -eq 0 ]; then
  cat <<'EOF'
Boot complete. The machine can run a governed task.

Start the runtime:
  ./scripts/start-agent-runtime.sh

Drive it from the terminal:
  uv run pi /start /new-project
  uv run pi /approve-most-recent
  uv run pi /dispatch
  uv run pi /ledger

Or attach your own AI tool over MCP:
  Claude Code picks up .mcp.json in this repo automatically.
  Codex and others: see skills/operate-agent-os/SKILL.md.
EOF
else
  echo "Boot is not complete. Each blocked line above prints the command that fixes it."
  echo "Re-run ./scripts/boot/boot.sh after fixing; every stage is idempotent."
fi
exit "$status"
