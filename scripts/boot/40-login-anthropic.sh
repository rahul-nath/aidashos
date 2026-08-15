#!/usr/bin/env bash
set -euo pipefail

# Sign in to the Anthropic subscription through Claude Code.
#
# The staff seat in configs/staffing.toml runs on the claude harness under your
# existing subscription; there is no API key to configure. Sign-in is a browser
# flow that Claude Code drives itself, so all this script can do is install the
# CLI when asked and put you into that flow.

INSTALL=false
if [ "${1:-}" = "--install" ]; then INSTALL=true; fi

if ! command -v claude >/dev/null 2>&1; then
  if [ "$INSTALL" = true ] && command -v npm >/dev/null 2>&1; then
    echo "Installing Claude Code..."
    npm install --global @anthropic-ai/claude-code
  else
    echo "Claude Code missing. Install with: npm install --global @anthropic-ai/claude-code" >&2
    exit 1
  fi
fi

claude --version

if [ -t 0 ] && [ -t 1 ]; then
  echo
  echo "Opening the Claude Code sign-in. Complete it, then type /exit to continue the boot."
  claude /login || true
else
  echo "Not a terminal, so the interactive sign-in was not started."
  echo "Run \`claude /login\` yourself; 60-verify-boot will confirm the seat is signed in."
fi
