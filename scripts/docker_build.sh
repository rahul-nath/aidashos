#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

IMAGE_TAG="${IMAGE_TAG:-local-first-agent-os:latest}"
docker build -t "$IMAGE_TAG" .
echo "$IMAGE_TAG"
