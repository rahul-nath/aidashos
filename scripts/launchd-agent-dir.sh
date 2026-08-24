#!/usr/bin/env bash
# Where the rendered launch agent plists live, for every script that installs,
# bootstraps, or boots one out.
#
# Deliberately not ~/Library/LaunchAgents. launchd reads that directory at every
# login and honours RunAtLoad, so a plist sitting there starts its service on
# login whether or not anyone asked for the runtime, and `launchctl bootout` is
# undone by the next login. That is why stop-agent-runtime.sh could only ever
# promise a stop until the machine restarted.
#
# Keeping the plists outside that directory makes launchd learn about these jobs
# only when start-agent-runtime.sh bootstraps them by path, and forget them when
# stop-agent-runtime.sh boots them out. RunAtLoad and KeepAlive keep their
# meaning inside that window: the services still come up on bootstrap and are
# still supervised while the runtime is up.
LOCAL_AGENT_LAUNCHD_DIR="${LOCAL_AGENT_LAUNCHD_DIR:-$HOME/.local-agent/launchd}"
