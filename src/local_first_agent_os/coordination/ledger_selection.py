# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Which ledger a coordination command talks to, as one value.

Four channels select a ledger, and any two of them disagreeing sends a command
to the wrong database with no error anywhere. They were resolved in five places
before this module: three functions in ``transport``, an inline ``os.environ``
read beside them, and four more readers in ``store``. This is the one place that
knows the set, so a transport can hand it to a child process or establish it in
this one without either restating the list.

The two ways of establishing it are not symmetric, and that asymmetry is the
whole reason this module has a lock in it. A child process gets its own copy of
the environment, so a subprocess transport can select per call and nothing else
in the system notices. This process has exactly one environment and one
``ROOT_OVERRIDE``, so an in-process transport is mutating state its own siblings
are reading.

A selection is applied and left in place rather than restored on the way out.
Restoring means moving the root back, and moving the root drops every cached
connection, so a process whose ambient root differs from its ledger root would
reconnect twice per command forever. Leaving it is also the more correct of the
two: the only readers of ``ROOT_OVERRIDE`` are the coordination ledger's own
path helpers, and pointing those at the ledger root is what a command already
asked for.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..settings import Settings
from .store import repo_root, set_root

COORDINATION_BACKEND_ENV = "AGENT_COORDINATION_BACKEND"
COORDINATION_DATABASE_URL_ENV = "AGENT_COORDINATION_DATABASE_URL"
COORDINATION_SCHEMA_ENV = "AGENT_COORDINATION_SCHEMA"


def default_coordination_root() -> Path:
    return Path.home() / ".local-agent" / "coordination" / "local_first_agent_os"


@dataclass(frozen=True)
class CoordinationLedgerSelection:
    """The four values that decide which ledger a command reaches.

    ``root`` still matters under Postgres even though it no longer picks the
    database: file claims normalize their paths against it, and the JSONL event
    log lives under it. A transport that got the other three right and this one
    wrong would write correct rows about the wrong paths.
    """

    root: Path
    backend: str
    database_url: str | None
    schema: str | None

    @classmethod
    def resolve(
        cls,
        settings: Settings | None = None,
        root: Path | None = None,
    ) -> CoordinationLedgerSelection:
        return cls(
            root=_resolve_root(settings, root),
            backend=_resolve_backend(settings),
            database_url=_resolve_database_url(settings),
            schema=_resolve_schema(),
        )

    @classmethod
    def of_this_process(cls) -> CoordinationLedgerSelection:
        """What a command would reach right now with nothing else applied."""

        return cls(
            root=repo_root(),
            backend=_resolve_backend(None),
            database_url=os.environ.get(COORDINATION_DATABASE_URL_ENV),
            schema=_resolve_schema(),
        )

    def child_environment(self, base: Mapping[str, str]) -> dict[str, str]:
        """The environment a child process needs to reach this same ledger.

        A missing database URL is removed rather than left inherited: the
        selection said there is none, and an inherited one would silently
        contradict it.
        """

        environment = dict(base)
        environment[COORDINATION_BACKEND_ENV] = self.backend
        if self.database_url:
            environment[COORDINATION_DATABASE_URL_ENV] = self.database_url
        else:
            environment.pop(COORDINATION_DATABASE_URL_ENV, None)
        if self.schema:
            environment[COORDINATION_SCHEMA_ENV] = self.schema
        return environment


def _resolve_root(settings: Settings | None, root: Path | None) -> Path:
    if root is not None:
        return root.expanduser()
    environment_root = os.environ.get("AGENT_COORDINATION_ROOT")
    if environment_root:
        return Path(environment_root).expanduser()
    if settings is not None:
        return settings.coordination_root.expanduser()
    return default_coordination_root()


def _resolve_backend(settings: Settings | None) -> str:
    """Which ledger engine a command should use.

    There is only one, and this exists so the answer keeps arriving through the
    same channel a child already reads. A root used to imply SQLite, which made
    `--root` mean two things at once: which directory, and which database engine.
    """

    if settings is not None:
        return settings.coordination_backend
    return (
        os.environ.get(COORDINATION_BACKEND_ENV)
        or os.environ.get("LOCAL_AGENT_COORDINATION_BACKEND")
        or "postgres"
    )


def _resolve_database_url(settings: Settings | None) -> str | None:
    if settings is not None:
        return settings.coordination_database_url or settings.database_url
    return (
        os.environ.get(COORDINATION_DATABASE_URL_ENV)
        or os.environ.get("LOCAL_AGENT_COORDINATION_DATABASE_URL")
        or os.environ.get("LOCAL_AGENT_DATABASE_URL")
    )


def _resolve_schema() -> str | None:
    raw = os.environ.get(COORDINATION_SCHEMA_ENV)
    return raw.strip() or None if raw is not None else None


def _set_or_clear(name: str, value: str | None) -> None:
    if value:
        if os.environ.get(name) != value:
            os.environ[name] = value
    else:
        os.environ.pop(name, None)


class ConflictingLedgerSelection(RuntimeError):
    """A thread asked for a second ledger while holding one open.

    Waiting would deadlock on this thread's own in-flight command, so this is
    raised instead. It is a programmer error: a command handler that reaches
    back into the ledger must reach the ledger it was already given.
    """


class _ProcessLedgerSelection:
    """Guards the one environment and one ``ROOT_OVERRIDE`` this process has.

    Commands sharing a selection run concurrently and take nothing but an
    uncontended lock acquire; a command wanting a different one waits for the
    in-flight batch to drain before switching. That ordering is the point. A
    sticky selection with no barrier is a data race whose symptom is a query
    against the wrong database, which no assertion downstream would catch.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._applied: CoordinationLedgerSelection | None = None
        self._in_flight = 0
        self._holders: dict[int, CoordinationLedgerSelection] = {}

    @contextmanager
    def entered(self, selection: CoordinationLedgerSelection) -> Iterator[None]:
        thread = threading.get_ident()
        with self._condition:
            held = self._holders.get(thread)
            if held is not None and held != selection:
                raise ConflictingLedgerSelection(
                    "a coordination command on this thread is already using "
                    f"{held.root} / {held.database_url}; it cannot nest a command "
                    f"against {selection.root} / {selection.database_url}"
                )
            if held is None:
                while self._in_flight and self._applied != selection:
                    self._condition.wait()
                self._apply(selection)
                self._in_flight += 1
                self._holders[thread] = selection
        try:
            yield
        finally:
            if held is None:
                with self._condition:
                    self._in_flight -= 1
                    del self._holders[thread]
                    self._condition.notify_all()

    def _apply(self, selection: CoordinationLedgerSelection) -> None:
        """Bring the process to ``selection``, touching only what disagrees.

        Compared field by field against the live process rather than against the
        last selection applied, so a ``set_root`` from somewhere else does not
        leave this believing something the process no longer does.
        """

        _set_or_clear(COORDINATION_BACKEND_ENV, selection.backend)
        _set_or_clear(COORDINATION_DATABASE_URL_ENV, selection.database_url)
        _set_or_clear(COORDINATION_SCHEMA_ENV, selection.schema)
        # `set_root` drops every cached connection, so it is called only when the
        # root actually moves. Calling it per command would undo the connection
        # reuse that took the WorkUnit suite from ~13s to ~2s.
        if repo_root() != selection.root.resolve():
            set_root(str(selection.root))
        self._applied = selection


_PROCESS_SELECTION = _ProcessLedgerSelection()


@contextmanager
def applied_ledger_selection(selection: CoordinationLedgerSelection) -> Iterator[None]:
    """Point this process at ``selection`` for the body, and hold it there."""

    with _PROCESS_SELECTION.entered(selection):
        yield


__all__ = [
    "COORDINATION_BACKEND_ENV",
    "COORDINATION_DATABASE_URL_ENV",
    "COORDINATION_SCHEMA_ENV",
    "ConflictingLedgerSelection",
    "CoordinationLedgerSelection",
    "applied_ledger_selection",
    "default_coordination_root",
]
