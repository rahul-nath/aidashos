#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_DIR="${LOCAL_AGENT_DAEMON_DIR:-$HOME/.local-agent/daemon}"
SESSIONS="$STATE_DIR/sessions"
LOCK="$STATE_DIR/lock"
SESSION_DAEMON_PID="$STATE_DIR/session-daemon.pid"
SESSION_DAEMON_LOG="$STATE_DIR/session-daemon.log"
# One place answers what happened to the runtime on the last leave: either the
# stop script's transcript, or the reason it was not run. It is a variable now
# because two paths write it and a test has to be able to read it somewhere
# other than the operator's own log.
STOP_LOG="${LOCAL_AGENT_STOP_RUNTIME_LOG:-/tmp/local-agent-stop-agent-runtime.log}"
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

# Whether the runtime is doing work, asked of the coordination ledger. The
# answer is the first line of the output; everything after it names the facts
# that produced it. A failure to run at all is folded into `unknown` here so the
# caller has one vocabulary to branch on rather than a word and an exit status.
runtime_activity() {
  local report errors
  # The answer is stdout. Diagnostics the pool writes on its way to reporting an
  # unreachable ledger are not the answer, and folding them in would push the
  # word this hook branches on off the first line.
  errors="$(mktemp)"
  if report="$(cd "$ROOT" && uv run local-agent runtime-activity 2>"$errors")"; then
    printf '%s\n' "$report"
  else
    printf 'unknown\nthe runtime activity check could not run:\n%s\n' "$(cat "$errors")"
  fi
  rm -f "$errors"
}

# Leave the runtime up, and say so where the operator looks for what happened to
# it. A runtime left running for a reason nobody recorded is indistinguishable
# from one that failed to stop.
refuse_stop() {
  local answer="$1"
  local reason="$2"
  local report="$3"
  {
    printf 'Runtime left running (%s): %s.\n' "$answer" "$reason"
    printf '\n'
    printf '%s\n' "$report" | tail -n +2
    printf '\n'
    printf 'The last terminal closed, and stop-agent-runtime.sh was not run.\n'
    printf 'Ask again:    (cd %s && uv run local-agent runtime-activity)\n' "$ROOT"
    printf 'Stop anyway:  %s/scripts/stop-agent-runtime.sh\n' "$ROOT"
    printf 'Why:          docs/runtime_lifetime_follows_work_gawd.md\n'
  } > "$STOP_LOG"
  printf 'local agent runtime left running (%s); see %s\n' "$answer" "$STOP_LOG" >&2
}

# The last terminal has left. Stopping the runtime here takes down both resident
# loops, the API, and Postgres, so it is authorised by the ledger rather than by
# the terminal count: only an idle answer stops it. Busy and unknown both leave
# it up, for different reasons, and both say which.
stop_runtime_when_no_work_is_live() {
  local report answer
  report="$(runtime_activity)"
  answer="$(printf '%s\n' "$report" | head -n 1)"
  case "$answer" in
    idle)
      "$ROOT/scripts/stop-agent-runtime.sh" > "$STOP_LOG" 2>&1 || true
      ;;
    busy)
      refuse_stop "$answer" "work is in flight" "$report"
      ;;
    *)
      # An unreadable ledger, or a word this hook does not know. Neither is
      # evidence of idleness, and only evidence of idleness may authorise the
      # stop.
      refuse_stop "unknown" "whether work is in flight could not be determined" "$report"
      ;;
  esac
}

leave_session() {
  flush_session_contexts --session-id "$session_id"
  prune_sessions
  tmp="$(mktemp)"
  grep -vxF "$pid" "$SESSIONS" > "$tmp" || true
  mv "$tmp" "$SESSIONS"
  remaining="$(wc -l < "$SESSIONS" | tr -d ' ')"
  if [ "$remaining" = "0" ]; then
    stop_runtime_when_no_work_is_live
  fi
}

case "$action" in
  enter) with_lock enter_session ;;
  leave) with_lock leave_session ;;
  *) echo "unknown session action: $action" >&2; exit 2 ;;
esac
