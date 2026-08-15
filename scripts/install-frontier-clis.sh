#!/usr/bin/env bash
set -euo pipefail

install=false
if [ "${1:-}" = "--install" ]; then install=true; fi

if [ "$install" = true ]; then
  command -v npm >/dev/null 2>&1 || {
    echo "npm/Node.js 18+ is required for the frontier CLI installers." >&2
    exit 1
  }
  npm install --global @openai/codex @anthropic-ai/claude-code
fi

if command -v codex >/dev/null 2>&1; then
  codex --version
  codex login status || echo "Codex is installed but not logged in. Run: codex login"
else
  echo "Codex CLI missing. Install with: npm install --global @openai/codex"
fi

if command -v claude >/dev/null 2>&1; then
  claude --version
  claude doctor || true
  echo "If Claude Code is not authenticated, run: claude"
else
  echo "Claude Code missing. Install with: npm install --global @anthropic-ai/claude-code"
fi

echo "Junior-only local tasks do not require these CLIs."
echo "Senior implementation and staff review do; they are never silently downgraded to junior."
