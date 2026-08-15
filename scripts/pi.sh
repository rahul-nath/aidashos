#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

: "${LOCAL_AGENT_REPO:=$ROOT}"
: "${LOCAL_AGENT_SHELL_SESSION_ID:=shell-$PPID}"
export LOCAL_AGENT_REPO
export LOCAL_AGENT_SHELL_SESSION_ID

if [ -z "${LOCAL_AGENT_TERMINAL_SESSION_STARTED:-}" ]; then
  "$ROOT/scripts/pi_terminal_session.sh" enter "$PPID" "$LOCAL_AGENT_SHELL_SESSION_ID" >/tmp/local-agent-pi-session-enter.log 2>&1 || true
fi

if [ "$#" -eq 0 ]; then
  uv run pi
else
  uv run pi "$@"
fi
