#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! docker info >/dev/null 2>&1; then
  echo "Docker is unavailable; start Docker before requesting verification infrastructure." >&2
  exit 1
fi

# The base install already acquired this image for the core database.
# Runtime relaunch may start the existing service, but never acquires an image.
exec docker compose up --pull never --no-build --wait --wait-timeout 120 -d postgres-test
