# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Gate-owned native launches that cannot reinitialize an inherited Seatbelt."""

from __future__ import annotations

import base64
import contextlib
import ctypes
import hashlib
import json
import math
import os
import secrets
import selectors
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from time import monotonic
from typing import BinaryIO, Literal, cast

from .constants import PROCESS_CANCELED_EXIT_CODE, PROCESS_TIMEOUT_EXIT_CODE
from .macho_dependencies import RuntimeDependencyClosure, runtime_dependency_reads
from .pow_wow.types import CommandRunCapture
from .seatbelt_policy import PathGrant, PathScope, SeatbeltPolicy
from .uid_verifier_client import (
    PreparedLaunch,
    UidProcess,
    UidVerifierClient,
    UidVerifierUnavailable,
)

BROKER_ENV = "LOCAL_AGENT_VERIFICATION_NATIVE_BROKER"
_MAX_REQUEST_BYTES = 65536
_MAX_CONCURRENT_CHILDREN = 4
_POLL_SECONDS = 0.05
_REQUEST_TIMEOUT_SECONDS = 0.5
_SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Apple SDK sys/un.h: these native socket options are absent from Python's constants.
_SOL_LOCAL = 0
_LOCAL_PEERPID = 0x002

_PROXY = (
    "import base64,json,os,signal,socket,sys\n"
    f"configuration=json.loads(os.environ[{BROKER_ENV!r}])\n"
    """request=json.loads(open(sys.argv[1],encoding="utf-8").read())
request["nonce"]=configuration["nonce"]
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
    connection.connect(configuration["socket"])
    connection.sendall(json.dumps(request,separators=(",",":")).encode()+b"\\n")
    def terminate(*_):connection.sendall(b'{"kind":"terminate"}\\n')
    signal.signal(signal.SIGTERM,terminate)
    with connection.makefile("rb") as stream:
        for line in stream:
            message=json.loads(line)
            if message["kind"]=="exit":
                signal.signal(signal.SIGTERM,signal.SIG_DFL)
                if message["code"]<0:os.kill(os.getpid(),-message["code"])
                raise SystemExit(message["code"])
            target=sys.stdout.buffer if message["kind"]=="stdout" else sys.stderr.buffer
            target.write(base64.b64decode(message["data"],validate=True))
            target.flush()
raise SystemExit("native verification broker disconnected before a result")
"""
)


class NativeLaunchRefused(RuntimeError):
    """No subprocess was admitted for this request."""


def _peer_uid(connection: socket.socket) -> int:
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    function = library.getpeereid
    function.argtypes = (ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint))
    function.restype = ctypes.c_int
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    if function(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)):
        raise NativeLaunchRefused("kernel peer identity is unavailable")
    return uid.value


@dataclass(frozen=True)
class _LaunchRequest:
    command: tuple[str, ...]
    cwd: Path
    scratch: Path
    environment: dict[str, str]
    posture: str
    harness: str


@dataclass(frozen=True)
class _ContextRequest:
    """Read-only proof that this process is a client of an active UID-owned gate."""


def authenticated_contained_client() -> bool:
    """An environment hint cannot establish trusted-host fixture unavailability.

    The active host must prove the caller's real UID/GID over a different-UID
    kernel-authenticated connection before host-only provisioning tests may defer.
    """
    raw = os.environ.get(BROKER_ENV)
    if raw is None:
        return False
    try:
        configuration = json.loads(raw)
        if not isinstance(configuration, dict) or set(configuration) != {
            "socket",
            "proxy",
            "nonce",
        }:
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(_REQUEST_TIMEOUT_SECONDS)
            connection.connect(configuration["socket"])
            host_uid = _peer_uid(connection)
            if host_uid == os.geteuid():
                return False
            request = {"version": 1, "nonce": configuration["nonce"], "operation": "context"}
            connection.sendall(json.dumps(request).encode() + b"\n")
            response = bytearray()
            deadline = monotonic() + _REQUEST_TIMEOUT_SECONDS
            while b"\n" not in response:
                remaining = deadline - monotonic()
                if remaining <= 0 or len(response) >= 4096:
                    return False
                connection.settimeout(remaining)
                chunk = connection.recv(4096 - len(response))
                if not chunk:
                    return False
                response.extend(chunk)
        context = json.loads(response)
        return (
            isinstance(context, dict)
            and set(context) == {"kind", "caller_uid", "gate_gid", "host_uid", "source_binding"}
            and context["kind"] == "contained_client"
            and type(context["caller_uid"]) is int
            and context["caller_uid"] == os.geteuid()
            and context["gate_gid"] == os.getegid()
            and context["host_uid"] == host_uid
            and isinstance(context["source_binding"], str)
            and len(context["source_binding"]) == 64
            and all(character in "0123456789abcdef" for character in context["source_binding"])
        )
    except (OSError, ValueError, TypeError, KeyError, NativeLaunchRefused):
        return False


@dataclass
class _OwnedProcess:
    process: subprocess.Popen[bytes] | UidProcess
    policy: SeatbeltPolicy
    kind: Literal["gate", "native", "metadata"]
    parent: _OwnedProcess | None = None


def broker_command(
    command: Sequence[str],
    cwd: Path,
    scratch: Path,
    environment: Mapping[str, str],
    *,
    posture: str,
    harness: str,
) -> tuple[str, ...] | None:
    """Inside a verifier, return an authenticated proxy without applying a second sandbox."""
    raw = os.environ.get(BROKER_ENV)
    if raw is None:
        return None
    configuration = json.loads(raw)
    if set(configuration) != {"socket", "proxy", "nonce"}:
        raise ValueError("invalid native verifier broker configuration")
    request = {
        "version": 1,
        "command": list(command),
        "cwd": str(cwd.resolve()),
        "scratch": str(scratch.resolve()),
        "environment": dict(environment),
        "posture": posture,
        "harness": harness,
    }
    payload = json.dumps(request, separators=(",", ":"))
    if len(payload.encode()) + len(configuration["nonce"]) + 32 > _MAX_REQUEST_BYTES:
        raise NativeLaunchRefused("native child request exceeds the gate protocol limit")
    request_path = scratch / "native-request.json"
    request_path.write_text(payload, encoding="utf-8")
    return (sys.executable, configuration["proxy"], str(request_path))


class NativeVerificationBroker:
    """Own one strict root process and bounded, successively restricted native siblings.

    A request selects a declared child posture, never an SBPL profile or a parent
    grant. Kernel peer identity selects a still-owned process group; a bearer nonce
    alone cannot reuse another process group's authority.
    """

    def __init__(
        self,
        *,
        policy: SeatbeltPolicy,
        snapshot: Path,
        outputs: Path,
        environment: Mapping[str, str],
        deadline: float,
        authority_valid: Callable[[], bool],
        uid_owner: UidVerifierClient | None = None,
        root_prepared: PreparedLaunch | None = None,
        runtime_dependencies: tuple[RuntimeDependencyClosure, ...] = (),
    ) -> None:
        if not math.isfinite(deadline):
            raise ValueError("native verifier requires a finite deadline")
        self.snapshot = snapshot.resolve()
        self.outputs = outputs.resolve()
        self.environment = dict(environment)
        self.deadline = deadline
        self.authority_valid = authority_valid
        self.runtime_dependencies = runtime_dependencies
        for closure in runtime_dependencies:
            if any(
                not policy.allows_read(path)
                for path in (closure.executable.path, *closure.verified_reads())
            ):
                raise ValueError("runtime dependency exceeds the parent verifier read policy")
        self.authority_invalidated = False
        self.records: list[dict[str, object]] = []
        if (uid_owner is None) != (root_prepared is None):
            raise ValueError("UID ownership and its root preparation must be supplied together")
        if uid_owner is None and not policy.fixed_process_group:
            raise ValueError("local broker preparation requires the restrictive group guard")
        self.uid_owner, self._root_prepared = uid_owner, root_prepared
        self._directory = (
            tempfile.TemporaryDirectory(prefix="aiv-", dir="/private/tmp")
            if root_prepared is None
            else None
        )
        if root_prepared is not None:
            socket_root = root_prepared.scratch
        else:
            assert self._directory is not None
            socket_root = Path(self._directory.name)
        self.socket_path = socket_root / "native.sock"
        self._nonce = secrets.token_hex(32)
        self.proxy_path = self.outputs / "native-verification-proxy.py"
        self.policy = policy.with_broker(self.socket_path, self.proxy_path)
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(self.socket_path))
        self._listener.listen(_MAX_CONCURRENT_CHILDREN)
        self._listener.settimeout(_POLL_SECONDS)
        self._lock = threading.RLock()
        self._owned: dict[int, _OwnedProcess] = {}
        self._uid_owned: dict[int, _OwnedProcess] = {}
        self._output_roots = {self.outputs}
        self._workers: set[threading.Thread] = set()
        self._capacity = threading.BoundedSemaphore(_MAX_CONCURRENT_CHILDREN)
        self._closed = threading.Event()
        proxy = self.proxy_path
        proxy.write_text(_PROXY, encoding="utf-8")
        self.configuration = json.dumps(
            {"socket": str(self.socket_path), "proxy": str(proxy), "nonce": self._nonce},
            separators=(",", ":"),
        )
        self.environment[BROKER_ENV] = self.configuration
        self._server = threading.Thread(
            target=self._accept, name="native-verification-broker", daemon=True
        )
        self._server.start()

    def __enter__(self) -> NativeVerificationBroker:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _invalidated(self) -> bool:
        if self._closed.is_set() or monotonic() >= self.deadline:
            return True
        try:
            valid = self.authority_valid()
        except Exception:
            valid = False
        if not valid:
            self.authority_invalidated = True
        return not valid

    def _spawn(
        self,
        command: Sequence[str],
        cwd: Path,
        environment: Mapping[str, str],
        policy: SeatbeltPolicy,
        kind: Literal["gate", "native", "metadata"] = "native",
        parent: _OwnedProcess | None = None,
        scratch: Path | None = None,
    ) -> _OwnedProcess:
        if self._invalidated():
            raise NativeLaunchRefused("verification authority expired or was revoked")
        with self._lock:
            if parent is not None and (
                self._owned.get(parent.process.pid) is not parent
                or parent.process.poll() is not None
            ):
                raise NativeLaunchRefused("native child parent no longer owns an active process")
            if self.uid_owner is None:
                process = subprocess.Popen(
                    (_SANDBOX_EXEC, "-p", policy.render(), *command),
                    cwd=cwd,
                    env=dict(environment),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            else:
                if parent is None:
                    prepared = self._root_prepared or self.uid_owner.prepare(
                        parent=None, scratch=None
                    )
                    self._root_prepared = None
                    self._output_roots.add(prepared.scratch)
                    resources = (PathGrant(prepared.home), PathGrant(prepared.scratch))
                    policy = replace(
                        policy,
                        reads=tuple((*group, *resources) for group in policy.reads),
                        writes=tuple((*group, *resources) for group in policy.writes),
                    )
                else:
                    assert isinstance(parent.process, UidProcess)
                    scratch = scratch or parent.process.prepared.scratch
                    if not parent.policy.allows_new_subtree(scratch):
                        raise NativeLaunchRefused(
                            "child resources exceed the active parent's grants"
                        )
                    prepared = self.uid_owner.prepare(
                        parent=parent.process.prepared, scratch=scratch
                    )
                environment = {
                    **environment,
                    "HOME": str(prepared.home),
                    "TMPDIR": str(prepared.scratch),
                }
                process = self.uid_owner.launch(
                    prepared, command, cwd, environment, policy.render()
                )
            owned = _OwnedProcess(process, policy, kind, parent)
            self._owned[process.pid] = owned
            if isinstance(process, UidProcess):
                self._uid_owned[process.prepared.uid] = owned
            return owned

    def _finish(self, owned: _OwnedProcess) -> None:
        process = owned.process
        # Signal the assigned group even if its leader already closed its pipes.
        # Leader wait status alone is not a gate-wide descendant cleanup attestation.
        if isinstance(process, UidProcess):
            if process.poll() is None:
                process.send_signal(signal.SIGKILL)
            process.wait(timeout=20)
        else:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=1)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        with self._lock:
            self._owned.pop(process.pid, None)
            if isinstance(process, UidProcess):
                self._uid_owned.pop(process.prepared.uid, None)
            self.records.append(
                {
                    "pid": process.pid,
                    "kind": owned.kind,
                    "exit_code": process.returncode,
                    "policy_sha256": hashlib.sha256(owned.policy.render().encode()).hexdigest(),
                    "leader_reaped": process.returncode is not None,
                    **(
                        {"uid_cleanup": process.receipt, "launch_digest": process.identity.digest}
                        if isinstance(process, UidProcess) and process.identity is not None
                        else {}
                    ),
                }
            )

    def run(self, command: Sequence[str], cwd: Path) -> CommandRunCapture:
        owned = self._spawn(command, cwd, self.environment, self.policy, "gate")
        return self._capture(owned, command, cwd, self.deadline)

    def _capture(
        self, owned: _OwnedProcess, command: Sequence[str], cwd: Path, deadline: float
    ) -> CommandRunCapture:
        process = owned.process
        exit_code: int | None = None
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=_POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    if self._invalidated() or monotonic() >= deadline:
                        exit_code = (
                            PROCESS_CANCELED_EXIT_CODE
                            if self.authority_invalidated or self._closed.is_set()
                            else PROCESS_TIMEOUT_EXIT_CODE
                        )
                        self._signal(owned, signal.SIGKILL)
                        stdout, stderr = process.communicate(
                            timeout=20 if isinstance(process, UidProcess) else 1
                        )
                        break
            assert process.returncode is not None
            return CommandRunCapture(
                shlex.join(command),
                str(cwd),
                stdout.decode(errors="replace"),
                stderr.decode(errors="replace"),
                process.returncode if exit_code is None else exit_code,
            )
        finally:
            self._finish(owned)

    def _accept(self) -> None:
        while not self._closed.is_set():
            try:
                connection, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if not self._capacity.acquire(blocking=False):
                self._refuse(connection, "native child concurrency limit reached")
                connection.close()
                continue
            worker = threading.Thread(target=self._handle, args=(connection,), daemon=True)
            with self._lock:
                self._workers.add(worker)
            worker.start()

    @staticmethod
    def _send(connection: socket.socket, message: dict[str, object]) -> None:
        connection.sendall(json.dumps(message, separators=(",", ":")).encode() + b"\n")

    @classmethod
    def _refuse(cls, connection: socket.socket, reason: str) -> None:
        with contextlib.suppress(OSError):
            cls._send(
                connection,
                {"kind": "stderr", "data": base64.b64encode((reason + "\n").encode()).decode()},
            )
            cls._send(connection, {"kind": "exit", "code": 125})

    def _request(
        self, connection: socket.socket
    ) -> tuple[_LaunchRequest | _ContextRequest, _OwnedProcess]:
        with self._lock:
            if self.uid_owner is None:
                peer_pid = struct.unpack("i", connection.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, 4))[
                    0
                ]
                owner = self._owned.get(os.getpgid(peer_pid))
            else:
                owner = self._uid_owned.get(_peer_uid(connection))
            if owner is None or owner.process.poll() is not None:
                raise NativeLaunchRefused("request is outside a live gate-owned process identity")
            parent_policy = owner.policy
        wire = bytearray()
        request_deadline = min(self.deadline, monotonic() + _REQUEST_TIMEOUT_SECONDS)
        while b"\n" not in wire:
            remaining = request_deadline - monotonic()
            if self._invalidated() or remaining <= 0:
                raise NativeLaunchRefused("native child request exceeded its admission deadline")
            connection.settimeout(remaining)
            data = connection.recv(min(4096, _MAX_REQUEST_BYTES + 1 - len(wire)))
            if not data:
                raise NativeLaunchRefused("native child request disconnected")
            wire.extend(data)
            if len(wire) > _MAX_REQUEST_BYTES:
                raise NativeLaunchRefused("native child request is too large")
        payload = json.loads(bytes(wire))
        if (
            isinstance(payload, dict)
            and type(payload.get("version")) is int
            and payload == {"version": 1, "nonce": self._nonce, "operation": "context"}
        ):
            return _ContextRequest(), owner
        expected = {
            "version",
            "nonce",
            "command",
            "cwd",
            "scratch",
            "environment",
            "posture",
            "harness",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected
            or type(payload["version"]) is not int
            or payload["version"] != 1
        ):
            raise NativeLaunchRefused("invalid native child request fields")
        if payload["nonce"] != self._nonce:
            raise NativeLaunchRefused("native child request does not belong to this gate")
        command, environment = payload["command"], payload["environment"]
        if (
            not isinstance(command, list)
            or not 1 <= len(command) <= 256
            or not command[0]
            or any(not isinstance(part, str) or "\x00" in part for part in command)
        ):
            raise NativeLaunchRefused("native child argv is invalid")
        if (
            not isinstance(environment, dict)
            or len(environment) > 256
            or any(
                not isinstance(key, str)
                or not key
                or "=" in key
                or not isinstance(value, str)
                or "\x00" in key + value
                for key, value in environment.items()
            )
        ):
            raise NativeLaunchRefused("native child environment is invalid")
        if payload["posture"] not in {
            "read_only_inspection",
            "unattended_implementation",
            "supervised_commands",
        } or payload["harness"] not in {"codex", "claude"}:
            raise NativeLaunchRefused("native child posture or harness is invalid")
        cwd, scratch = (
            Path(payload["cwd"]).resolve(strict=True),
            Path(payload["scratch"]).resolve(strict=True),
        )
        if (
            not cwd.is_dir()
            or not scratch.is_dir()
            or not any(scratch.is_relative_to(root) for root in self._output_roots)
        ):
            raise NativeLaunchRefused("native child scratch is outside verifier outputs")
        if not (
            any(cwd.is_relative_to(root) for root in self._output_roots)
            or cwd.is_relative_to(self.snapshot)
        ):
            raise NativeLaunchRefused("native child working directory is outside the gate")
        if not parent_policy.allows_read(cwd):
            raise NativeLaunchRefused("native child working directory is outside its parent grants")
        if not parent_policy.allows_new_subtree(scratch):
            raise NativeLaunchRefused("native child scratch is outside its parent resource grants")
        executable = shutil.which(command[0], path=environment.get("PATH", ""))
        if (
            executable is None
            or not parent_policy.allows_read(Path(executable))
            or not stat.S_ISREG(Path(executable).stat().st_mode)
        ):
            raise NativeLaunchRefused("native child executable is outside its parent grants")
        return _LaunchRequest(
            tuple(command), cwd, scratch, environment, payload["posture"], payload["harness"]
        ), owner

    def _git_probe(
        self, parent: _OwnedProcess, cwd: Path, arguments: tuple[str, ...]
    ) -> subprocess.CompletedProcess[str]:
        policy = parent.policy
        executable = shutil.which("git", path=self.environment["PATH"])
        if executable is None or not policy.allows_read(Path(executable)):
            raise NativeLaunchRefused("Git metadata tool is outside the parent grants")
        command = (executable, "-C", str(cwd), *arguments)
        owned = self._spawn(command, cwd, self.environment, policy, "metadata", parent)
        capture = self._capture(owned, command, cwd, min(self.deadline, monotonic() + 2))
        if capture.exit_code in (PROCESS_TIMEOUT_EXIT_CODE, PROCESS_CANCELED_EXIT_CODE):
            raise NativeLaunchRefused("bounded Git metadata inspection did not complete")
        return subprocess.CompletedProcess(
            command, capture.exit_code, capture.stdout, capture.stderr
        )

    def _read_shebang(self, parent: _OwnedProcess, cwd: Path, path: Path) -> bytes:
        code = (
            "import base64,os,stat,sys;"
            "fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NONBLOCK|os.O_NOFOLLOW);"
            "assert stat.S_ISREG(os.fstat(fd).st_mode);"
            "print(base64.b64encode(os.read(fd,4096)).decode());os.close(fd)"
        )
        command = (
            self.environment.get("UV_PYTHON", sys.executable),
            "-I",
            "-S",
            "-c",
            code,
            str(path.resolve()),
        )
        owned = self._spawn(command, cwd, self.environment, parent.policy, "metadata", parent)
        capture = self._capture(owned, command, cwd, min(self.deadline, monotonic() + 2))
        if capture.exit_code != 0:
            raise NativeLaunchRefused("bounded executable inspection did not complete")
        return base64.b64decode(capture.stdout.strip(), validate=True).split(b"\n", 1)[0]

    def _handle(self, connection: socket.socket) -> None:
        owned: _OwnedProcess | None = None
        connection.settimeout(_REQUEST_TIMEOUT_SECONDS)
        try:
            request, owner = self._request(connection)
            connection.settimeout(_REQUEST_TIMEOUT_SECONDS)
            if isinstance(request, _ContextRequest):
                if self.uid_owner is None or not isinstance(owner.process, UidProcess):
                    raise NativeLaunchRefused("this broker has no qualified UID ownership")
                self._send(
                    connection,
                    {
                        "kind": "contained_client",
                        "caller_uid": owner.process.prepared.uid,
                        "gate_gid": owner.process.prepared.gid,
                        "host_uid": os.geteuid(),
                        "source_binding": self.uid_owner.source_binding,
                    },
                )
                return
            parent = owner.policy
            from .process_containment import (
                _CONTEXT_ENV,
                _EXACT_ENV,
                _PREFIX_ENV,
                _containment_policy,
            )
            from .spawn_authority import ReadOnlyInspection, UnattendedImplementation
            from .staffing import FrontierHarness

            posture = (
                ReadOnlyInspection()
                if request.posture == "read_only_inspection"
                else UnattendedImplementation()
            )
            if any(key.startswith(("DYLD_", "LD_", "_RLD_")) for key in request.environment):
                raise NativeLaunchRefused("native child environment contains a loader override")
            allowed_extra = {
                BROKER_ENV,
                "PYTHONPATH",
                "TMPDIR",
                "TMP",
                "TEMP",
                "UV_CACHE_DIR",
                "XDG_CACHE_HOME",
                "npm_config_cache",
                "LOCAL_AGENT_LEDGER_READER_DATABASE_URL",
                "AGENT_COORDINATION_DATABASE_URL",
                "LOCAL_AGENT_COORDINATION_DATABASE_URL",
            }
            if request.posture != "supervised_commands" and any(
                key not in _EXACT_ENV
                and key not in _CONTEXT_ENV
                and key not in allowed_extra
                and not key.startswith(_PREFIX_ENV)
                for key in request.environment
            ):
                raise NativeLaunchRefused(
                    "native child environment contains an undeclared variable"
                )
            environment = {**request.environment, BROKER_ENV: self.configuration}
            if "PYTHONPATH" in self.environment:
                environment["PYTHONPATH"] = self.environment["PYTHONPATH"]
            dependency_reads = tuple(
                PathGrant(path, PathScope.EXACT)
                for path in runtime_dependency_reads(
                    self.runtime_dependencies, request.command, environment
                )
            )
            if request.posture == "supervised_commands":
                policy = parent
            else:
                child = _containment_policy(
                    command=request.command,
                    cwd=request.cwd,
                    scratch=request.scratch,
                    posture=posture,
                    harness=FrontierHarness(request.harness),
                    environment=environment,
                    git_probe=lambda cwd, args: self._git_probe(owner, cwd, args),
                    read_shebang=lambda path: self._read_shebang(owner, request.cwd, path),
                )
                child = replace(
                    child, reads=tuple((*group, *dependency_reads) for group in child.reads)
                )
                policy = parent.intersect(child.with_broker(self.socket_path, self.proxy_path))
            owned = self._spawn(
                request.command,
                request.cwd,
                environment,
                policy,
                parent=owner,
                scratch=request.scratch,
            )
            process = owned.process
            if isinstance(process, UidProcess):
                self._stream_uid(connection, owned, owner)
                return
            with selectors.DefaultSelector() as selector:
                selector.register(connection, selectors.EVENT_READ, "connection")
                for kind, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                    assert stream is not None
                    selector.register(stream, selectors.EVENT_READ, kind)
                open_streams = 2
                while open_streams or process.poll() is None:
                    if self._invalidated() or owner.process.poll() is not None:
                        raise NativeLaunchRefused("native child authority expired or was revoked")
                    for key, _ in selector.select(_POLL_SECONDS):
                        if key.data == "connection":
                            self._control(connection, owned)
                            continue
                        chunk = os.read(cast(BinaryIO, key.fileobj).fileno(), 16384)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            open_streams -= 1
                        else:
                            self._send(
                                connection,
                                {"kind": key.data, "data": base64.b64encode(chunk).decode()},
                            )
                self._send(connection, {"kind": "exit", "code": process.returncode})
        except (OSError, ValueError, TypeError, NativeLaunchRefused, UidVerifierUnavailable) as exc:
            self._refuse(connection, f"native launch refused: {exc}")
        finally:
            if owned is not None:
                self._finish(owned)
            connection.close()
            self._capacity.release()
            with self._lock:
                self._workers.discard(threading.current_thread())

    @staticmethod
    def _signal(owned: _OwnedProcess, value: signal.Signals) -> None:
        with contextlib.suppress(ProcessLookupError):
            if isinstance(owned.process, UidProcess):
                owned.process.send_signal(value)
            else:
                os.killpg(owned.process.pid, value)

    def _control(self, connection: socket.socket, owned: _OwnedProcess) -> None:
        message = bytearray()
        deadline = min(self.deadline, monotonic() + _REQUEST_TIMEOUT_SECONDS)
        while b"\n" not in message:
            remaining = deadline - monotonic()
            if remaining <= 0 or len(message) >= 64:
                raise NativeLaunchRefused("native child control exceeded its bound")
            connection.settimeout(remaining)
            chunk = connection.recv(64 - len(message))
            if not chunk:
                raise NativeLaunchRefused("native child caller disconnected")
            message.extend(chunk)
        if json.loads(message) != {"kind": "terminate"}:
            raise NativeLaunchRefused("native child control is not a termination request")
        self._signal(owned, signal.SIGTERM)

    def _stream_uid(
        self, connection: socket.socket, owned: _OwnedProcess, owner: _OwnedProcess
    ) -> None:
        process = owned.process
        assert isinstance(process, UidProcess)
        with selectors.DefaultSelector() as selector:
            selector.register(connection, selectors.EVENT_READ)
            while True:
                for output in process.output(_POLL_SECONDS):
                    self._send(
                        connection,
                        {"kind": output.channel, "data": base64.b64encode(output.data).decode()},
                    )
                if process.poll() is not None:
                    # A terminal UID frame follows pipe EOF and atomic UID zero.
                    for output in process.output(0):
                        self._send(
                            connection,
                            {
                                "kind": output.channel,
                                "data": base64.b64encode(output.data).decode(),
                            },
                        )
                    self._send(connection, {"kind": "exit", "code": process.returncode})
                    return
                if self._invalidated() or owner.process.poll() is not None:
                    raise NativeLaunchRefused("native child authority expired or was revoked")
                if selector.select(0):
                    self._control(connection, owned)

    def close(self) -> None:
        self._closed.set()
        self._listener.close()
        self._server.join(timeout=1)
        with self._lock:
            for owned in tuple(self._owned.values()):
                self._signal(owned, signal.SIGKILL)
            workers = tuple(self._workers)
        for worker in workers:
            worker.join(timeout=20 if self.uid_owner is not None else 2)
        if any(worker.is_alive() for worker in workers):
            raise RuntimeError("native verifier child cleanup did not complete")
        if self._directory is not None:
            self._directory.cleanup()
        else:
            self.socket_path.unlink(missing_ok=True)
