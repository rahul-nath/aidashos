#!/usr/bin/env bash
set -euo pipefail

# Complete local observability stack, split out of start-agent-runtime.sh so the core stack
# (postgres + llama + whisper) can come up without the heavier telemetry
# containers. Brought up on demand via `pi /start /logging` (and torn down
# with `pi /stop /logging`), or run directly:
#
#   ./scripts/start-local-observability.sh        # up   (default)
#   ./scripts/start-local-observability.sh down   # stop the containers
#   ./scripts/start-local-observability.sh up minimal
#   ./scripts/start-local-observability.sh down minimal
#
# Alloy ships logs to Loki and traces to Tempo. Tempo, Pyroscope, and Loki
# persist to MinIO; Prometheus and Grafana provide metrics collection and the
# local operator dashboard.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Compose cannot expand '~' in a bind mount. Export the resolved daemon log
# directory so Alloy can tail both repo-managed logs and autostart/launchd logs.
export LOCAL_AGENT_HOST_DAEMON_LOG_DIR="${LOCAL_AGENT_DAEMON_DIR:-$HOME/.local-agent/daemon}"
export LOCAL_AGENT_HOST_LAUNCHD_LOG_DIR="${LOCAL_AGENT_LAUNCHD_LOG_DIR:-$HOME/.local-agent/logs}"
mkdir -p \
  "$ROOT/.local_agent/logs" \
  "$LOCAL_AGENT_HOST_DAEMON_LOG_DIR" \
  "$LOCAL_AGENT_HOST_LAUNCHD_LOG_DIR"

ACTION="${1:-up}"
MODE="${2:-full}"

# minio first so its buckets exist before the consumers start; minio-init is a
# one-shot that exits once buckets are created.
OBSERVABILITY=(minio minio-init loki tempo pyroscope prometheus alloy grafana)
MINIMAL_OBSERVABILITY=(prometheus)

wait_until_ready() {
  local mode="$1"
  local deadline=$((SECONDS + 120))
  local pending name url
  local -a names urls
  if [ "$mode" = "minimal" ]; then
    names=(prometheus)
    urls=(http://127.0.0.1:9090/-/ready)
  else
    names=(minio loki tempo pyroscope prometheus alloy grafana)
    urls=(
      http://127.0.0.1:9000/minio/health/live
      http://127.0.0.1:3100/ready
      http://127.0.0.1:3200/ready
      http://127.0.0.1:4040/ready
      http://127.0.0.1:9090/-/ready
      http://127.0.0.1:12345/-/ready
      http://127.0.0.1:3000/api/health
    )
  fi

  while (( SECONDS < deadline )); do
    pending=0
    for index in "${!names[@]}"; do
      name="${names[$index]}"
      url="${urls[$index]}"
      if ! curl --connect-timeout 2 --max-time 5 -fsS "$url" >/dev/null 2>&1; then
        pending=$((pending + 1))
        if [ "$(docker inspect -f '{{.State.Status}}' "local-agent-$name" 2>/dev/null || true)" = "exited" ]; then
          echo "observability service exited before readiness: $name" >&2
          docker compose logs --tail=100 "$name" >&2
          return 1
        fi
      fi
    done
    if (( pending == 0 )); then
      if [ "$mode" = "full" ] && [ "$(docker inspect -f '{{.State.ExitCode}}' local-agent-minio-init 2>/dev/null || true)" != "0" ]; then
        echo "MinIO bucket initialization failed." >&2
        docker compose logs --tail=100 minio-init >&2
        return 1
      fi
      return 0
    fi
    sleep 2
  done

  echo "observability stack did not become ready within 120 seconds" >&2
  docker compose ps -a >&2
  if [ "$mode" = "minimal" ]; then
    docker compose logs --tail=100 "${MINIMAL_OBSERVABILITY[@]}" >&2
  else
    docker compose logs --tail=100 "${OBSERVABILITY[@]}" >&2
  fi
  return 1
}

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running. Start Docker Desktop and retry." >&2
  exit 1
fi

if [[ "$MODE" != "minimal" && "$MODE" != "full" ]]; then
  echo "Usage: start-local-observability.sh [up|down] [minimal|full]" >&2
  exit 2
fi

case "$ACTION" in
  up)
    if [ "$MODE" = "minimal" ]; then
      "$ROOT/scripts/start-docker-compose-infra.sh" observability-minimal
      wait_until_ready minimal
      echo "minimal observability ready: ${MINIMAL_OBSERVABILITY[*]}"
    else
      "$ROOT/scripts/start-docker-compose-infra.sh" observability
      wait_until_ready full
      echo "full observability ready: ${OBSERVABILITY[*]}"
    fi
    ;;
  down)
    if [ "$MODE" = "minimal" ]; then
      docker compose stop "${MINIMAL_OBSERVABILITY[@]}"
      echo "minimal observability stopped: ${MINIMAL_OBSERVABILITY[*]}"
    else
      docker compose stop "${OBSERVABILITY[@]}"
      echo "full observability stopped: ${OBSERVABILITY[*]}"
    fi
    ;;
  *)
    echo "Usage: start-local-observability.sh [up|down] [minimal|full]" >&2
    exit 2
    ;;
esac
