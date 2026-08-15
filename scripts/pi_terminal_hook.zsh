hook_file="${${(%):-%x}:A}"
export LOCAL_AGENT_REPO="${LOCAL_AGENT_REPO:-${hook_file:h:h}}"
export LOCAL_AGENT_SHELL_SESSION_ID="${LOCAL_AGENT_SHELL_SESSION_ID:-shell-$$}"

if [[ -z "${LOCAL_AGENT_TERMINAL_SESSION_STARTED:-}" ]]; then
  export LOCAL_AGENT_TERMINAL_SESSION_STARTED=1
  "$LOCAL_AGENT_REPO/scripts/pi_terminal_session.sh" enter "$$" "$LOCAL_AGENT_SHELL_SESSION_ID" >/dev/null 2>&1 &!
  trap '"$LOCAL_AGENT_REPO/scripts/pi_terminal_session.sh" leave "$$" "$LOCAL_AGENT_SHELL_SESSION_ID" >/dev/null 2>&1' EXIT
fi

unalias pi 2>/dev/null

pi() {
  "$LOCAL_AGENT_REPO/scripts/pi.sh" "$@"
}

alias pi='noglob pi'
