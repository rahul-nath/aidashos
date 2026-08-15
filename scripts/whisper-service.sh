#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR="$ROOT/.local_agent/run"
LOG_DIR="$ROOT/.local_agent/logs"
PID_FILE="$RUN_DIR/whisper-server.pid"
LABEL="com.rahul.local-first-agent.whisper"
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

WHISPER_HOST="${LOCAL_AGENT_WHISPER_HOST:-127.0.0.1}"
WHISPER_PORT="${LOCAL_AGENT_WHISPER_PORT:-8090}"
WHISPER_URL="${LOCAL_AGENT_WHISPER_BASE_URL:-http://${WHISPER_HOST}:${WHISPER_PORT}}"

mkdir -p "$RUN_DIR" "$LOG_DIR"

whisper_up() {
  curl --connect-timeout 2 --max-time 5 -fsS "$WHISPER_URL/health" >/dev/null 2>&1 ||
    curl --connect-timeout 2 --max-time 5 -fsS "$WHISPER_URL/" >/dev/null 2>&1
}

wait_for_up() {
  local _i
  for _i in {1..120}; do
    if whisper_up; then
      return 0
    fi
    sleep 0.5
  done
  return 1
}

pid_alive() {
  local pid="${1:-}"
  [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1
}

wait_for_exit() {
  local pid="$1"
  local _i
  for _i in {1..20}; do
    if ! pid_alive "$pid"; then
      return 0
    fi
    sleep 0.2
  done
  return 1
}

stop_pid() {
  local pid="${1:-}"
  if ! pid_alive "$pid"; then
    return
  fi
  kill "$pid" >/dev/null 2>&1 || true
  if ! wait_for_exit "$pid"; then
    kill -9 "$pid" >/dev/null 2>&1 || true
  fi
}

start_whisper() {
  if whisper_up; then
    echo "whisper-server already running at $WHISPER_URL"
    return
  fi

  if command -v launchctl >/dev/null 2>&1 && [ -f "$PLIST" ]; then
    if ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      launchctl bootstrap "$DOMAIN" "$PLIST"
    fi
    rm -f "$PID_FILE"
  else
    nohup "$ROOT/scripts/start-whisper.sh" >"$LOG_DIR/whisper-server.log" 2>&1 &
    echo "$!" >"$PID_FILE"
  fi

  if ! wait_for_up; then
    echo "whisper-server did not become ready at $WHISPER_URL" >&2
    return 1
  fi
  echo "whisper-server started at $WHISPER_URL"
}

stop_whisper() {
  if command -v launchctl >/dev/null 2>&1 &&
    launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$LABEL"
  fi

  if [ -f "$PID_FILE" ]; then
    stop_pid "$(cat "$PID_FILE" 2>/dev/null || true)"
    rm -f "$PID_FILE"
  fi

  local pids pid command
  pids="$(lsof -nP -iTCP:"$WHISPER_PORT" -sTCP:LISTEN -t 2>/dev/null || true)"
  for pid in $pids; do
    command="$(ps -o command= -p "$pid" 2>/dev/null || true)"
    if [[ "$command" == *whisper-server* ]]; then
      stop_pid "$pid"
    else
      echo "Refusing to stop non-whisper process on port $WHISPER_PORT: $command" >&2
      return 1
    fi
  done

  if whisper_up; then
    echo "whisper-server is still reachable at $WHISPER_URL" >&2
    return 1
  fi
  echo "whisper-server stopped"
}

case "${1:-}" in
  start)
    start_whisper
    ;;
  stop)
    stop_whisper
    ;;
  *)
    echo "Usage: $0 {start|stop}" >&2
    exit 2
    ;;
esac
