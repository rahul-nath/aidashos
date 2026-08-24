#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# The port comes from LOCAL_AGENT_LLAMA_BASE_URL when it is set, because that is
# the one the application connects to. Setting only the model's URL used to move
# the client while leaving the scripts starting and stopping 8080.
_llama_port_from_base_url() {
  local url="${LOCAL_AGENT_LLAMA_BASE_URL:-}"
  [ -n "$url" ] || return 1
  local tail="${url##*:}"
  local port="${tail%%/*}"
  [[ "$port" =~ ^[0-9]+$ ]] || return 1
  printf '%s' "$port"
}
LLAMA_PORT="${LOCAL_AGENT_LLAMA_PORT:-$(_llama_port_from_base_url || echo 8080)}"
WHISPER_PORT="${LOCAL_AGENT_WHISPER_PORT:-8090}"
API_PORT="${LOCAL_AGENT_API_PORT:-8000}"
WEB_PORT="${LOCAL_AGENT_WEB_PORT:-5173}"
SESSION_DAEMON_PORT="${LOCAL_AGENT_SESSION_DAEMON_PORT:-8765}"
PI_DAEMON_PORT="${LOCAL_AGENT_PI_DAEMON_PORT:-8766}"
DAEMON_DIR="${LOCAL_AGENT_DAEMON_DIR:-$HOME/.local-agent/daemon}"
RUN_DIR="$ROOT/.local_agent/run"

# `--force` skips the ledger question below. An unrecognised argument is an
# error rather than something to ignore, because a typo that quietly became an
# ordinary stop would be the exact thing this script now asks before doing.
FORCE=false
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=true ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Usage: stop-agent-runtime.sh [--force]" >&2
      exit 2
      ;;
  esac
done

LAUNCH_LABELS=(
  com.rahul.local-first-agent.session-daemon
  com.rahul.local-first-agent.lifecycle-maintenance
  # Installed by scripts/launchd/install.sh and never bootstrapped by the start
  # script, which brings Postgres up through Docker Compose instead. It is on
  # this list anyway: a stop that leaves a loaded agent behind is not a stop.
  com.rahul.local-first-agent.postgres
  com.rahul.local-first-agent.pi-daemon
  com.rahul.local-first-agent.whisper
  com.rahul.local-first-agent.llama
  # The two resident loops, which must be booted out rather than only killed.
  # Their plists set KeepAlive, so signalling the pid of a supervised loop asks
  # launchd to start it again a throttle interval later, and a stop that the
  # machine undoes by itself is not a stop.
  com.rahul.local-first-agent.enqueue-drainer
  com.rahul.local-first-agent.ledger-dispatcher
)
LAUNCH_DOMAIN="gui/$(id -u)"

COMPOSE_SERVICES=(
  postgres
  alloy
  tempo
  pyroscope
  prometheus
  loki
  minio-init
  minio
  grafana
)

pid_alive() {
  local pid="${1:-}"
  [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1
}

child_pids() {
  local pid="$1"
  pgrep -P "$pid" 2>/dev/null || true
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

# Eject launchd-managed daemons from the user domain so they don't respawn
# under us when we kill their PIDs below. This is durable rather than
# per-session: the plists live outside ~/Library/LaunchAgents, so nothing
# re-bootstraps them at the next login and only start-agent-runtime.sh brings
# them back.
bootout_launch_agents() {
  local label
  for label in "${LAUNCH_LABELS[@]}"; do
    if launchctl print "$LAUNCH_DOMAIN/$label" >/dev/null 2>&1; then
      echo "Booting out launchd agent: $label"
      launchctl bootout "$LAUNCH_DOMAIN/$label" 2>/dev/null || true
    fi
  done
}

stop_pid_tree() {
  local pid="${1:-}"
  local label="$2"
  if ! pid_alive "$pid"; then
    return
  fi

  local child
  for child in $(child_pids "$pid"); do
    stop_pid_tree "$child" "$label child"
  done

  echo "Stopping $label (pid $pid)"
  kill "$pid" >/dev/null 2>&1 || true
  if ! wait_for_exit "$pid"; then
    echo "Force stopping $label (pid $pid)"
    kill -9 "$pid" >/dev/null 2>&1 || true
  fi
}

stop_pid_file() {
  local pid_file="$1"
  local label="$2"
  if [ ! -f "$pid_file" ]; then
    return
  fi
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  stop_pid_tree "$pid" "$label"
  rm -f "$pid_file"
}

stop_port_if_matches() {
  local port="$1"
  local label="$2"
  local pattern="$3"
  local pids
  pids="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true)"
  if [ -z "$pids" ]; then
    return
  fi

  local pid command
  for pid in $pids; do
    command="$(ps -o command= -p "$pid" 2>/dev/null || true)"
    if [[ "$command" =~ $pattern ]]; then
      stop_pid_tree "$pid" "$label on port $port"
    else
      echo "Leaving port $port alone; pid $pid is not a known $label process: $command" >&2
    fi
  done
}

stop_repo_llama_processes() {
  local preset="$RUN_DIR/llama_presets.ini"
  local pids
  pids="$(pgrep -f "llama-server .*${preset}" 2>/dev/null || true)"
  local pid
  for pid in $pids; do
    stop_pid_tree "$pid" "repo llama-server"
  done
}

# Whether the runtime is doing work, asked of the coordination ledger. The
# answer is the first line of the output; every line after it names the facts
# that produced it. A check that could not run at all is folded into `unknown`
# here, so the caller below branches on one vocabulary rather than on a word and
# an exit status separately.
runtime_activity() {
  local report errors
  errors="$(mktemp)"
  if report="$(uv run local-agent runtime-activity 2>"$errors")"; then
    printf '%s\n' "$report"
  else
    printf 'unknown\nthe runtime activity check could not run:\n%s\n' "$(cat "$errors")"
  fi
  rm -f "$errors"
}

# Everything below this point is destructive: it boots out both resident loops,
# kills the model servers and the daemons, and stops Postgres. A milestone
# executing right now coordinates through the ledger that goes down with them,
# and its frontier process has already spent quota, so a stop taken at the wrong
# moment destroys that work rather than deferring it.
#
# The terminal hook has asked this question since the runtime lifetime work, but
# it asked on the path where a closing terminal fired the stop by accident. That
# path is gone: nothing starts or stops this runtime by itself any more, so this
# script is the only way to stop it and therefore the only place left to ask.
#
# `unknown` refuses alongside `busy`, which is the whole point rather than
# caution. An unreadable ledger is not evidence of idleness, and only evidence
# of idleness may authorise a destructive stop: refusing costs an idle runtime
# nobody needed, and proceeding costs the work.
refuse_unless_idle() {
  local report answer
  report="$(runtime_activity)"
  answer="$(printf '%s\n' "$report" | head -n 1)"

  if [ "$answer" = "idle" ]; then
    return 0
  fi

  local reason
  case "$answer" in
    busy) reason="work is in flight" ;;
    *) reason="whether work is in flight could not be determined" ;;
  esac

  {
    printf 'Refusing to stop (%s): %s.\n' "$answer" "$reason"
    printf '\n'
    printf '%s\n' "$report" | tail -n +2
    printf '\n'
    printf 'Ask again:   (cd %s && uv run local-agent runtime-activity)\n' "$ROOT"
    printf 'Stop anyway: %s/scripts/stop-agent-runtime.sh --force\n' "$ROOT"
  } >&2
  exit 1
}

flush_session_memory() {
  if curl --connect-timeout 2 --max-time 5 -fsS "http://127.0.0.1:${SESSION_DAEMON_PORT}/health" >/dev/null 2>&1; then
    echo "Flushing session memory"
    if ! uv run local-agent session-flush >/tmp/local-agent-session-flush.log 2>&1; then
      cat /tmp/local-agent-session-flush.log >&2
      echo "Session memory flush failed; refusing to stop the runtime." >&2
      return 1
    fi
  fi
}

stop_compose_services() {
  if ! command -v docker >/dev/null 2>&1 || [ ! -f "$ROOT/docker-compose.yml" ]; then
    return
  fi
  echo "Stopping local Docker Compose infrastructure"
  docker compose stop "${COMPOSE_SERVICES[@]}" >/dev/null 2>&1 || true
}

# Before the flush, not after it: the flush writes, and a stop this script is
# about to refuse should not have moved anything first.
if [ "$FORCE" = "true" ]; then
  echo "Stopping without asking the ledger (--force)."
else
  refuse_unless_idle
fi

flush_session_memory

bootout_launch_agents

# Do not send `/stop` through Pi during shutdown. A foreground operation such
# as ASR can hold the daemon's request lock indefinitely; this script owns the
# process lifecycle and terminates the model services directly below.

# The two resident loops first: both hold database connections and the
# dispatcher can have an agent subprocess under it, which stop_pid_tree reaches.
#
# The pid files only describe loops this checkout started. Each loop is a
# singleton over the coordination database, so the one that is actually running
# may belong to another git worktree, and an operator who runs this script
# expects the runtime to be down afterwards regardless of which directory
# started it. Ask the database who owns each loop and stop that process too.
. "$ROOT/scripts/resident-loop-owners.sh"
read_resident_loop_owners

stop_resident_loop() {
  local name="$1"
  local label="$2"
  stop_pid_file "$RUN_DIR/$name.pid" "$label"

  # Ask before killing by pid file, not after: stopping the tracked process is
  # what makes the lock's owner change, and the answer read here is from before
  # that happened, which is exactly the process still to be reached.
  local pid
  pid="$(resident_loop_owner_pid "$name")"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    stop_pid_tree "$pid" "$label owned by another checkout"
  fi
}

stop_resident_loop work-unit-enqueue-drainer "work unit enqueue drainer"
stop_resident_loop ledger-dispatcher "ledger dispatcher"

stop_pid_file "$DAEMON_DIR/pi-daemon.pid" "pi daemon"
stop_pid_file "$RUN_DIR/pi-daemon.pid" "tracked pi daemon"
stop_port_if_matches "$PI_DAEMON_PORT" "pi daemon" "(pi-daemon|run_pi_daemon|local-agent|python)"

stop_pid_file "$DAEMON_DIR/session-daemon.pid" "session daemon"
stop_port_if_matches "$SESSION_DAEMON_PORT" "session daemon" "(session-daemon|run_session_daemon|local-agent)"

stop_pid_file "$RUN_DIR/llama-router.pid" "tracked llama router"
stop_repo_llama_processes
stop_port_if_matches "$LLAMA_PORT" "llama-server" "llama-server"

stop_pid_file "$RUN_DIR/whisper-server.pid" "tracked whisper-server"
stop_port_if_matches "$WHISPER_PORT" "whisper-server" "whisper-server"

stop_port_if_matches "$API_PORT" "local-agent API" "(uvicorn|local-agent serve|fastapi|python)"
stop_port_if_matches "$WEB_PORT" "web dev server" "(vite|npm|node)"

rm -f "$DAEMON_DIR/sessions" "$DAEMON_DIR/lock"
rm -f "$RUN_DIR/llama-router.pid"
rm -f "$RUN_DIR/whisper-server.pid"
rm -f "$RUN_DIR/pi-daemon.pid"
rm -f "$RUN_DIR/work-unit-enqueue-drainer.pid"
rm -f "$RUN_DIR/ledger-dispatcher.pid"

stop_compose_services

echo "Local agent runtime stopped."
