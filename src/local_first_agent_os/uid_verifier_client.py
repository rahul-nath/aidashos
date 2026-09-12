# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Authenticated host transport for the installed exclusive-UID verifier owner."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import math
import signal
import socket
import subprocess
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Literal

HELPER_SOCKET = Path("/private/var/db/aidashos-verifier-uid/control.sock")
_SCHEMA = "aidashos.uid-gate.v1"
_MAX_MESSAGE_BYTES = 262144
_MAX_CAPTURE_BYTES = 64 * 1024 * 1024
_CLEANUP_SECONDS = 20.0


class UidVerifierUnavailable(RuntimeError):
    """The installed owner did not provide complete authenticated launch evidence."""


def _text(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise UidVerifierUnavailable("helper response requires nonempty text")
    return value


def _integer(value: object) -> int:
    if type(value) is not int:
        raise UidVerifierUnavailable("helper response requires an integer")
    return value


def _record(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise UidVerifierUnavailable("helper response requires a record")
    return value


def _digest(value: object) -> str:
    value = _text(value)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise UidVerifierUnavailable("helper response requires a SHA-256 binding")
    return value


def _root_peer(connection: socket.socket) -> None:
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    function = library.getpeereid
    function.argtypes = (
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
    )
    function.restype = ctypes.c_int
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    if function(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) or uid.value != 0:
        raise UidVerifierUnavailable("verifier helper peer is not authenticated root")


@dataclass(frozen=True)
class GateStaging:
    handle: str
    gid: int
    directory: Path
    source: Path
    toolchain: Path


@dataclass(frozen=True)
class PreparedLaunch:
    handle: str
    uid: int
    gid: int
    home: Path
    scratch: Path


@dataclass(frozen=True)
class LaunchIdentity:
    pid: int
    pgid: int
    digest: str
    requested_digest: str


@dataclass(frozen=True)
class ProcessOutput:
    channel: Literal["stdout", "stderr"]
    data: bytes


class UidProcess:
    """Output is retained independently from the final UID cleanup receipt."""

    def __init__(
        self, owner: UidVerifierClient, prepared: PreparedLaunch, command: Sequence[str]
    ) -> None:
        self.owner, self.prepared, self.command = owner, prepared, tuple(command)
        self.identity: LaunchIdentity | None = None
        self.returncode: int | None = None
        self.receipt: dict[str, object] | None = None
        self.requested_digest: str | None = None
        self._condition = threading.Condition()
        self._events: deque[ProcessOutput] = deque()
        self._stdout: list[bytes] = []
        self._stderr: list[bytes] = []
        self._captured_bytes = 0

    @property
    def pid(self) -> int:
        if self.identity is None:
            raise UidVerifierUnavailable("helper has not acknowledged the actual launch")
        return self.identity.pid

    def _output(self, output: ProcessOutput) -> None:
        with self._condition:
            if self.receipt is not None:
                raise UidVerifierUnavailable("helper emitted output after its terminal receipt")
            self._captured_bytes += len(output.data)
            if self._captured_bytes > _MAX_CAPTURE_BYTES:
                raise UidVerifierUnavailable("native launch exceeded its bounded evidence capture")
            (self._stdout if output.channel == "stdout" else self._stderr).append(output.data)
            self._events.append(output)
            self._condition.notify_all()

    def _completed(self, code: int, receipt: dict[str, object]) -> None:
        with self._condition:
            if self.receipt is not None:
                raise UidVerifierUnavailable("helper emitted duplicate terminal launch evidence")
            self.returncode, self.receipt = code, receipt
            self._condition.notify_all()

    def poll(self) -> int | None:
        self.owner._check()
        with self._condition:
            return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        deadline = self.owner.deadline + _CLEANUP_SECONDS
        if timeout is not None:
            deadline = min(deadline, monotonic() + timeout)
        with self._condition:
            while self.returncode is None:
                self.owner._check()
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(self.command, timeout or _CLEANUP_SECONDS)
                self._condition.wait(min(remaining, 0.1))
            return self.returncode

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]:
        self.wait(timeout)
        with self._condition:
            return b"".join(self._stdout), b"".join(self._stderr)

    def output(self, timeout: float) -> tuple[ProcessOutput, ...]:
        with self._condition:
            if not self._events and self.returncode is None:
                self._condition.wait(timeout)
            self.owner._check()
            events = tuple(self._events)
            self._events.clear()
            return events

    def send_signal(self, value: signal.Signals) -> None:
        if value is signal.SIGTERM:
            self.owner._send({"kind": "terminate", "handle": self.prepared.handle})
        elif value is signal.SIGKILL:
            self.owner._send({"kind": "cancel", "handle": self.prepared.handle})
        else:
            raise ValueError("UID launch ownership only admits TERM or forced cancellation")


class UidVerifierClient:
    """One root-authenticated connection owns all launch leases for one frozen source."""

    def __enter__(self) -> UidVerifierClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __init__(
        self,
        *,
        source_binding: str,
        deadline: float,
        process_limit: int,
        socket_path: Path = HELPER_SOCKET,
    ) -> None:
        if (
            len(source_binding) != 64
            or any(character not in "0123456789abcdef" for character in source_binding)
            or not math.isfinite(deadline)
            or deadline <= monotonic()
            or type(process_limit) is not int
            or not 8 <= process_limit <= 1024
        ):
            raise ValueError(
                "UID verifier admission requires a source, deadline, and process bound"
            )
        self.source_binding, self.deadline = source_binding, deadline
        self._condition = threading.Condition()
        self._write_lock = threading.Lock()
        self._prepare_lock = threading.Lock()
        self._opened: GateStaging | None = None
        self._prepared: deque[PreparedLaunch] = deque()
        self._known: dict[str, PreparedLaunch] = {}
        self._processes: dict[str, UidProcess] = {}
        self._failure: UidVerifierUnavailable | None = None
        self._closed_receipt: dict[str, object] | None = None
        self._closing = False
        self._connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._connection.settimeout(0.5)
        try:
            self._connection.connect(str(socket_path))
            _root_peer(self._connection)
            self._reader = threading.Thread(target=self._receive, daemon=True)
            self._reader.start()
            self._send(
                {
                    "kind": "open",
                    "schema": _SCHEMA,
                    "source_binding": source_binding,
                    "duration": min(3600.0, deadline - monotonic()),
                    "process_limit": process_limit,
                }
            )
            with self._condition:
                self._await(lambda: self._opened is not None)
        except BaseException:
            self._connection.close()
            raise

    @property
    def staging(self) -> GateStaging:
        self._check()
        assert self._opened is not None
        return self._opened

    def _check(self) -> None:
        if self._failure is not None:
            raise self._failure

    def _await(self, predicate: Callable[[], bool], seconds: float = 5.0) -> None:
        deadline = min(self.deadline + _CLEANUP_SECONDS, monotonic() + seconds)
        while not predicate():
            self._check()
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise UidVerifierUnavailable("helper did not settle before its cleanup deadline")
            self._condition.wait(min(remaining, 0.1))

    def _send(self, value: Mapping[str, object]) -> None:
        self._check()
        payload = json.dumps(value, separators=(",", ":")).encode() + b"\n"
        if len(payload) > _MAX_MESSAGE_BYTES:
            raise ValueError("UID helper request exceeds the protocol bound")
        with self._write_lock:
            try:
                self._connection.sendall(payload)
            except OSError as error:
                self._failed(error)
                raise UidVerifierUnavailable("UID helper control transport failed") from error

    def _failed(self, error: BaseException) -> None:
        with self._condition:
            self._failure = UidVerifierUnavailable(f"UID helper evidence unavailable: {error}")
            self._condition.notify_all()
        self._connection.close()

    def prepare(self, *, parent: PreparedLaunch | None, scratch: Path | None) -> PreparedLaunch:
        if (parent is None) != (scratch is None):
            raise ValueError("only the root launch can omit its parent scratch authority")
        relative = None if scratch is None else str(scratch.relative_to(self.staging.directory))
        with self._prepare_lock:
            self._send(
                {
                    "kind": "prepare",
                    "parent_handle": None if parent is None else parent.handle,
                    "scratch": relative,
                }
            )
            with self._condition:
                self._await(lambda: bool(self._prepared))
                return self._prepared.popleft()

    def launch(
        self,
        prepared: PreparedLaunch,
        command: Sequence[str],
        cwd: Path,
        environment: Mapping[str, str],
        profile: str,
    ) -> UidProcess:
        process = UidProcess(self, prepared, command)
        declaration = {
            "argv": list(command),
            "cwd": str(cwd),
            "environment": sorted(environment.items()),
            "profile": profile,
            "source_binding": self.source_binding,
        }
        process.requested_digest = hashlib.sha256(
            json.dumps(declaration, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self._condition:
            if self._known.get(prepared.handle) != prepared or prepared.handle in self._processes:
                raise ValueError("launch must select one unstarted preparation from this gate")
            self._processes[prepared.handle] = process
        self._send(
            {
                "kind": "launch",
                "handle": prepared.handle,
                "argv": list(command),
                "cwd": str(cwd),
                "environment": dict(environment),
                "profile": profile,
                "source_binding": self.source_binding,
            }
        )
        with self._condition:
            self._await(lambda: process.identity is not None or process.receipt is not None)
        if process.identity is None:
            raise UidVerifierUnavailable("prepared launch was canceled before process admission")
        return process

    def _cleanup_receipt(self, value: object, uid: int) -> dict[str, object]:
        receipt = _record(value)
        if (
            receipt.get("kind") != "cleaned"
            or receipt.get("uid") != uid
            or receipt.get("source_binding") != self.source_binding
        ):
            raise UidVerifierUnavailable("cleanup receipt does not bind this source and UID")
        _digest(receipt.get("lease_digest"))
        return receipt

    def _frame(self, raw: object) -> None:
        frame = _record(raw)
        kind = frame.get("kind")
        with self._condition:
            if self._closed_receipt is not None:
                raise UidVerifierUnavailable("helper emitted evidence after gate closure")
            if kind == "opened":
                if self._opened is not None:
                    raise UidVerifierUnavailable("helper repeated gate admission")
                self._opened = GateStaging(
                    _text(frame.get("gate_handle")),
                    _integer(frame.get("gid")),
                    Path(_text(frame.get("staging"))),
                    Path(_text(frame.get("source"))),
                    Path(_text(frame.get("toolchain"))),
                )
                opened = self._opened
                if (
                    opened.gid <= 0
                    or not opened.directory.is_absolute()
                    or opened.directory != opened.directory.resolve()
                    or opened.source != opened.directory / "source"
                    or opened.toolchain != opened.directory / "toolchain"
                ):
                    raise UidVerifierUnavailable("helper staging paths are not a closed anchor")
            elif kind == "prepared":
                prepared = PreparedLaunch(
                    _text(frame.get("handle")),
                    _integer(frame.get("uid")),
                    _integer(frame.get("gid")),
                    Path(_text(frame.get("home"))),
                    Path(_text(frame.get("scratch"))),
                )
                if (
                    self._opened is None
                    or prepared.handle in self._known
                    or prepared.uid <= 0
                    or prepared.uid == self._opened.gid
                    or prepared.uid in {item.uid for item in self._known.values()}
                    or prepared.gid != self._opened.gid
                    or prepared.home.parent != prepared.scratch.parent
                    or prepared.home.name != "home"
                    or prepared.scratch.name != "scratch"
                    or not prepared.home.is_relative_to(self._opened.directory)
                    or any(path != path.resolve() for path in (prepared.home, prepared.scratch))
                ):
                    raise UidVerifierUnavailable(
                        "helper preparation reuses identity or escapes staging"
                    )
                self._known[prepared.handle] = prepared
                self._prepared.append(prepared)
            elif kind == "gate_closed":
                assert self._opened is not None
                receipt = self._cleanup_receipt(frame.get("receipt"), self._opened.gid)
                children = frame.get("launch_receipts")
                if not isinstance(children, list):
                    raise UidVerifierUnavailable("gate closure requires every launch receipt")
                by_uid = {_record(child).get("uid"): child for child in children}
                if len(by_uid) != len(children) or set(by_uid) != {
                    prepared.uid for prepared in self._known.values()
                }:
                    raise UidVerifierUnavailable("gate closure does not cover its owned UID set")
                for uid, child in by_uid.items():
                    self._cleanup_receipt(child, _integer(uid))
                self._closed_receipt = receipt
            else:
                handle = _text(frame.get("handle"))
                process = self._processes.get(handle)
                if process is None:
                    if kind == "canceled" and handle in self._known:
                        self._cleanup_receipt(frame.get("receipt"), self._known[handle].uid)
                        self._condition.notify_all()
                        return
                    raise UidVerifierUnavailable("helper frame selects an unknown active launch")
                if kind == "launched":
                    if frame.get("uid") != process.prepared.uid or process.identity is not None:
                        raise UidVerifierUnavailable(
                            "helper launch identity conflicts with admission"
                        )
                    process.identity = LaunchIdentity(
                        _integer(frame.get("pid")),
                        _integer(frame.get("pgid")),
                        _digest(frame.get("digest")),
                        _digest(frame.get("requested_digest")),
                    )
                    if (
                        process.identity.pid <= 0
                        or process.identity.pgid != process.identity.pid
                        or process.identity.requested_digest != process.requested_digest
                    ):
                        raise UidVerifierUnavailable(
                            "helper launch does not bind the exact requested command"
                        )
                elif kind in ("stdout", "stderr"):
                    process._output(
                        ProcessOutput(
                            kind, base64.b64decode(_text(frame.get("data")), validate=True)
                        )
                    )
                elif kind in ("exit", "canceled"):
                    process._completed(
                        _integer(frame.get("code")),
                        self._cleanup_receipt(frame.get("receipt"), process.prepared.uid),
                    )
                else:
                    raise UidVerifierUnavailable("helper emitted an unknown protocol frame")
            self._condition.notify_all()

    def _receive(self) -> None:
        pending = bytearray()
        try:
            while self._closed_receipt is None:
                if monotonic() >= self.deadline + _CLEANUP_SECONDS:
                    raise UidVerifierUnavailable("helper did not attest bounded gate cleanup")
                try:
                    chunk = self._connection.recv(8192)
                except TimeoutError:
                    continue
                if not chunk:
                    raise UidVerifierUnavailable("helper disconnected without aggregate cleanup")
                pending.extend(chunk)
                while b"\n" in pending:
                    line, remainder = pending.split(b"\n", 1)
                    pending = bytearray(remainder)
                    if len(line) > _MAX_MESSAGE_BYTES:
                        raise UidVerifierUnavailable("helper response exceeds the protocol bound")
                    self._frame(json.loads(line))
                if len(pending) > _MAX_MESSAGE_BYTES:
                    raise UidVerifierUnavailable("helper response exceeds the protocol bound")
        except (OSError, ValueError, TypeError, AssertionError, UidVerifierUnavailable) as error:
            self._failed(error)

    def close(self) -> dict[str, object]:
        if self._closed_receipt is None and not self._closing:
            self._closing = True
            self._send({"kind": "close"})
        with self._condition:
            self._await(lambda: self._closed_receipt is not None, _CLEANUP_SECONDS)
        self._reader.join(timeout=1)
        self._connection.close()
        assert self._closed_receipt is not None
        return self._closed_receipt

    def require_closed_staging(self, directory: Path) -> None:
        """Only aggregate cleanup can release this gate's exact installed toolchain."""
        with self._condition:
            self._check()
            if self._closed_receipt is None or self._opened is None:
                raise UidVerifierUnavailable("staged toolchain still has unproven UID ownership")
            if directory != self._opened.toolchain:
                raise UidVerifierUnavailable("cleanup does not select this gate's toolchain anchor")
