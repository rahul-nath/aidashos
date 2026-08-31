# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Machine-wide ownership of the resident coordination loops.

The enqueue drainer and the ledger dispatcher are singletons over the
coordination database, not over a directory. `start-agent-runtime.sh` used to
guard them with `$ROOT/.local_agent/run/<name>.pid`, which is a per-checkout
file, so every git worktree got its own pair of loops all polling the same
Postgres. Claiming stayed correct because both claim paths use `FOR UPDATE SKIP
LOCKED`, but *which checkout's code* ran a given WorkUnit became a race, and a
worktree on an older branch could execute work queued against newer contracts.

The guard now lives at the scope of the thing being contended. A session-level
advisory lock on the coordination database is held for the lifetime of the loop
process, and the holder's identity rides on that same connection's
`application_name`. One connection is therefore the single source of truth for
both "is this loop owned" and "by whom": there is no pid file to go stale, no
PID-reuse window, and a crashed owner releases the lock the moment its
connection drops.

The one failure this does not cover is the lock connection dying under a loop
that keeps running. That requires the Postgres server to go away, which also
breaks the connection the loop drains through, so the loop fails on its next
poll regardless.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..runtime_source import runtime_checkout, runtime_revision
from .store import ok, postgres_database_url, postgres_schema

# `application_name` is a Postgres name, capped at NAMEDATALEN - 1 bytes. Longer
# values are silently truncated, which would corrupt the encoding below rather
# than fail, so the encoder budgets against this explicitly. Every field except
# the checkout is fixed-width for that reason; the checkout absorbs what is left.
_APPLICATION_NAME_LIMIT = 63
_OWNER_PREFIX = "laos"
_OWNER_FIELDS = 5
_REVISION_DISPLAY_LENGTH = 7
_HOST_FINGERPRINT_LENGTH = 8
_TRUNCATION_MARKER = "~"


class ResidentLoop(StrEnum):
    """The loops that may exist at most once per coordination database.

    The singleton values are processes `start-agent-runtime.sh` leaves running.
    Their values are the names used for log files, so an operator
    reading a lock holder and an operator reading `.local_agent/logs` see the
    same word.
    """

    ENQUEUE_DRAINER = "work-unit-enqueue-drainer"
    LEDGER_DISPATCHER = "ledger-dispatcher"
    CRASH_RECONCILER = "work-unit-crash-reconciler"
    """Unattended recovery of executions that died without recording a halt.

    Started only after the process-containment probe succeeds, because an
    automatic retry is safe only when the successor keeps the plan's authority.
    """

    REFINERY = "refinery"
    """One integration run per target project, holding that project's queue.

    Always scoped by ``target_project_id``, never held unscoped. Per project
    rather than global because two projects share nothing - different
    repositories, different integrated branches, different verification commands
    - and serializing them would make a slow project's test suite the pacing item
    for every other project on the machine.

    Two refineries on one repository would each compute a fast-forward from a
    base the other was about to invalidate, and one would silently lose a batch.
    """

    REFINERY_FLEET = "refinery-fleet"
    """One resident that discovers projects before taking per-project locks.

    The fleet lock prevents two launchd residents from duplicating the scan.
    Each actual integration still takes ``REFINERY`` scoped by project, so a
    manual project drain and the resident fleet cannot race on one repository.
    """

    @property
    def scoped_by(self) -> str | None:
        """What a scope string means for this loop, when it needs one.

        `resident_loop_owners` reads every loop unscoped, which is the right
        answer for the ones an operator starts one of. It is not the whole answer
        for a loop that only ever runs scoped, and reporting `owned: false` for a
        machine running three refineries would be a confident lie. This is what
        lets the report say which question it just answered.
        """

        return "target_project_id" if self is ResidentLoop.REFINERY else None


@dataclass(frozen=True)
class ResidentLoopOwner:
    """Which process, on which host, from which checkout, owns a loop."""

    pid: int
    revision: str | None
    checkout: str
    host_fingerprint: str
    checkout_truncated: bool = False

    @property
    def is_on_this_host(self) -> bool:
        """Whether `pid` refers to a process this machine can signal.

        The pid is self-reported by whoever holds the lock, and several hosts
        may share one coordination database. A pid from another host names a
        real process here too, just the wrong one, so nothing may act on this
        pid without first agreeing on the machine it came from.
        """

        return self.host_fingerprint == _host_fingerprint()

    def describe(self) -> str:
        checkout = self.checkout if self.checkout else "unknown checkout"
        if self.checkout_truncated:
            checkout = f"...{checkout}"
        revision = self.revision or "unknown revision"
        where = "" if self.is_on_this_host else " on another host"
        return f"pid {self.pid} in {checkout} at {revision}{where}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "revision": self.revision,
            "checkout": self.checkout,
            "checkout_truncated": self.checkout_truncated,
            "host_fingerprint": self.host_fingerprint,
            "is_on_this_host": self.is_on_this_host,
        }


@dataclass(frozen=True)
class ResidentLoopHeld:
    """This process owns the loop and may run it."""

    loop: ResidentLoop
    owner: ResidentLoopOwner


@dataclass(frozen=True)
class ResidentLoopBusy:
    """Another process owns the loop. `owner` is None when it cannot be read.

    An unreadable owner is not an error. `pg_stat_activity` shows another
    session's `application_name` only to a superuser or a member of
    `pg_read_all_stats`, so a locked-out-but-unidentified holder is a normal
    outcome under a restricted role, and it is still a definite answer to the
    question that matters: this process must not run the loop.
    """

    loop: ResidentLoop
    owner: ResidentLoopOwner | None

    def describe(self) -> str:
        if self.owner is None:
            return f"{self.loop.value} is already owned by another process"
        return f"{self.loop.value} is already owned by {self.owner.describe()}"


# A lease is one or the other, never a flag plus a maybe-populated owner.
ResidentLoopLease = ResidentLoopHeld | ResidentLoopBusy


def _lock_key(loop: ResidentLoop, scope: str | None) -> int:
    """The advisory lock key for one loop, in one namespace, over one scope.

    Advisory keys are cluster-wide, so the schema has to be part of the key or
    a test schema's dispatcher would lock out production's. `scope` is part of
    it for the same reason one level down: the dispatcher's `--tier` exists so
    an operator can deliberately run one process per tier, and those processes
    do not duplicate each other. Keying on the scope makes the lock reject only
    what is actually a duplicate: a second dispatcher claiming the same tiers.

    The result is masked to 63 bits so that `classid << 32 | objid`, which is
    how `pg_locks` decomposes a key, stays inside a signed `bigint` when the
    holder is looked up.
    """

    namespace = f"resident-loop:{postgres_schema() or 'default'}:{loop.value}:{scope or 'any'}"
    digest = hashlib.sha256(namespace.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, "big") & 0x7FFF_FFFF_FFFF_FFFF


def _host_fingerprint() -> str:
    """A short, stable stand-in for this machine's name.

    The hostname itself does not fit the `application_name` budget and is not
    needed: the only question asked of it is whether two records came from the
    same machine, and equality survives hashing.
    """

    digest = hashlib.sha256(os.uname().nodename.encode("utf-8")).hexdigest()
    return digest[:_HOST_FINGERPRINT_LENGTH]


def _encode_owner(pid: int, revision: str | None, checkout: Path) -> str:
    """Pack the owner's identity into one `application_name`.

    Postgres truncates an over-long name instead of rejecting it, so the
    checkout is trimmed here to whatever the fixed fields leave over. The tail
    is what survives: worktree directories in this repo are `<topic>-<hash>`,
    and the hash is what makes two of them different.
    """

    short_revision = (revision or "")[:_REVISION_DISPLAY_LENGTH] or "unknown"
    fixed = f"{_OWNER_PREFIX}:{pid}:{short_revision}:{_host_fingerprint()}:"
    budget = _APPLICATION_NAME_LIMIT - len(fixed)
    name = checkout.name or str(checkout)
    if budget <= len(_TRUNCATION_MARKER):
        # Nothing meaningful fits. Drop the checkout rather than emit a
        # fragment that would decode as a different directory.
        return f"{fixed}{_TRUNCATION_MARKER}"
    if len(name) > budget:
        name = _TRUNCATION_MARKER + name[-(budget - len(_TRUNCATION_MARKER)) :]
    return f"{fixed}{name}"


def _decode_owner(application_name: str | None) -> ResidentLoopOwner | None:
    if not application_name:
        return None
    parts = application_name.split(":", _OWNER_FIELDS - 1)
    if len(parts) != _OWNER_FIELDS or parts[0] != _OWNER_PREFIX:
        # Something else holds a lock on this key, or an older runtime wrote a
        # name this version does not understand. Either way the identity is
        # unknown, which the busy lease already represents.
        return None
    try:
        pid = int(parts[1])
    except ValueError:
        return None
    revision = None if parts[2] == "unknown" else parts[2]
    checkout = parts[4]
    truncated = checkout.startswith(_TRUNCATION_MARKER)
    if truncated:
        checkout = checkout[len(_TRUNCATION_MARKER) :]
    return ResidentLoopOwner(
        pid=pid,
        revision=revision,
        checkout=checkout,
        host_fingerprint=parts[3],
        checkout_truncated=truncated,
    )


def _connect(application_name: str) -> Any:
    import psycopg

    # Autocommit because a session-level advisory lock outlives transactions
    # and an open idle transaction would hold back vacuum for the life of the
    # loop, which is forever.
    return psycopg.connect(
        postgres_database_url(),
        autocommit=True,
        application_name=application_name,
    )


_HOLDER_QUERY = """
SELECT activity.application_name AS application_name
FROM pg_locks AS locks
JOIN pg_stat_activity AS activity ON activity.pid = locks.pid
WHERE locks.locktype = 'advisory'
  AND locks.granted
  AND locks.objsubid = 1
  AND ((locks.classid::bigint << 32) + locks.objid::bigint) = %s
LIMIT 1
"""


def _read_holder(connection: Any, key: int) -> ResidentLoopOwner | None:
    row = connection.execute(_HOLDER_QUERY, (key,)).fetchone()
    if row is None:
        return None
    # The store's connections use a dict row factory; this module opens its own
    # with the driver default, so read positionally.
    application_name = row[0] if not isinstance(row, dict) else row["application_name"]
    return _decode_owner(application_name)


def resident_loop_owner(loop: ResidentLoop, scope: str | None = None) -> ResidentLoopOwner | None:
    """Who owns `loop` right now, without attempting to take it.

    This is a question, so it never acquires. A caller that intends to run the
    loop must still use `hold_resident_loop`: between this answer and that
    acquisition another process may take the lock, and only the acquisition is
    a decision.
    """

    connection = _connect(f"{_OWNER_PREFIX}-probe")
    try:
        return _read_holder(connection, _lock_key(loop, scope))
    finally:
        connection.close()


def resident_loop_owners() -> dict[ResidentLoop, ResidentLoopOwner | None]:
    """Current owner of every unscoped resident loop.

    This is what an operator and a start script want: the loops
    `start-agent-runtime.sh` runs. Deliberately tier-scoped dispatchers are
    addressed one at a time through `resident_loop_owner`.
    """

    connection = _connect(f"{_OWNER_PREFIX}-probe")
    try:
        return {loop: _read_holder(connection, _lock_key(loop, None)) for loop in ResidentLoop}
    finally:
        connection.close()


@contextmanager
def hold_resident_loop(
    loop: ResidentLoop,
    scope: str | None = None,
) -> Iterator[ResidentLoopLease]:
    """Try to become the machine's owner of `loop` for the duration of the block.

    Yields the lease rather than raising on contention: a second runtime
    finding the loop already owned is the normal, expected outcome of starting
    the runtime twice, not a failure anyone needs a traceback for. The caller
    decides what to do with a `ResidentLoopBusy`, and the type makes it
    impossible to proceed without having looked.
    """

    pid = os.getpid()
    checkout = runtime_checkout()
    revision = runtime_revision()
    key = _lock_key(loop, scope)
    connection = _connect(_encode_owner(pid, revision, checkout))
    try:
        row = connection.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (key,)).fetchone()
        acquired = bool(row[0] if not isinstance(row, dict) else row["acquired"])
        if not acquired:
            yield ResidentLoopBusy(loop=loop, owner=_read_holder(connection, key))
            return
        yield ResidentLoopHeld(
            loop=loop,
            owner=ResidentLoopOwner(
                pid=pid,
                revision=revision,
                checkout=checkout.name,
                host_fingerprint=_host_fingerprint(),
            ),
        )
    finally:
        # Closing the connection releases a session-level advisory lock, so the
        # explicit unlock is only to make the release immediate and legible in
        # `pg_locks` if closing is ever deferred. The close below is the actual
        # guarantee, which is why a failure here is not worth propagating.
        with contextlib.suppress(Exception):
            connection.execute("SELECT pg_advisory_unlock(%s)", (key,))
        connection.close()


def describe_resident_loops() -> dict[str, Any]:
    """The coordination-command view of who owns each resident loop.

    This is what `start-agent-runtime.sh` asks before spawning, so a second
    worktree reports the real owner instead of printing "Started" for a process
    that is about to exit. The lock, not this answer, is what actually prevents
    the duplicate; this exists so the operator is told the truth on their
    terminal rather than in a log file they have no reason to open.
    """

    owners = resident_loop_owners()
    return ok(
        loops=[
            {
                "loop": loop.value,
                "owned": owner is not None,
                # Rendered here so the shell that prints it does not have to
                # reimplement the phrasing, and so the two cannot drift.
                "description": owner.describe() if owner else None,
                "owner": owner.to_payload() if owner else None,
                # Non-null means this row answered the unscoped question for a
                # loop that only ever runs scoped, so `owned: false` here says
                # nothing about whether any are running. Ask
                # `resident_loop_owner(loop, scope)` for those.
                "scoped_by": loop.scoped_by,
            }
            for loop, owner in owners.items()
        ],
    )


__all__ = [
    "ResidentLoop",
    "ResidentLoopBusy",
    "ResidentLoopHeld",
    "ResidentLoopLease",
    "ResidentLoopOwner",
    "describe_resident_loops",
    "hold_resident_loop",
    "resident_loop_owner",
    "resident_loop_owners",
]
