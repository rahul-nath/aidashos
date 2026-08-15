#!/usr/bin/env bash
set -euo pipefail

# Brings up Docker Compose infrastructure. Three stacks:
#   postgres              - Postgres only (the agent runtime dependency)
#   observability-minimal - Postgres plus Prometheus metrics
#   observability         - Postgres plus the complete telemetry stack
#
# `pi /start /observability` shells out to this script with the observability
# stack; compose is additive, so it joins an already-running postgres.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

STACK="${1:-postgres}"

# Which services each stack is made of is stated once, in docker-compose.yml,
# next to the services themselves. This script only says which stacks it wants.
# The lists that used to live here were byte-identical copies of lists in
# stop-docker-compose-infra.sh, start-local-observability.sh, and
# stop-agent-runtime.sh, so adding a telemetry service meant editing all of them
# and only the compose file failed loudly when one was missed.
case "$STACK" in
  postgres)
    profiles=(--profile core)
    ;;
  observability-minimal)
    profiles=(--profile core --profile observability-minimal)
    ;;
  observability)
    profiles=(--profile core --profile observability)
    ;;
  *)
    echo "Usage: start-docker-compose-infra.sh [postgres|observability-minimal|observability]" >&2
    exit 2
    ;;
esac

if ! docker info >/dev/null 2>&1; then
  if [[ "${LOCAL_AGENT_QUIET_DOCKER_UNAVAILABLE:-false}" == "true" ]]; then
    exit 0
  fi
  echo "Docker is unavailable; start Docker before requesting local infrastructure." >&2
  exit 1
fi

docker compose "${profiles[@]}" up -d

# init-postgres.sql creates local_agent_dbos on first container creation; this
# covers the case of a pre-existing postgres volume from before that init ran.
docker exec local-agent-postgres createdb -U postgres local_agent_dbos >/dev/null 2>&1 || true

echo "services up [$STACK]"
