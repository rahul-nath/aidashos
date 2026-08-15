# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Transport implementations for the typed coordination command protocol.

Only this module knows that a command's transportable representation is an argv
list handled by the packaged coordination CLI. Both transports use that
representation, deliberately: the subprocess one has no choice, and the
in-process one reuses it so there is one grammar rather than two dispatchers
that can disagree about what a command takes.

What separates them is a process boundary, and which side of it a command runs
on is a real decision rather than an implementation detail. An external agent
calling this repository over MCP genuinely needs the boundary. The application
calling its own functions does not, and paid roughly 0.4s of interpreter
startup per command for it, plus a class of stdio failures that only exist
because a resident daemon was forking children.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..settings import CoordinationTransportKind, Settings
from .contracts import (
    CoordinationCommand,
    CoordinationCommandName,
    RawCoordinationCommand,
    spill_payload_location,
)
from .ledger_selection import (
    CoordinationLedgerSelection,
    applied_ledger_selection,
    default_coordination_root,
)

_FILE_TRANSPORT_THRESHOLD_BYTES = 64 * 1024


def coordination_script_path() -> Path:
    return Path(__file__).resolve().parents[3] / "agent_coordination_mcp.py"


def coordination_root(settings: Settings | None = None, root: Path | None = None) -> Path:
    return CoordinationLedgerSelection.resolve(settings, root).root


def coordination_backend(settings: Settings | None = None, root: Path | None = None) -> str:
    return CoordinationLedgerSelection.resolve(settings, root).backend


def coordination_database_url(settings: Settings | None = None) -> str | None:
    return CoordinationLedgerSelection.resolve(settings).database_url


class CoordinationTransport(Protocol):
    """Execute one typed command and return its serialized ledger response."""

    def execute(self, command: CoordinationCommand) -> Mapping[str, object]: ...


def _require_ok(
    command: CoordinationCommand,
    payload: Mapping[str, object],
) -> Mapping[str, object]:
    if not payload.get("ok"):
        raise RuntimeError(f"coordination command {command.name.value!r} failed: {payload}")
    return payload


@dataclass(frozen=True)
class SubprocessCoordinationTransport:
    """CLI-parity transport, for a ledger core that is a separate program.

    ``timeout_seconds`` is a wall-clock bound only this transport can offer: it
    kills a child. Nothing in-process can be interrupted the same way, so the
    bound is a property of the boundary rather than of the protocol.
    """

    selection: CoordinationLedgerSelection
    timeout_seconds: int = 30

    def execute(self, command: CoordinationCommand) -> Mapping[str, object]:
        ledger_root = self.selection.root
        argv, temporary_content_path = _spill_large_payload(command, ledger_root)
        process_command = [
            sys.executable,
            str(coordination_script_path()),
            "--root",
            str(ledger_root),
            *argv,
        ]
        env = self.selection.child_environment(os.environ)
        try:
            # All three standard streams are provided explicitly: a resident
            # daemon parent can hold a revoked tty as fd 0, and a child that
            # inherits it dies at interpreter startup before printing JSON.
            process = subprocess.run(
                process_command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                env=env,
                check=False,
            )
        finally:
            if temporary_content_path is not None:
                temporary_content_path.unlink(missing_ok=True)
        try:
            payload = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"coordination command {command.name.value!r} returned non-JSON output: "
                f"{process.stderr}"
            ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"coordination command {command.name.value!r} returned non-object JSON"
            )
        return _require_ok(command, payload)


@dataclass
class InProcessCoordinationTransport:
    """Library transport for a ledger core implementing the same command sum."""

    execute_command: Callable[[CoordinationCommand], Mapping[str, object]]

    def execute(self, command: CoordinationCommand) -> Mapping[str, object]:
        return _require_ok(command, self.execute_command(command))


@dataclass(frozen=True)
class PackagedCoordinationCore:
    """The coordination CLI as a function, pointed at one ledger.

    The payload spilling the subprocess transport needs has no counterpart here:
    it exists because an argv list is an operating system limit, and a list
    handed to `parse_args` in this process is not.

    The JSON round trip does have a counterpart, and is the reason it is kept.
    A child can only ever hand back what survives `json.dumps`; this could hand
    back a `datetime`, a `Path`, or a `set`, and a caller that started depending
    on one would break the day an operator selected the other transport. Same
    value space, or they are not the same protocol.
    """

    selection: CoordinationLedgerSelection

    def __call__(self, command: CoordinationCommand) -> Mapping[str, object]:
        from .cli import execute_argv

        with applied_ledger_selection(self.selection):
            payload = execute_argv(command.to_argv())
        return json.loads(json.dumps(payload, sort_keys=True))


@dataclass
class RecordingCoordinationTransport:
    """Deterministic test provider that records typed values, not argv text."""

    responses: dict[CoordinationCommandName, list[Mapping[str, object]]] = field(
        default_factory=dict
    )
    commands: list[CoordinationCommand] = field(default_factory=list)

    def execute(self, command: CoordinationCommand) -> Mapping[str, object]:
        self.commands.append(command)
        queued = self.responses.get(command.name)
        if queued:
            return queued.pop(0)
        return {"ok": True}


@dataclass(frozen=True)
class CoordinationTransportFactory:
    """Construct the runtime transport from the ledger configuration."""

    @staticmethod
    def create(
        *,
        settings: Settings | None = None,
        root: Path | None = None,
        timeout_seconds: int = 30,
    ) -> CoordinationTransport:
        selection = CoordinationLedgerSelection.resolve(settings, root)
        kind = _transport_kind(settings)
        if kind is CoordinationTransportKind.IN_PROCESS:
            return InProcessCoordinationTransport(PackagedCoordinationCore(selection))
        return SubprocessCoordinationTransport(
            selection=selection,
            timeout_seconds=timeout_seconds,
        )


def _transport_kind(settings: Settings | None) -> CoordinationTransportKind:
    if settings is not None:
        return settings.coordination_transport
    from ..settings import get_settings

    try:
        return get_settings().coordination_transport
    except Exception:
        # A caller with no settings and an unloadable environment still gets a
        # working ledger; it is the same default the model declares.
        return CoordinationTransportKind.IN_PROCESS


def _spill_large_payload(
    command: CoordinationCommand,
    ledger_root: Path,
) -> tuple[list[str], Path | None]:
    argv = command.to_argv()
    location = spill_payload_location(command)
    if location is None:
        return argv, None
    payload_index, file_flag = location
    if payload_index >= len(argv):
        return argv, None
    content = argv[payload_index]
    if len(content.encode("utf-8")) <= _FILE_TRANSPORT_THRESHOLD_BYTES:
        return argv, None

    transport_dir = ledger_root / ".agent_coordination"
    transport_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=transport_dir,
        prefix="coordination-payload-",
        suffix=".txt",
        delete=False,
    ) as handle:
        handle.write(content)
        temporary_content_path = Path(handle.name)

    if command.name is CoordinationCommandName.SUBMIT_ARTIFACT:
        rewritten = [
            *argv[:payload_index],
            *argv[payload_index + 1 :],
            file_flag.value,
            str(temporary_content_path),
        ]
    else:
        source_flag_index = payload_index - 1
        rewritten = [
            *argv[:source_flag_index],
            file_flag.value,
            str(temporary_content_path),
            *argv[payload_index + 1 :],
        ]
    return rewritten, temporary_content_path


def legacy_command(argv: list[str]) -> RawCoordinationCommand:
    return RawCoordinationCommand.from_argv(argv)


__all__ = [
    "CoordinationTransport",
    "CoordinationTransportFactory",
    "InProcessCoordinationTransport",
    "PackagedCoordinationCore",
    "RecordingCoordinationTransport",
    "SubprocessCoordinationTransport",
    "coordination_backend",
    "coordination_database_url",
    "coordination_root",
    "coordination_script_path",
    "default_coordination_root",
    "legacy_command",
]
