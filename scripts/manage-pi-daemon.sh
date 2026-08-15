#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
action="${1:-status}"
UV_BIN="$(command -v uv || true)"
[ -n "$UV_BIN" ] || { echo "uv is required; run ./scripts/bootstrap.sh" >&2; exit 1; }
PID_FILE="${LOCAL_AGENT_DAEMON_DIR:-$HOME/.local-agent/daemon}/pi-daemon.pid"

stop_autostart_daemon() {
  [ -f "$PID_FILE" ] || return 0
  local pid command
  pid="$(tr -dc '0-9' < "$PID_FILE")"
  command="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  if [ -z "$pid" ] || [[ "$command" != *"local_first_agent_os.pi_daemon"* ]]; then
    rm -f "$PID_FILE"
    return 0
  fi
  kill -TERM "$pid" 2>/dev/null || true
  for _ in {1..20}; do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
  done
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
}

case "$(uname -s)" in
  Darwin)
    label="com.rahul.local-first-agent.pi-daemon"
    domain="gui/$(id -u)"
    service="$HOME/Library/LaunchAgents/$label.plist"
    render() { uv run python "$ROOT/scripts/render-pi-daemon-service.py" launchd "$ROOT" "$UV_BIN" "$service"; }
    case "$action" in
      install) render; stop_autostart_daemon; launchctl bootout "$domain/$label" 2>/dev/null || true; launchctl bootstrap "$domain" "$service" ;;
      restart) render; stop_autostart_daemon; launchctl bootout "$domain/$label" 2>/dev/null || true; launchctl bootstrap "$domain" "$service" ;;
      stop) launchctl bootout "$domain/$label" 2>/dev/null || true ;;
      status) launchctl print "$domain/$label" ;;
      uninstall) launchctl bootout "$domain/$label" 2>/dev/null || true; rm -f "$service" ;;
      *) echo "Usage: $0 [install|restart|stop|status|uninstall]" >&2; exit 2 ;;
    esac
    ;;
  Linux)
    unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    service="$unit_dir/local-first-agent-os-pi.service"
    render() { uv run python "$ROOT/scripts/render-pi-daemon-service.py" systemd "$ROOT" "$UV_BIN" "$service"; systemctl --user daemon-reload; }
    case "$action" in
      install) render; systemctl --user enable --now local-first-agent-os-pi.service ;;
      restart) render; systemctl --user restart local-first-agent-os-pi.service ;;
      stop) systemctl --user stop local-first-agent-os-pi.service ;;
      status) systemctl --user status local-first-agent-os-pi.service ;;
      uninstall) systemctl --user disable --now local-first-agent-os-pi.service 2>/dev/null || true; rm -f "$service"; systemctl --user daemon-reload ;;
      *) echo "Usage: $0 [install|restart|stop|status|uninstall]" >&2; exit 2 ;;
    esac
    ;;
  *) echo "Unsupported platform" >&2; exit 1 ;;
esac
