#!/usr/bin/env bash
set -euo pipefail

# Tears down Docker Compose infrastructure. Mirrors
# start-docker-compose-infra.sh.
#   observability - stop + remove the telemetry containers
#   postgres      - stop + remove Postgres
#
# Named data volumes (Grafana dashboards, Prometheus/Loki/Tempo history) are
# kept; `docker compose rm` only removes the containers.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

STACK="${1:-observability}"

# The asymmetry this script exists for: stopping the telemetry must leave
# Postgres up, because the running agent is talking to the ledger inside it.
# That is why `core` is a profile in docker-compose.yml rather than an omission.
# A profile-less service is selected by every profile-scoped command, so a
# profile-less postgres would be torn down by the `observability` arm below.
case "$STACK" in
  postgres)
    profiles=(--profile core)
    ;;
  observability)
    profiles=(--profile observability)
    ;;
  *)
    echo "Usage: stop-docker-compose-infra.sh [observability|postgres]" >&2
    exit 2
    ;;
esac

docker compose "${profiles[@]}" rm --stop --force

echo "services down [$STACK]"
