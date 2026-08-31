#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CURL_HEALTH=(curl --connect-timeout 2 --max-time 5 -fsS)

load_dotenv_file() {
  local env_file="$1"
  local python_bin="${2:-$ROOT/.venv/bin/python}"
  local exports

  [ -f "$env_file" ] || return 0
  if [ ! -x "$python_bin" ]; then
    echo "Python environment is not ready for dotenv parsing: $python_bin" >&2
    return 1
  fi

  # A .env file is not a shell script. In particular, sourcing an unquoted JSON
  # array such as ["--headless"] removes its inner quotes and corrupts values
  # consumed by pydantic-settings. Parse with python-dotenv, validate names, and
  # emit shell-escaped assignments without ever placing values in argv.
  exports="$("$python_bin" - "$env_file" <<'PY'
import re
import shlex
import sys

from dotenv import dotenv_values

env_file = sys.argv[1]
for key, value in dotenv_values(env_file).items():
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        raise SystemExit(f"invalid environment variable name in {env_file}: {key!r}")
    if value is not None:
        print(f"export {key}={shlex.quote(value)}")
PY
)"
  eval "$exports"
}

# ASR is opt-in. whisper.cpp holds a multi-gigabyte model resident for the whole
# session, and the launchd job loads ggml-large-v3-turbo plus a Core ML encoder,
# so a runtime that starts it by default charges every session for a capability
# most sessions never use. Ask for it when you want it.
parse_runtime_args() {
  START_ASR=false
  # A quota probe is a real provider request carrying the CLI scaffold, so a
  # routine restart must not spend one per staffed tier. Use --frontier-probe
  # when deliberately validating newly edited model ids; live scheduling treats
  # the useful dispatch itself as the availability check.
  PROBE_FRONTIER=false
  # The environment variable exists so a launchd plist or a scripted caller can
  # ask without an argv change. An explicit flag wins because it is the more
  # specific request.
  if [ "${LOCAL_AGENT_START_ASR:-}" = "true" ]; then
    START_ASR=true
  fi
  local arg
  for arg in "$@"; do
    case "$arg" in
      --with-asr) START_ASR=true ;;
      --no-asr) START_ASR=false ;;
      --no-frontier-probe) PROBE_FRONTIER=false ;;
      --frontier-probe) PROBE_FRONTIER=true ;;
      *)
        echo "Unknown argument: $arg" >&2
        echo "Usage: start-agent-runtime.sh [--with-asr | --no-asr]" \
          "[--frontier-probe | --no-frontier-probe]" >&2
        return 2
        ;;
    esac
  done
}

# Sourcing this file is supported only for focused tests of the dotenv loader
# and the argument parser; runtime startup remains an executable-script
# operation.
if [[ "${BASH_SOURCE[0]}" != "$0" ]]; then
  return 0
fi

parse_runtime_args "$@"

cd "$ROOT"

# Frontier CLIs installed by their native user installers live here on macOS.
# Codex-launched shells do not necessarily include it, and the Pi daemon must
# inherit a stable PATH before it starts detached dispatch work.
if [ -d "$HOME/.local/bin" ]; then
  export PATH="$HOME/.local/bin:$PATH"
fi

# Install the declared environment before parsing .env so the same dotenv
# implementation used by pydantic-settings defines startup semantics.
uv sync
load_dotenv_file "$ROOT/.env"

export LOCAL_AGENT_DATABASE_URL="${LOCAL_AGENT_DATABASE_URL:-postgresql+psycopg://postgres:postgres@127.0.0.1:5432/local_agent}"
export LOCAL_AGENT_COORDINATION_BACKEND="${LOCAL_AGENT_COORDINATION_BACKEND:-postgres}"
export LOCAL_AGENT_COORDINATION_DATABASE_URL="${LOCAL_AGENT_COORDINATION_DATABASE_URL:-$LOCAL_AGENT_DATABASE_URL}"
export LOCAL_AGENT_DBOS_SYSTEM_DATABASE_URL="${LOCAL_AGENT_DBOS_SYSTEM_DATABASE_URL:-postgresql+psycopg://postgres:postgres@127.0.0.1:5432/local_agent_dbos}"
export DBOS_SYSTEM_DATABASE_URL="${DBOS_SYSTEM_DATABASE_URL:-$LOCAL_AGENT_DBOS_SYSTEM_DATABASE_URL}"
export LOCAL_AGENT_USE_DBOS="${LOCAL_AGENT_USE_DBOS:-true}"
export LOCAL_AGENT_MOCK_MODELS="${LOCAL_AGENT_MOCK_MODELS:-false}"
export LOCAL_AGENT_PROJECTS_ROOT="${LOCAL_AGENT_PROJECTS_ROOT:-$HOME/ai_projects}"
export LOCAL_AGENT_LLAMA_MODELS_DIR="${LOCAL_AGENT_LLAMA_MODELS_DIR:-$HOME/models}"
export LOCAL_AGENT_LLAMA_PARALLEL="${LOCAL_AGENT_LLAMA_PARALLEL:-4}"
export LOCAL_AGENT_PI_DAEMON_HOST="${LOCAL_AGENT_PI_DAEMON_HOST:-127.0.0.1}"
export LOCAL_AGENT_PI_DAEMON_PORT="${LOCAL_AGENT_PI_DAEMON_PORT:-8766}"
export LOCAL_AGENT_RUNTIME_REVISION="$(git rev-parse HEAD)"

# Whisper.cpp (ASR backend). The active ASR model comes from the registry.
export LOCAL_AGENT_WHISPER_HOST="${LOCAL_AGENT_WHISPER_HOST:-127.0.0.1}"
export LOCAL_AGENT_WHISPER_PORT="${LOCAL_AGENT_WHISPER_PORT:-8090}"
export LOCAL_AGENT_WHISPER_CPP_DIR="${LOCAL_AGENT_WHISPER_CPP_DIR:-$LOCAL_AGENT_PROJECTS_ROOT/whisper.cpp}"
export LOCAL_AGENT_WHISPER_BIN_PATH="${LOCAL_AGENT_WHISPER_BIN_PATH:-$LOCAL_AGENT_WHISPER_CPP_DIR/build/bin/whisper-server}"
export LOCAL_AGENT_WHISPER_MODELS_DIR="${LOCAL_AGENT_WHISPER_MODELS_DIR:-$LOCAL_AGENT_WHISPER_CPP_DIR/models}"
export LOCAL_AGENT_WHISPER_IDLE_MODEL="${LOCAL_AGENT_WHISPER_IDLE_MODEL:-ggml-base.en.bin}"
export LOCAL_AGENT_WHISPER_THREADS="${LOCAL_AGENT_WHISPER_THREADS:-8}"

mkdir -p .local_agent/logs .local_agent/run

# The Docker infrastructure helper owns Postgres startup and DBOS database
# creation. Observability remains opt-in through `pi /start /logging`.
"$ROOT/scripts/start-docker-compose-infra.sh" postgres

uv run local-agent init-db

# Symmetric undo of stop-agent-runtime.sh's bootout, and now the only thing that
# starts these agents at all: their plists live outside ~/Library/LaunchAgents,
# so launchd does not load them at login and this loop is where they enter the
# domain. The whisper/llama bring-up below short-circuits if these already
# brought the ports up.
LAUNCH_LABELS=(
  com.rahul.local-first-agent.postgres-backup
  com.rahul.local-first-agent.lifecycle-maintenance
  com.rahul.local-first-agent.session-daemon
  com.rahul.local-first-agent.pi-daemon
  com.rahul.local-first-agent.llama
  com.rahul.local-first-agent.enqueue-drainer
  com.rahul.local-first-agent.ledger-dispatcher
  com.rahul.local-first-agent.work-unit-crash-reconciler
  com.rahul.local-first-agent.refinery-fleet
)
# Bootstrapping the whisper agent is a launch, so it belongs behind the same
# gate as the manual bring-up below rather than happening unconditionally here.
if [ "$START_ASR" = "true" ]; then
  LAUNCH_LABELS+=(com.rahul.local-first-agent.whisper)
fi
LAUNCH_DOMAIN="gui/$(id -u)"
. "$ROOT/scripts/launchd-agent-dir.sh"
LAUNCH_PLIST_DIR="$LOCAL_AGENT_LAUNCHD_DIR"
# A failed bootstrap used to be discarded (`2>/dev/null || true`) and the script
# went on to exit 0, so a runtime that never came up reported success. That is
# how five plists pointing at a `uv` version that no longer existed stayed
# invisible: launchd could not exec, exited before writing a log line, and the
# only surviving evidence was a log file frozen at the last working run.
#
# The error is now printed and the script exits non-zero, because a bring-up
# script that cannot bring a service up has failed at the one thing it does.
# `already bootstrapped` (EALREADY, 37) stays benign: the guard above races with
# a concurrent start, and losing that race is not an error.
bootstrap_failures=()
for label in "${LAUNCH_LABELS[@]}"; do
  plist="$LAUNCH_PLIST_DIR/$label.plist"
  if [ -f "$plist" ] && ! launchctl print "$LAUNCH_DOMAIN/$label" >/dev/null 2>&1; then
    echo "Bootstrapping launchd agent: $label"
    if ! bootstrap_error="$(launchctl bootstrap "$LAUNCH_DOMAIN" "$plist" 2>&1)"; then
      if printf '%s' "$bootstrap_error" | grep -qi 'already bootstrapped'; then
        echo "  already bootstrapped by another process; continuing."
      else
        echo "  bootstrap failed: ${bootstrap_error:-(no output)}" >&2
        bootstrap_failures+=("$label")
      fi
    fi
  fi
done
if [ "${#bootstrap_failures[@]}" -gt 0 ]; then
  echo >&2
  echo "Failed to bootstrap: ${bootstrap_failures[*]}" >&2
  echo "The runtime is not up. Inspect a plist with:" >&2
  echo "  plutil -p $LAUNCH_PLIST_DIR/${bootstrap_failures[0]}.plist" >&2
  echo "A plist naming a binary that no longer exists is the usual cause." >&2
  exit 1
fi

LLAMA_URL="${LOCAL_AGENT_LLAMA_BASE_URL:-http://127.0.0.1:8080}"
LLAMA_LABEL="com.rahul.local-first-agent.llama"
llama_launchd=false
# Whichever branch below starts llama-server also decides where its output lands,
# so the failure message downstream can name the log the operator should open.
LLAMA_LOG=".local_agent/logs/llama-router.log"
if launchctl print "$LAUNCH_DOMAIN/$LLAMA_LABEL" >/dev/null 2>&1; then
  llama_launchd=true
  LLAMA_LOG="$(/usr/libexec/PlistBuddy -c 'Print :StandardErrorPath' \
    "$LAUNCH_PLIST_DIR/$LLAMA_LABEL.plist" 2>/dev/null || echo "$LLAMA_LOG")"
  rm -f .local_agent/run/llama-router.pid
elif ! "${CURL_HEALTH[@]}" "$LLAMA_URL/models" >/dev/null 2>&1; then
  nohup ./scripts/start-llama.sh >.local_agent/logs/llama-router.log 2>&1 &
  echo "$!" > .local_agent/run/llama-router.pid
fi

llama_ready=false
for _ in {1..80}; do
  if "${CURL_HEALTH[@]}" "$LLAMA_URL/models" >/dev/null 2>&1; then
    llama_ready=true
    break
  fi
  sleep 0.5
done
if [ "$llama_ready" = "false" ]; then
  if [ "$llama_launchd" = "true" ]; then
    echo "llama-server (launchd) not ready after 40s; kickstarting..." >&2
    launchctl kickstart -k "$LAUNCH_DOMAIN/$LLAMA_LABEL" 2>/dev/null || true
    for _ in {1..80}; do
      if "${CURL_HEALTH[@]}" "$LLAMA_URL/models" >/dev/null 2>&1; then
        llama_ready=true
        break
      fi
      sleep 0.5
    done
  fi
fi
if [ "$llama_ready" = "false" ]; then
  echo "llama-server did not become ready. Check $LLAMA_LOG" >&2
  exit 1
fi

# ----------------------------------------------------------------------------
# whisper.cpp server (ASR backend) - only when this run asked for it
# ----------------------------------------------------------------------------
WHISPER_URL="${LOCAL_AGENT_WHISPER_BASE_URL:-http://${LOCAL_AGENT_WHISPER_HOST}:${LOCAL_AGENT_WHISPER_PORT}}"

# If launchd owns whisper, let it (and only it) bind the port. The supervised
# launchd job loads ggml-large-v3-turbo + the Core ML encoder before it listens,
# so the health check below would fail during that window and we'd spawn a second
# whisper-server — both fighting over port 8090 and making KeepAlive hot-loop.
# Defer to launchd when its job is loaded; the readiness wait that follows covers
# the model-load delay. Only fall back to a manual launch when launchd isn't.
WHISPER_LABEL="com.rahul.local-first-agent.whisper"
whisper_up() {
  "${CURL_HEALTH[@]}" "$WHISPER_URL/health" >/dev/null 2>&1 || "${CURL_HEALTH[@]}" "$WHISPER_URL/" >/dev/null 2>&1
}
wait_whisper() {
  for _ in {1..120}; do
    if whisper_up; then return 0; fi
    sleep 0.5
  done
  return 1
}

if [ "$START_ASR" = "false" ]; then
  # Not started, and equally not stopped: a whisper someone else brought up is
  # theirs, and killing it here would make this script's job ambiguous.
  if whisper_up; then
    echo "ASR not requested; leaving the whisper-server already listening on $WHISPER_URL alone."
  else
    echo "ASR not requested; whisper-server not started (rerun with --with-asr for transcription)."
  fi
else
  whisper_launchd=false
  if launchctl print "$LAUNCH_DOMAIN/$WHISPER_LABEL" >/dev/null 2>&1; then
    whisper_launchd=true
    rm -f .local_agent/run/whisper-server.pid
  elif ! whisper_up; then
    nohup ./scripts/start-whisper.sh >.local_agent/logs/whisper-server.log 2>&1 &
    echo "$!" > .local_agent/run/whisper-server.pid
  fi

  if ! wait_whisper; then
    # A loaded launchd job can be wedged (crash-looping) rather than just slow to
    # load, in which case the wait above never succeeds. Force one restart and
    # give it another window before giving up.
    if [ "$whisper_launchd" = "true" ]; then
      echo "whisper-server (launchd) not ready after 60s; kickstarting..." >&2
      launchctl kickstart -k "$LAUNCH_DOMAIN/$WHISPER_LABEL" 2>/dev/null || true
      wait_whisper || true
    fi
  fi
  if ! whisper_up; then
    echo "whisper-server did not become ready. Check .local_agent/logs/whisper-server.log and launchctl print $LAUNCH_DOMAIN/$WHISPER_LABEL" >&2
    exit 1
  fi
fi

# Service reconciliation is a typed application concern.  The shell supplies
# its environment and paths, then gets out of the lifecycle decision tree.
uv run python -m local_first_agent_os.runtime_services pi-daemon \
  --repo-root "$ROOT" \
  --launch-domain "$LAUNCH_DOMAIN" \
  --launch-plist-dir "$LAUNCH_PLIST_DIR"

if [ "$START_ASR" = "true" ]; then
  echo "Postgres/DBOS schema/llama/whisper/pi-daemon are ready."
else
  echo "Postgres/DBOS schema/llama/pi-daemon are ready (ASR off)."
fi

# The junior tier (general -> gemma4) is the first dependency of every
# finalization pow-wow. Pre-load it so a fresh runtime can run intake without a
# separate manual step; every other role stays load-on-demand. `pi` can return
# zero for a completed-but-degraded workflow, so success is not inferred from
# its exit code: the second command proves the exact router model answers.
gemma_ready=false
for _ in {1..3}; do
  if uv run pi /start /gemma4 >/dev/null 2>&1 && \
    uv run python -m local_first_agent_os.model_probe \
      --base-url "$LLAMA_URL" --model gemma4 >/dev/null; then
    gemma_ready=true
    break
  fi
  sleep 2
done
if [ "$gemma_ready" = "true" ]; then
  echo "Junior tier pre-loaded and answered readiness proof (general -> gemma4)."
else
  echo "ERROR: gemma4 did not load and answer the readiness proof." >&2
  echo "       Check $LLAMA_LOG, then retry: pi /start /gemma4" >&2
  exit 1
fi

# Warn without blocking the runtime operators need to restaff unavailable tiers.
if [ "$PROBE_FRONTIER" = "true" ]; then
  uv run python -m local_first_agent_os.frontier_probe || true
fi

# ----------------------------------------------------------------------------
# The two resident loops that make queued work actually move.
#
# Everything above starts a service that answers when asked. These two are the
# ones that go looking: the drainer hands a QUEUED WorkUnit to DBOS, and the
# dispatcher claims the dispatch intents that WorkUnit's milestones submit.
# Without them a WorkUnit sits at QUEUED and a milestone sits at PENDING until a
# human types a command, which is what made the pair look like a manual step.
#
# They inherit the exported environment above, which is the point: the database
# URLs and LOCAL_AGENT_USE_DBOS are already correct here, so neither loop needs
# an operator to reconstruct them on a command line.
#
# Both are singletons over the coordination database, not over this directory.
# `$ROOT/.local_agent/run/<name>.pid` cannot express that: it is per-checkout, so
# running this script in a second git worktree started a second pair against the
# same Postgres, and which checkout's code ran a given WorkUnit became a race.
# Ownership is now an advisory lock the loop process holds for its lifetime, and
# the query below is how this script reports the existing owner instead of
# printing "Started" for a process that is about to exit.
# ----------------------------------------------------------------------------

. "$ROOT/scripts/resident-loop-owners.sh"
read_resident_loop_owners

# The launchd agent that supervises a loop, when one is installed for it.
#
# A supervised loop is not this script's to start. Both would race for the same
# advisory lock, the loser would exit immediately, and this script would have
# written a pid file for a process that is already gone - so the next run would
# read that file, find the pid dead, and try again, forever reporting a start
# that never held anything.
resident_loop_launch_label() {
  case "$1" in
    work-unit-enqueue-drainer) echo "com.rahul.local-first-agent.enqueue-drainer" ;;
    ledger-dispatcher) echo "com.rahul.local-first-agent.ledger-dispatcher" ;;
    *) echo "" ;;
  esac
}

start_resident_loop() {
  local name="$1"
  shift
  local pid_file=".local_agent/run/$name.pid"
  local log_file=".local_agent/logs/$name.log"
  local existing owner label

  label="$(resident_loop_launch_label "$name")"
  if [ -n "$label" ] && launchctl print "$LAUNCH_DOMAIN/$label" >/dev/null 2>&1; then
    echo "$name is supervised by launchd ($label); leaving it to launchd."
    return 0
  fi

  existing="$(cat "$pid_file" 2>/dev/null || true)"
  if [ -n "$existing" ] && kill -0 "$existing" 2>/dev/null; then
    echo "$name already running (pid $existing)"
    return 0
  fi

  # Empty means unowned, or means the query could not run. Either way this
  # proceeds and the lock decides; this check exists so the operator is told on
  # their terminal rather than in a log file they have no reason to open.
  owner="$(resident_loop_owner_description "$name")"
  if [ -n "$owner" ]; then
    echo "$name already owned by $owner; not starting a second one."
    echo "  Stop it from that checkout, or run ./scripts/stop-agent-runtime.sh here."
    return 0
  fi

  nohup "$@" >>"$log_file" 2>&1 &
  echo "$!" > "$pid_file"
  echo "Started $name (pid $!), logging to $log_file"
}

start_resident_loop work-unit-enqueue-drainer \
  uv run python agent_coordination_mcp.py --root "$ROOT" run_enqueue_drainer \
  --interval-seconds 5

start_resident_loop ledger-dispatcher \
  uv run python agent_coordination_mcp.py --root "$ROOT" run_ledger_dispatcher \
  --interval-seconds 2

uv run local-agent models-help
