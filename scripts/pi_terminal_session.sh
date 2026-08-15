#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${LOCAL_AGENT_DAEMON_DIR:-$HOME/.local-agent/daemon}"
SESSIONS="$STATE_DIR/sessions"
LOCK="$STATE_DIR/lock"
SESSION_DAEMON_PID="$STATE_DIR/session-daemon.pid"
SESSION_DAEMON_LOG="$STATE_DIR/session-daemon.log"
mkdir -p "$STATE_DIR"

action="${1:?enter or leave}"
pid="${2:?shell pid}"
session_id="${3:-shell-$pid}"
session_daemon_port="${LOCAL_AGENT_SESSION_DAEMON_PORT:-8765}"

with_lock() {
  shlock -f "$LOCK" -p "$$" >/dev/null 2>&1 || true
  "$@"
  rm -f "$LOCK"
}

prune_sessions() {
  touch "$SESSIONS"
  tmp="$(mktemp)"
  while IFS= read -r existing; do
    [ -z "$existing" ] && continue
    if kill -0 "$existing" >/dev/null 2>&1; then
      echo "$existing" >> "$tmp"
    fi
  done < "$SESSIONS"
  sort -u "$tmp" > "$SESSIONS"
  rm -f "$tmp"
}

session_daemon_ready() {
  curl --connect-timeout 2 --max-time 5 -fsS "http://127.0.0.1:${session_daemon_port}/health" >/dev/null 2>&1
}

ensure_session_daemon() {
  if session_daemon_ready; then
    return
  fi
  if [ -f "$SESSION_DAEMON_PID" ] && ! kill -0 "$(cat "$SESSION_DAEMON_PID")" >/dev/null 2>&1; then
    rm -f "$SESSION_DAEMON_PID"
  fi
  (
    cd "$ROOT"
    nohup uv run local-agent session-daemon </dev/null >"$SESSION_DAEMON_LOG" 2>&1 &
    echo "$!" > "$SESSION_DAEMON_PID"
  )
  for _ in {1..40}; do
    if session_daemon_ready; then
      return
    fi
    sleep 0.25
  done
  echo "session daemon did not become ready; check $SESSION_DAEMON_LOG" >&2
}

flush_session_contexts() {
  (
    cd "$ROOT"
    uv run local-agent session-flush "$@" >/tmp/local-agent-session-flush.log 2>&1
  ) || true
}

stop_session_daemon() {
  if [ -f "$SESSION_DAEMON_PID" ]; then
    kill "$(cat "$SESSION_DAEMON_PID")" >/dev/null 2>&1 || true
    rm -f "$SESSION_DAEMON_PID"
  fi
}

enter_session() {
  prune_sessions
  before="$(wc -l < "$SESSIONS" | tr -d ' ')"
  printf '%s\n' "$pid" >> "$SESSIONS"
  sort -u "$SESSIONS" -o "$SESSIONS"
  if [ "$before" = "0" ]; then
    "$ROOT/scripts/start-agent-runtime.sh" >/tmp/local-agent-start-agent-runtime.log 2>&1 || cat /tmp/local-agent-start-agent-runtime.log >&2
  fi
  ensure_session_daemon
}

leave_session() {
  flush_session_contexts --session-id "$session_id"
  prune_sessions
  tmp="$(mktemp)"
  grep -vxF "$pid" "$SESSIONS" > "$tmp" || true
  mv "$tmp" "$SESSIONS"
  remaining="$(wc -l < "$SESSIONS" | tr -d ' ')"
  if [ "$remaining" = "0" ]; then
    "$ROOT/scripts/stop-agent-runtime.sh" >/tmp/local-agent-stop-agent-runtime.log 2>&1 || true
  fi
}

case "$action" in
  enter) with_lock enter_session ;;
  leave) with_lock leave_session ;;
  *) echo "unknown session action: $action" >&2; exit 2 ;;
esac
