#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Watch the WorkUnit event log and say something when a human is needed.
#
# A run parks on `MILESTONE_WAITING_FOR_OPERATOR` and waits, and nothing pushes
# that anywhere: it sits in the ledger until somebody looks. An unattended lane
# that only reports into a page you have to be looking at is an unattended lane
# that stops for hours without telling you.
#
# Read-only by construction. It calls `list_work_units`, `get_work_unit`, and
# `list_work_unit_events` and nothing else. None of those reach `launch_dbos()`,
# which is what actually starts recovery, so leaving this running never claims
# work, never drains an outbox, and never becomes a second dispatcher.
#
# Usage:
#   scripts/notify.sh                       # every non-terminal WorkUnit
#   scripts/notify.sh <work_unit_id>        # just this one
#   scripts/notify.sh --interval 5          # poll faster (default 10s)
#   scripts/notify.sh --replay              # also announce events already in the log
#   scripts/notify.sh --all-events          # print progress events too, not just actionable ones

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WORK_UNIT_ID=""
INTERVAL=10
REPLAY=0
ALL_EVENTS=0

while [ $# -gt 0 ]; do
  case "$1" in
    --interval) INTERVAL="$2"; shift 2 ;;
    --replay) REPLAY=1; shift ;;
    --all-events) ALL_EVENTS=1; shift ;;
    -h|--help) sed -n '6,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) WORK_UNIT_ID="$1"; shift ;;
  esac
done

# Fail here rather than let the coordination store fall through its resolution
# chain. `LOCAL_AGENT_DATABASE_URL` is the third link in that chain, so a shell
# that has it exported and the coordination variables missing connects to a
# different database and reports on the wrong ledger without saying so.
if [ -z "${AGENT_COORDINATION_DATABASE_URL:-}" ] && [ -z "${LOCAL_AGENT_COORDINATION_DATABASE_URL:-}" ]; then
  cat >&2 <<'MISSING'
notify.sh needs the coordination ledger environment. Export it first:

  export LOCAL_AGENT_COORDINATION_BACKEND=postgres
  export AGENT_COORDINATION_BACKEND=postgres
  export LOCAL_AGENT_COORDINATION_DATABASE_URL="postgresql+psycopg://postgres:postgres@127.0.0.1:5432/local_agent"
  export AGENT_COORDINATION_DATABASE_URL="$LOCAL_AGENT_COORDINATION_DATABASE_URL"

Deliberately not defaulted. Guessing the ledger is how you end up watching one
database while your run writes to another.
MISSING
  exit 2
fi

export LOCAL_AGENT_NOTIFY_WORK_UNIT_ID="$WORK_UNIT_ID"
export LOCAL_AGENT_NOTIFY_INTERVAL="$INTERVAL"
export LOCAL_AGENT_NOTIFY_REPLAY="$REPLAY"
export LOCAL_AGENT_NOTIFY_ALL_EVENTS="$ALL_EVENTS"

exec uv run python - <<'PY'
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

from local_first_agent_os.work_units import service

# Events that mean a person has something to do, or that the run stopped. These
# get a desktop notification; everything else is at most a printed line, because
# a notifier that fires on every phase transition is one you turn off.
ACTIONABLE = {
    "MILESTONE_WAITING_FOR_OPERATOR": "needs your decision",
    "APPROVAL_REQUESTED": "needs your approval",
    "MILESTONE_FAILED": "milestone failed",
    "MILESTONE_BLOCKED": "milestone blocked",
    "WORK_UNIT_BLOCKED": "work unit blocked",
    "WORK_UNIT_FAILED": "work unit failed",
    "WORK_UNIT_SUCCEEDED": "work unit finished",
    "WORK_UNIT_CANCELLED": "work unit cancelled",
}

# Worth printing, never worth interrupting for.
PROGRESS = {
    "MILESTONE_SUCCEEDED",
    "MILESTONE_STARTED",
    "MILESTONE_READY",
    "PHASE_STARTED",
    "PHASE_COMPLETED",
    "PHASE_SKIPPED",
    "DISPATCH_INTENT_CREATED",
    "APPROVAL_RECEIVED",
}

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED", "SUPERSEDED", "COMPLETE"}

_HAS_OSASCRIPT = shutil.which("osascript") is not None


def notify(title: str, message: str) -> None:
    """Desktop notification, degrading to stdout where there isn't one.

    A failure to notify must never take the watcher down with it: the printed
    line is the fallback and the loop is the point.
    """

    if not _HAS_OSASCRIPT:
        return
    body = message.replace('"', "'")
    head = title.replace('"', "'")
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{body}" with title "{head}" sound name "Submarine"',
            ],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def watched_work_unit_ids(pinned: str) -> list[str]:
    if pinned:
        return [pinned]
    units = []
    for row in service.list_work_units():
        status = str(getattr(row, "status", "") or (row.get("status") if isinstance(row, dict) else ""))
        status = status.rsplit(".", 1)[-1].strip("'\"")
        wid = getattr(row, "work_unit_id", None) or (
            row.get("work_unit_id") if isinstance(row, dict) else None
        )
        if wid and status not in TERMINAL_STATUSES:
            units.append(wid)
    return units


def title_for(work_unit_id: str) -> str:
    try:
        return service.get_work_unit(work_unit_id).title or work_unit_id[:12]
    except Exception:
        return work_unit_id[:12]


def main() -> int:
    pinned = os.environ.get("LOCAL_AGENT_NOTIFY_WORK_UNIT_ID", "").strip()
    interval = max(1.0, float(os.environ.get("LOCAL_AGENT_NOTIFY_INTERVAL", "10")))
    replay = os.environ.get("LOCAL_AGENT_NOTIFY_REPLAY", "0") == "1"
    all_events = os.environ.get("LOCAL_AGENT_NOTIFY_ALL_EVENTS", "0") == "1"

    # Start at the tail unless asked otherwise. Restarting a watcher should not
    # replay an hour of history as fresh notifications, which is the behaviour
    # that gets a notifier muted and then forgotten.
    cursors: dict[str, int] = {}
    titles: dict[str, str] = {}

    targets = watched_work_unit_ids(pinned)
    if not targets:
        print("no non-terminal work units to watch", file=sys.stderr)
        return 1
    for wid in targets:
        titles[wid] = title_for(wid)
        cursors[wid] = 0 if replay else len(service.list_work_unit_events(wid, limit=1000))
        print(f"watching {titles[wid]} ({wid[:12]})", flush=True)

    while True:
        try:
            for wid in list(watched_work_unit_ids(pinned)):
                if wid not in cursors:
                    titles[wid] = title_for(wid)
                    cursors[wid] = 0
                    print(f"watching {titles[wid]} ({wid[:12]})", flush=True)
                events = service.list_work_unit_events(wid, limit=1000)
                fresh = events[cursors[wid] :]
                cursors[wid] = len(events)
                for event in fresh:
                    kind = str(event.get("event_type"))
                    when = str(event.get("occurred_at"))[11:19]
                    phase = event.get("phase") or ""
                    if kind in ACTIONABLE:
                        line = f"{when}  {kind}  {phase}  <- {ACTIONABLE[kind]}"
                        print(line, flush=True)
                        notify(titles[wid], f"{ACTIONABLE[kind]} ({phase or kind})")
                    elif all_events or kind in PROGRESS:
                        print(f"{when}  {kind}  {phase}", flush=True)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            # A ledger blip is not a reason to stop watching. Say so and keep
            # polling; a watcher that exits on the first transient error is
            # indistinguishable from one that is quietly not running.
            print(f"poll failed ({type(exc).__name__}: {exc}); retrying", file=sys.stderr, flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
PY
