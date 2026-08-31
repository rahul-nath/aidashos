#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=toolchain-pins.env
. "$ROOT/scripts/toolchain-pins.env"

install=false
if [ "${1:-}" = "--install" ]; then install=true; fi

if [ "$install" = true ]; then
  command -v npm >/dev/null 2>&1 || {
    echo "npm/Node.js 18+ is required for the frontier CLI installers." >&2
    exit 1
  }
  npm install --global \
    "@openai/codex@$CODEX_CLI_VERSION" \
    "@anthropic-ai/claude-code@$CLAUDE_CODE_VERSION"
fi

if command -v codex >/dev/null 2>&1; then
  codex_version="$(codex --version | awk '{print $2}')"
  [ "$codex_version" = "$CODEX_CLI_VERSION" ] || {
    echo "Codex CLI $codex_version is installed; expected $CODEX_CLI_VERSION." >&2
    exit 1
  }
  codex --version
  codex login status || echo "Codex is installed but not logged in. Run: codex login"
else
  echo "Codex CLI missing. Install with: npm install --global @openai/codex"
fi

if command -v claude >/dev/null 2>&1; then
  claude_version="$(claude --version | awk '{print $1}')"
  [ "$claude_version" = "$CLAUDE_CODE_VERSION" ] || {
    echo "Claude Code $claude_version is installed; expected $CLAUDE_CODE_VERSION." >&2
    exit 1
  }
  claude --version
  claude doctor || true
  echo "If Claude Code is not authenticated, run: claude"
else
  echo "Claude Code missing. Install with: npm install --global @anthropic-ai/claude-code"
fi

echo "Junior-only local tasks do not require these CLIs."
echo "Senior implementation and staff review do; they are never silently downgraded to junior."
