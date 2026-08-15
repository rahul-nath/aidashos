#!/usr/bin/env bash
set -euo pipefail

# Sign in to the ChatGPT subscription through the Codex CLI.
#
# The senior seat in configs/staffing.toml runs on the codex harness under your
# existing subscription. `codex login` drives a browser flow and returns when
# it finishes, so unlike the Claude sign-in this one can run unattended in the
# middle of the boot sequence.

INSTALL=false
if [ "${1:-}" = "--install" ]; then INSTALL=true; fi

if ! command -v codex >/dev/null 2>&1; then
  if [ "$INSTALL" = true ] && command -v npm >/dev/null 2>&1; then
    echo "Installing the Codex CLI..."
    npm install --global @openai/codex
  else
    echo "Codex CLI missing. Install with: npm install --global @openai/codex" >&2
    exit 1
  fi
fi

codex --version

if codex login status >/dev/null 2>&1; then
  echo "Codex is already signed in."
  exit 0
fi

if [ -t 0 ] && [ -t 1 ]; then
  echo "Opening the Codex sign-in (browser flow)..."
  codex login
else
  echo "Not a terminal, so the interactive sign-in was not started."
  echo "Run \`codex login\` yourself; 60-verify-boot will confirm the seat is signed in."
fi
