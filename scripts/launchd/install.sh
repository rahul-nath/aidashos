#!/usr/bin/env bash
set -euo pipefail

# Installs (or removes) the launch agents: postgres bring-up, llama-server,
# whisper-server, pi-daemon, the session-daemon, and the two resident loops that
# move queued work. Run `install.sh` to install them and `install.sh uninstall`
# to remove them.
#
# These are not boot agents. The plists are rendered into LOCAL_AGENT_LAUNCHD_DIR
# rather than ~/Library/LaunchAgents, so login does not start them and a bootout
# survives a restart; see scripts/launchd-agent-dir.sh for why. What installs
# them is this script, what starts them afterwards is start-agent-runtime.sh, and
# what stops them is stop-agent-runtime.sh.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
UV_BIN="$(command -v uv || true)"
[ -n "$UV_BIN" ] || { echo "uv is required; run $ROOT/scripts/bootstrap.sh" >&2; exit 1; }
. "$ROOT/scripts/launchd-agent-dir.sh"
LAUNCH_AGENTS="$LOCAL_AGENT_LAUNCHD_DIR"
DOMAIN="gui/$(id -u)"
LAUNCHD_BOOTOUT_TIMEOUT_SECONDS="${LOCAL_AGENT_LAUNCHD_BOOTOUT_TIMEOUT_SECONDS:-15}"
LAUNCHD_BOOTOUT_POLL_SECONDS="${LOCAL_AGENT_LAUNCHD_BOOTOUT_POLL_SECONDS:-0.2}"

case "$LAUNCHD_BOOTOUT_TIMEOUT_SECONDS" in
  ''|*[!0-9]*)
    echo "LOCAL_AGENT_LAUNCHD_BOOTOUT_TIMEOUT_SECONDS must be a positive integer" >&2
    exit 2
    ;;
esac
[ "$LAUNCHD_BOOTOUT_TIMEOUT_SECONDS" -gt 0 ] || {
  echo "LOCAL_AGENT_LAUNCHD_BOOTOUT_TIMEOUT_SECONDS must be a positive integer" >&2
  exit 2
}
[[ "$LAUNCHD_BOOTOUT_POLL_SECONDS" =~ ^([0-9]+([.][0-9]*)?|[.][0-9]+)$ ]] || {
  echo "LOCAL_AGENT_LAUNCHD_BOOTOUT_POLL_SECONDS must be a positive number" >&2
  exit 2
}
[[ "$LAUNCHD_BOOTOUT_POLL_SECONDS" != 0 && "$LAUNCHD_BOOTOUT_POLL_SECONDS" != 0.0 ]] || {
  echo "LOCAL_AGENT_LAUNCHD_BOOTOUT_POLL_SECONDS must be a positive number" >&2
  exit 2
}

PLISTS=(
  com.rahul.local-first-agent.postgres
  com.rahul.local-first-agent.lifecycle-maintenance
  com.rahul.local-first-agent.llama
  com.rahul.local-first-agent.whisper
  com.rahul.local-first-agent.session-daemon
  com.rahul.local-first-agent.pi-daemon
  # Last, and after postgres for a reason worth stating: both loops need the
  # coordination database. Bootstrapping them behind it means the ordinary
  # install does not spend a throttle interval failing, though neither one
  # depends on the order - launchd offers no ordering guarantee, and a loop
  # that starts early simply retries.
  com.rahul.local-first-agent.enqueue-drainer
  com.rahul.local-first-agent.ledger-dispatcher
)

wait_for_bootout() {
  local label="$1"
  local deadline=$((SECONDS + LAUNCHD_BOOTOUT_TIMEOUT_SECONDS))

  while launchctl print "$DOMAIN/$label" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then
      echo "Timed out waiting for launchd to unload $label after ${LAUNCHD_BOOTOUT_TIMEOUT_SECONDS}s." >&2
      launchctl print "$DOMAIN/$label" >&2 || true
      return 1
    fi
    sleep "$LAUNCHD_BOOTOUT_POLL_SECONDS"
  done
}

bootout_and_wait() {
  local label="$1"

  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  wait_for_bootout "$label"
}

action="${1:-install}"

case "$action" in
  install)
    mkdir -p "$LAUNCH_AGENTS" "$HOME/.local-agent/logs"
    for label in "${PLISTS[@]}"; do
      dst="$LAUNCH_AGENTS/$label.plist"
      uv run python "$ROOT/scripts/render-launchd-template.py" \
        "$SCRIPT_DIR/$label.plist" "$dst" "$ROOT" "$UV_BIN"
      bootout_and_wait "$label"
      launchctl bootstrap "$DOMAIN" "$dst"
      echo "loaded $label"
    done
    ;;
  uninstall)
    for label in "${PLISTS[@]}"; do
      bootout_and_wait "$label"
      rm -f "$LAUNCH_AGENTS/$label.plist"
      echo "removed $label"
    done
    ;;
  *)
    echo "Usage: install.sh [install|uninstall]" >&2
    exit 2
    ;;
esac
