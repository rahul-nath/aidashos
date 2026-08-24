hook_file="${${(%):-%x}:A}"
export LOCAL_AGENT_REPO="${LOCAL_AGENT_REPO:-${hook_file:h:h}}"
export LOCAL_AGENT_SHELL_SESSION_ID="${LOCAL_AGENT_SHELL_SESSION_ID:-shell-$$}"

# Opening a terminal is not a request to run the agent runtime.
#
# This block used to be unconditional, so every interactive shell ran
# `pi_terminal_session.sh enter`: the first shell started the whole runtime and
# every shell after it called ensure_session_daemon. That guard is a health
# check against the session daemon's port, and when the daemon cannot bind it -
# a Postgres outage is enough, because the daemon waits for the database before
# it listens - the check fails for every new shell and every new shell starts
# another copy. The resident session daemon count then grows with the number of
# terminals opened, which is how six of them ended up holding ~86 MB each.
#
# Starting and stopping the runtime is now something an operator asks for by
# name, and the two scripts are the only answer:
#
#   scripts/start-agent-runtime.sh    to start
#   scripts/stop-agent-runtime.sh     to stop
#
# Export LOCAL_AGENT_TERMINAL_AUTOSTART=1 before this file is sourced to restore
# the terminal-driven lifecycle.
if [[ "${LOCAL_AGENT_TERMINAL_AUTOSTART:-0}" == "1" && -z "${LOCAL_AGENT_TERMINAL_SESSION_STARTED:-}" ]]; then
  export LOCAL_AGENT_TERMINAL_SESSION_STARTED=1
  "$LOCAL_AGENT_REPO/scripts/pi_terminal_session.sh" enter "$$" "$LOCAL_AGENT_SHELL_SESSION_ID" >/dev/null 2>&1 &!
  trap '"$LOCAL_AGENT_REPO/scripts/pi_terminal_session.sh" leave "$$" "$LOCAL_AGENT_SHELL_SESSION_ID" >/dev/null 2>&1' EXIT
fi

unalias pi 2>/dev/null

pi() {
  "$LOCAL_AGENT_REPO/scripts/pi.sh" "$@"
}

alias pi='noglob pi'
