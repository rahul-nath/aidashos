# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Who owns each resident coordination loop, in a shape a shell script can read.

`start-agent-runtime.sh` needs this to refuse to start a duplicate loop, and
`stop-agent-runtime.sh` needs it to reach a loop this checkout did not launch.
Both want the same answer, so both read it from here.

This is a file rather than a heredoc inside the scripts. Bash 3.2, which is what
macOS ships and what those scripts run under, parses the body of a heredoc that
appears inside a `$(...)`, so an apostrophe in a Python comment is a syntax error
in the shell script that contains it. That is a trap with no warning and no
relation to what the code does, and the way out of it is to not embed Python in
shell.

One line per owned loop, tab separated:

    <loop name>\t<pid, when signalable from this host>\t<human description>
"""

from __future__ import annotations

from local_first_agent_os.coordination.resident_loop import resident_loop_owners

FIELD_SEPARATOR = "\t"


def main() -> int:
    for loop, owner in resident_loop_owners().items():
        if owner is None:
            continue
        # A pid is reported only for this host. It is self-reported by whoever
        # holds the lock, and a coordination database can be shared, so a pid
        # from elsewhere names a real process here too, belonging to someone
        # else. The stop script signals what this column holds.
        pid = str(owner.pid) if owner.is_on_this_host else ""
        print(FIELD_SEPARATOR.join((loop.value, pid, owner.describe())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
