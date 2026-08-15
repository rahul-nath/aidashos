#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Who currently owns each resident coordination loop, for the start and stop
# scripts.
#
# The enqueue drainer and the ledger dispatcher are singletons over the
# coordination database rather than over a checkout, and each one's owner is
# whichever process holds its advisory lock. Both scripts need the same answer
# from the same place: start refuses to spawn a duplicate, stop reaches the real
# owner even when this checkout is not the one that launched it. Two copies of
# the lookup would be two chances for those answers to disagree.
#
# Source this file, call `read_resident_loop_owners` once, then query it.

# One subprocess for the whole script, and one that answers by calling the
# function rather than by re-parsing printed JSON. Every later lookup is shell
# string handling.
read_resident_loop_owners() {
  RESIDENT_LOOP_OWNERS="$(uv run python "$ROOT/scripts/resident_loop_owners.py" 2>/dev/null)" \
    || RESIDENT_LOOP_OWNERS=""
}

_resident_loop_field() {
  local name="$1"
  local field="$2"
  printf '%s\n' "$RESIDENT_LOOP_OWNERS" | awk -F'\t' -v name="$name" -v field="$field" \
    '$1 == name { print $field; exit }'
}

# Empty when the loop is unowned, or when the query could not run at all. Both
# mean the same thing to a caller: proceed, and let the lock decide.
resident_loop_owner_description() {
  _resident_loop_field "$1" 3
}

# Empty when the loop is unowned or its owner is on another host, which is the
# one case where signalling this pid would hit an unrelated process.
resident_loop_owner_pid() {
  _resident_loop_field "$1" 2
}
