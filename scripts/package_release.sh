#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

uv run pytest
npm --prefix web run build

ZIP_PATH="../local_first_agent_os_release.zip"
rm -f "$ZIP_PATH"
zip -r "$ZIP_PATH" . \
  -x '.venv/*' \
  -x 'web/node_modules/*' \
  -x 'web/test-results/*' \
  -x 'web/playwright-report/*' \
  -x '.git/*' \
  -x '.local_agent/*' \
  -x '.pytest_cache/*' \
  -x '.ruff_cache/*' \
  -x '__pycache__/*' \
  -x '*/__pycache__/*' \
  -x '*.pyc'

echo "$ZIP_PATH"
