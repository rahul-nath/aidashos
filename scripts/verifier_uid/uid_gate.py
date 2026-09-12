#!/usr/bin/python3 -I -S
# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Standalone privileged UID lease owner; imports only the system standard library.

Install this file root-owned and use the packet's pinned interpreter with -I -S.
The authenticated operator supplies launch data over stdin, never executable
Python or a caller-selected identity. No project binary runs before identity drop.
This preparation is not production-qualified until its native canaries pass.
"""

from __future__ import annotations

import base64
import ctypes
import errno
import fcntl
import grp
import hashlib
import json
import math
import os
import pwd
import re
import resource
import secrets
import select
import selectors
import signal
import socket
import stat
import sys
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

STATE = Path("/private/var/db/aidashos-verifier-uid")
CONFIG = STATE / "configuration.json"
MAX_MESSAGE = 262144
MAX_LAUNCHES = 9
MAX_ACTIVE_GATES = 4
MAX_LAUNCH_RECORDS = 8192
MAX_GATE_DECLARATION_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
POLL_SECONDS = 0.05
CLEANUP_SECONDS = 15.0
SIGNALER_SECONDS = 0.25
FIRST_UID = 55000
LAST_UID = 1048575
SCHEMA = "aidashos.uid-gate.v1"
SOCKET = STATE / "control.sock"
QUALIFICATION_CHECKS = frozenset(
    (
        "set-id-exec-and-spawn",
        "persona-spawn",
        "thread-credentials",
        "uid-process-limit",
        "detached-descendant-cleanup",
        "normal-uv-python-node",
        "declared-read-denials",
        "parent-disconnect",
        "helper-crash-recovery",
        "cross-uid-inherited-staging-acl",
        "relocated-installed-toolchain",
        "external-job-creation",
    )
)
TERMINATION_SIGNALS = frozenset((signal.SIGTERM, signal.SIGINT, signal.SIGHUP))
# These rules never deny session creation or posix_spawn, which normal uv/Node
# require. Set-id exec inheritance and spawn-persona behavior still require the
# versioned native qualification; syscall filtering alone is not that proof.
IDENTITY_RULES = (
    "(deny job-creation) "
    "(deny syscall-unix (syscall-number SYS_setuid SYS_seteuid SYS_setreuid "
    "SYS_setgid SYS_setegid SYS_setregid SYS_setgroups SYS_settid "
    "SYS_settid_with_pid SYS_persona))"
)
POLICY_SYMBOLS = frozenset(
    (
        "version",
        "allow",
        "deny",
        "default",
        "file-read-data",
        "file-read*",
        "file-read-metadata",
        "file-write*",
        "file-ioctl",
        "network*",
        "network-outbound",
        "network-inbound",
        "network-bind",
        "pseudo-tty",
        "literal",
        "subpath",
        "regex",
        "require-all",
        "require-any",
        "require-not",
        "remote",
        "local",
        "ip",
        "tcp",
        "unix-socket",
        "path-literal",
        "path-subpath",
        "process-exec",
        "process-fork",
        "signal",
        "target",
        "same-sandbox",
        "mach-lookup",
        "global-name",
    )
)


def protected_profile(profile):
    """Accept the closed host-renderer vocabulary, then add the fixed guard.

    Token-level validation distinguishes quoted paths from policy symbols. It
    rejects imports, executable Scheme and sandbox-removal modifiers before the
    profile reaches sandbox_init. The guard bytes participate in the digest.
    """
    decoder, offset, depth = json.JSONDecoder(), 0, 0
    while offset < len(profile):
        character = profile[offset]
        if character.isspace():
            offset += 1
        elif character == "(":
            depth += 1
            if depth > 32:
                raise Refused("Seatbelt policy nesting exceeds the closed renderer")
            offset += 1
        elif character == ")":
            depth -= 1
            if depth < 0:
                raise Refused("unbalanced Seatbelt policy")
            offset += 1
        elif character == '"' or profile[offset : offset + 2] == '#"':
            if character == "#":
                offset += 1
            _, consumed = decoder.raw_decode(profile[offset:])
            offset += consumed
        else:
            match = re.match(r"[^\s()\"]+", profile[offset:])
            if match is None or (match[0] not in POLICY_SYMBOLS and match[0] != "1"):
                raise Refused("Seatbelt policy contains an undeclared symbol or modifier")
            offset += len(match[0])
    if depth:
        raise Refused("unbalanced Seatbelt policy")
    return profile + " " + IDENTITY_RULES


class Refused(RuntimeError):
    """No authority is granted for an invalid or unavailable launch."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise Refused("integer outside the declared contract")
    return value


def text(value, empty=False):
    if not isinstance(value, str) or (not empty and not value) or "\0" in value:
        raise Refused("expected nonempty NUL-free text")
    return value


def exact_keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise Refused("message fields differ from the declared contract")


@dataclass(frozen=True)
class GateSpec:
    source_binding: str
    duration: float
    process_limit: int

    @classmethod
    def parse(cls, raw):
        exact_keys(raw, ("kind", "schema", "source_binding", "duration", "process_limit"))
        if raw["kind"] != "open" or raw["schema"] != SCHEMA:
            raise Refused("unsupported gate protocol")
        source = text(raw["source_binding"])
        if len(source) != 64 or any(c not in "0123456789abcdef" for c in source):
            raise Refused("source binding must be a SHA-256 digest")
        duration = raw["duration"]
        if (
            type(duration) not in (int, float)
            or not math.isfinite(duration)
            or not 0 < duration <= 3600
        ):
            raise Refused("gate duration must preserve the registered upper bound")
        return cls(source, float(duration), integer(raw["process_limit"], 8, 1024))


@dataclass(frozen=True)
class LaunchSpec:
    argv: tuple
    cwd: str
    environment: tuple
    profile: str
    source_binding: str

    @classmethod
    def parse(cls, raw, gate):
        exact_keys(raw, ("kind", "argv", "cwd", "environment", "profile", "source_binding"))
        if raw["kind"] != "launch" or raw["source_binding"] != gate.source_binding:
            raise Refused("launch does not bind the admitted gate source")
        argv = raw["argv"]
        if not isinstance(argv, list) or not 1 <= len(argv) <= 256:
            raise Refused("invalid immutable argv")
        argv = (text(argv[0]), *(text(arg, empty=True) for arg in argv[1:]))
        cwd = text(raw["cwd"])
        if not cwd.startswith("/") or not argv[0].startswith("/"):
            raise Refused("cwd and executable must be absolute")
        environment = raw["environment"]
        if not isinstance(environment, dict) or len(environment) > 256:
            raise Refused("invalid launch environment")
        pairs = tuple(
            sorted((text(key), text(value, empty=True)) for key, value in environment.items())
        )
        if any("=" in key or key.startswith("SUDO_") for key, _ in pairs):
            raise Refused("privileged startup environment cannot travel to the gate")
        profile = text(raw["profile"])
        if len(profile.encode()) > MAX_MESSAGE // 2:
            raise Refused("Seatbelt profile exceeds protocol bound")
        return cls(argv, cwd, pairs, protected_profile(profile), gate.source_binding)

    def payload(self):
        return {
            "argv": self.argv,
            "cwd": self.cwd,
            "environment": self.environment,
            "profile": self.profile,
            "source_binding": self.source_binding,
        }


class DarwinMembership:
    """Zero is accepted only from a complete, successful in-kernel UID scan.

    proc_listpids maps errors to zero, leaving errno. A non-null one-pid buffer
    distinguishes emptiness from its NULL-buffer global-size query. Both real and
    effective UID scans must be empty; an error never becomes cleanup evidence.
    """

    def __init__(self):
        library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self._list = library.proc_listpids
        self._list.argtypes = (ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int)
        self._list.restype = ctypes.c_int

    def empty(self, uid):
        integer(uid, FIRST_UID, LAST_UID)
        for query in (4, 5):  # SDK: PROC_UID_ONLY, PROC_RUID_ONLY.
            pid = ctypes.c_int()
            ctypes.set_errno(0)
            size = self._list(query, uid, ctypes.byref(pid), ctypes.sizeof(pid))
            error = ctypes.get_errno()
            if error or size < 0 or size not in (0, ctypes.sizeof(pid)):
                raise Refused("kernel UID membership query is unavailable")
            if size:
                return False
        return True


class DarwinACL:
    """Apply inherited group/operator access only to a newly created pinned FD."""

    def __init__(self):
        self.library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        pointer = ctypes.c_void_p
        signatures = {
            "acl_init": ([ctypes.c_int], pointer),
            "acl_create_entry": ([ctypes.POINTER(pointer), ctypes.POINTER(pointer)], ctypes.c_int),
            "acl_set_tag_type": ([pointer, ctypes.c_int], ctypes.c_int),
            "acl_set_qualifier": ([pointer, pointer], ctypes.c_int),
            "acl_set_permset_mask_np": ([pointer, ctypes.c_uint64], ctypes.c_int),
            "acl_get_flagset_np": ([pointer, ctypes.POINTER(pointer)], ctypes.c_int),
            "acl_add_flag_np": ([pointer, ctypes.c_int], ctypes.c_int),
            "acl_set_fd_np": ([ctypes.c_int, pointer, ctypes.c_int], ctypes.c_int),
            "acl_free": ([pointer], ctypes.c_int),
            "mbr_gid_to_uuid": ([ctypes.c_uint, pointer], ctypes.c_int),
            "mbr_uid_to_uuid": ([ctypes.c_uint, pointer], ctypes.c_int),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.library, name)
            function.argtypes, function.restype = arguments, result

    def initialize(self, fd, gid, operator):
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise Refused("staging ACL target is not a newly owned directory")
        library = self.library
        acl = ctypes.c_void_p(library.acl_init(2))
        if not acl:
            raise Refused("native ACL allocation failed")

        def checked(result):
            if result != 0:
                raise Refused("native inherited staging ACL could not be installed")

        try:
            for resolver, identity in (
                (library.mbr_gid_to_uuid, gid),
                (library.mbr_uid_to_uuid, operator),
            ):
                uuid = (ctypes.c_ubyte * 16)()
                entry, flags = ctypes.c_void_p(), ctypes.c_void_p()
                checked(resolver(identity, uuid))
                checked(library.acl_create_entry(ctypes.byref(acl), ctypes.byref(entry)))
                checked(library.acl_set_tag_type(entry, 1))  # ACL_EXTENDED_ALLOW.
                checked(library.acl_set_qualifier(entry, uuid))
                # SDK permissions 1..11 cover data/metadata, not ACL/owner changes.
                checked(library.acl_set_permset_mask_np(entry, sum(1 << n for n in range(1, 12))))
                checked(library.acl_get_flagset_np(entry, ctypes.byref(flags)))
                checked(library.acl_add_flag_np(flags, 1 << 5))  # File inheritance.
                checked(library.acl_add_flag_np(flags, 1 << 6))  # Directory inheritance.
            checked(library.acl_set_fd_np(fd, acl, 0x100))  # ACL_TYPE_EXTENDED.
        finally:
            library.acl_free(acl)


def new_staging_directory(parent, name, uid, gid, operator, acl, parent_fd=None):
    """The sole privileged staging mutation accepts a module-owned fresh name."""
    if not re.fullmatch(r"[a-z0-9-]+", name):
        raise Refused("staging name is not an internal identifier")
    path = parent / name
    os.mkdir(path if parent_fd is None else name, mode=0o700, dir_fd=parent_fd)
    fd = os.open(
        path if parent_fd is None else name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=parent_fd,
    )
    try:
        acl.initialize(fd, gid, operator)
        os.fchown(fd, uid, gid)
    finally:
        os.close(fd)
    return path


def anchored_directory(anchor, relative, allowed_owners):
    """Pin each existing directory without following aliases or changing it."""
    if not isinstance(relative, str) or relative.startswith("/"):
        raise Refused("scratch must be relative to the immutable staging anchor")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise Refused("scratch contains an alias or parent traversal")
    fd = os.open(anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = following
            if os.fstat(fd).st_uid not in allowed_owners:
                raise Refused("scratch directory belongs to a foreign principal")
        return fd
    except BaseException:
        os.close(fd)
        raise


def root_owned(path):
    """Reject symlinks and every writable ancestor of privileged state/code."""
    for part in (path, *path.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise Refused("privileged path is not exclusively root-owned")


def persist(path, payload):
    temporary = path.with_name(path.name + ".new-" + secrets.token_hex(12))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(payload))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def reserve_uid(state):
    """Advance a protected cursor before use; crashes may waste IDs, never reuse them."""
    fd = os.open(state / "allocation.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        cursor = state / "allocation.json"
        if cursor.exists():
            with cursor.open("rb") as stream:
                raw = json.load(stream)
            exact_keys(raw, ("next_uid",))
            uid = integer(raw["next_uid"], FIRST_UID, LAST_UID + 1)
        else:
            uid = FIRST_UID
        while uid <= LAST_UID:
            directory = state / ("uid-" + str(uid))
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                uid += 1
                continue
            persist(cursor, {"next_uid": uid + 1})
            return uid, directory
        raise Refused("no unused exclusive verifier UID remains")
    finally:
        os.close(fd)


class Lease:
    """A UID is never reused, including after a process or helper crash."""

    def __init__(self, directory, uid, manifest):
        self.directory, self.uid = directory, uid
        self.manifest = manifest
        self.path = directory / "lease.json"
        self.closed = False
        self.lock_descriptor = os.open(
            directory / "owner.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(self.lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock_descriptor)
            raise Refused("the gate still has a live owner or an in-flight root launcher") from None

    @classmethod
    def allocate(cls, state, membership, gate, gate_uid=None):
        while True:
            uid, directory = reserve_uid(state)
            lease = cls(
                directory,
                uid,
                {
                    "schema": SCHEMA,
                    "uid": uid,
                    "nonce": secrets.token_hex(32),
                    "source_binding": gate.source_binding,
                    "status": "quarantined",
                    "launches": [],
                    "aggregate_memory": "unqualified",
                    "kind": "gate_reservation" if gate_uid is None else "launch_reservation",
                    "gate_uid": gate_uid,
                },
            )
            lease.write()
            try:
                pwd.getpwuid(uid)
            except KeyError:
                pass
            else:
                os.close(lease.lock_descriptor)
                continue
            if membership.empty(uid):
                return lease
            os.close(lease.lock_descriptor)

    def write(self):
        persist(self.path, self.manifest)

    def prepare_work(self, parent, gid, operator, acl, parent_fd):
        name = "l" + str(self.uid)
        directory = parent / name
        os.mkdir(name, mode=0o755, dir_fd=parent_fd)
        directory_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            for child in ("home", "scratch"):
                path = new_staging_directory(
                    directory, child, self.uid, gid, operator, acl, directory_fd
                )
                self.manifest[child] = str(path)
        finally:
            os.close(directory_fd)
        self.manifest["gid"] = gid
        self.write()

    def validate_environment(self, spec):
        environment = dict(spec.environment)
        if (
            environment.get("HOME") != self.manifest["home"]
            or environment.get("TMPDIR") != self.manifest["scratch"]
        ):
            raise Refused("launch must use the gate-owned home and temporary directory")

    def record_launch(self, spec):
        if self.closed:
            raise Refused("gate launch authority is closed")
        if self.manifest["launches"]:
            raise Refused("a prepared launch UID admits exactly one command tree")
        launch_digest = digest(spec.payload())
        record = {"digest": launch_digest, "handle": self.manifest["nonce"], "spec": spec.payload()}
        if len(self.manifest["launches"]) >= MAX_LAUNCH_RECORDS:
            raise Refused("gate launch record capacity exhausted")
        if len(canonical(self.manifest)) + len(canonical(record)) > MAX_MANIFEST_BYTES:
            raise Refused("gate manifest capacity exhausted")
        self.manifest["launches"].append(record)
        self.write()
        return launch_digest

    def close(self):
        self.closed = True

    def receipt(self, membership):
        if not self.closed or not membership.empty(self.uid):
            raise Refused("cleanup lacks closed authority and zero UID members")
        self.manifest["status"] = "cleaned"
        self.write()
        return {
            "kind": "cleaned",
            "uid": self.uid,
            "source_binding": self.manifest["source_binding"],
            "lease_digest": digest(self.manifest),
            "aggregate_memory": "unqualified",
        }


def require_exclusive_kernel_group(gid):
    """Darwin credentials include the effective GID as the first kernel group.

    Python's macOS getgroups can query directory-service membership instead.
    The plain libc symbol and one-entry buffer prove the complete kernel vector:
    extra groups cause EINVAL rather than being silently truncated.
    """
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    query = library.getgroups
    query.argtypes = (ctypes.c_int, ctypes.POINTER(ctypes.c_uint32))
    query.restype = ctypes.c_int
    group = ctypes.c_uint32()
    count = query(1, ctypes.byref(group))
    if count < 0:
        raise OSError(ctypes.get_errno(), "exclusive kernel group query failed")
    if count != 1 or group.value != gid:
        raise Refused("kernel credential is not exactly the assigned effective group")


def drop_identity(uid, process_limit, gid=None):
    integer(uid, FIRST_UID, LAST_UID)
    gid = uid if gid is None else integer(gid, FIRST_UID, LAST_UID)
    # The serialized same-UID cleanup signaler needs one slot of the total cap.
    workload_limit = process_limit - 1
    resource.setrlimit(resource.RLIMIT_NPROC, (workload_limit, workload_limit))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    if (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) != (
        uid,
        uid,
        gid,
        gid,
    ):
        raise Refused("identity drop did not establish the exclusive principal")
    require_exclusive_kernel_group(gid)
    # A successful regain would be a fatal contract violation, before any command.
    for regain in (lambda: os.seteuid(0), lambda: os.setuid(0)):
        try:
            regain()
        except PermissionError:
            pass
        else:
            raise Refused("saved privileged identity survived the drop")


def apply_sandbox(profile):
    library = ctypes.CDLL("/usr/lib/libsandbox.dylib", use_errno=True)
    initialize = library.sandbox_init
    initialize.argtypes = (ctypes.c_char_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_char_p))
    initialize.restype = ctypes.c_int
    error = ctypes.c_char_p()
    if initialize(profile.encode(), 0, ctypes.byref(error)) != 0:
        detail = (error.value or b"no compiler diagnostic")[:1024].decode(errors="replace")
        raise Refused("declared Seatbelt profile could not be applied: " + detail)


def close_descriptors(first):
    """Enumerate the actual single-threaded child table, not its possibly lowered limit."""
    for name in os.listdir("/dev/fd"):
        descriptor = int(name)
        if descriptor >= first:
            try:
                os.close(descriptor)
            except OSError as failure:
                # listdir's temporary descriptor can appear in its own snapshot.
                if failure.errno != errno.EBADF:
                    raise


def fork_launch(spec, uid, process_limit, children, identifier, gid=None, anchor=None):
    """The root branch reads no caller files and executes no caller programs."""
    out_read, out_write = os.pipe()
    err_read, err_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        phase = "signal_setup"
        try:
            for number in TERMINATION_SIGNALS:
                signal.signal(number, signal.SIG_DFL)
            signal.pthread_sigmask(signal.SIG_UNBLOCK, TERMINATION_SIGNALS)
            phase = "stdio"
            os.dup2(out_write, 1)
            os.dup2(err_write, 2)
            null = os.open("/dev/null", os.O_RDONLY)
            os.dup2(null, 0)
            phase = "session"
            os.setsid()
            phase = "identity"
            drop_identity(uid, process_limit, gid)
            # Keep the inherited lease lock through the credential transition.
            # If the owner dies, recovery cannot observe zero before an already
            # forked root child either drops identity or exits without launching.
            phase = "descriptor_cleanup"
            close_descriptors(3)
            phase = "sandbox"
            apply_sandbox(spec.profile)
            phase = "cwd_validation"
            if (
                anchor is not None
                and os.path.commonpath((os.path.realpath(spec.cwd), anchor)) != anchor
            ):
                raise Refused("contained cwd resolves outside the immutable staging anchor")
            phase = "environment"
            os.environ.clear()
            os.environ.update(dict(spec.environment))
            phase = "chdir"
            os.chdir(spec.cwd)
            phase = "exec"
            os.execve(spec.argv[0], spec.argv, dict(spec.environment))
        except BaseException as failure:
            try:
                # Never serialize argv, environment, or traceback locals.
                report: dict[str, object] = {"phase": phase, "error_type": type(failure).__name__}
                if isinstance(failure, OSError):
                    report["errno"] = failure.errno
                if isinstance(failure, Refused):
                    report["reason"] = str(failure)[:1024]
                os.write(2, b"verifier launch failed: " + canonical(report) + b"\n")
            except BaseException:
                pass
        finally:
            os._exit(125)
    children[pid] = identifier
    os.close(out_write)
    os.close(err_write)
    return pid, out_read, err_read


def sweep_uid(uid, number=signal.SIGKILL):
    """Use a dropped child's kernel credential check, never root kill(-1)/PID races."""
    if number not in (signal.SIGKILL, signal.SIGTERM):
        raise Refused("unsupported lifecycle signal")
    pid = os.fork()
    if pid == 0:
        try:
            for handled in TERMINATION_SIGNALS:
                signal.signal(handled, signal.SIG_DFL)
            signal.pthread_sigmask(signal.SIG_UNBLOCK, TERMINATION_SIGNALS)
            drop_identity(uid, 1024)
            close_descriptors(0)
            os.kill(-1, number)
        except ProcessLookupError:
            pass
        except BaseException:
            os._exit(125)
        os._exit(0)
    deadline = time.monotonic() + SIGNALER_SECONDS
    status = None
    while time.monotonic() < deadline:
        waited, result = os.waitpid(pid, os.WNOHANG)
        if waited:
            status = result
            break
        time.sleep(0.005)
    if status is None:
        # The direct child has not been reaped, so its PID cannot be recycled.
        # An adversarial UID peer can stop the dropped signaler; root kills only
        # this still-owned helper child, never an enumerated arbitrary PID.
        os.kill(pid, signal.SIGKILL)
        deadline = time.monotonic() + SIGNALER_SECONDS
        while time.monotonic() < deadline:
            waited, result = os.waitpid(pid, os.WNOHANG)
            if waited:
                status = result
                break
            time.sleep(0.005)
    if status is None:
        raise Refused("cleanup signaler did not terminate within its bounded lifetime")
    if not (os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0) and not os.WIFSIGNALED(status):
        raise Refused("UID cleanup signaler failed")


def drain(lease, membership, children: set[int]):
    lease.close()
    deadline = time.monotonic() + CLEANUP_SECONDS
    while time.monotonic() < deadline:
        for pid in tuple(children):
            # These are unreaped direct children, so their PIDs cannot recycle.
            # Stop even a delayed root launcher before it can acquire this UID.
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            waited, _ = os.waitpid(pid, os.WNOHANG)
            if waited:
                children.remove(pid)
        empty = membership.empty(lease.uid)
        if not children and empty:
            return lease.receipt(membership)
        if not empty:
            sweep_uid(lease.uid)
        time.sleep(POLL_SECONDS)
    raise Refused("UID cleanup timed out; its durable lease remains quarantined")


def emit(message):
    # A stalled host must not suspend cancellation/deadline ownership indefinitely.
    remaining = canonical(message) + b"\n"
    deadline = time.monotonic() + POLL_SECONDS
    while remaining:
        if time.monotonic() >= deadline:
            raise Refused("host output deadline expired")
        if not select.select([], [1], [], max(0, deadline - time.monotonic()))[1]:
            raise Refused("host stopped consuming bounded control output")
        try:
            sent = os.write(1, remaining)
        except BlockingIOError:
            continue
        if sent <= 0:
            raise Refused("host output disconnected")
        remaining = remaining[sent:]


@contextmanager
def admission(lease, deadline):
    """Closing authority and the final fork cannot interleave in Python handlers."""
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, TERMINATION_SIGNALS)
    try:
        if (
            lease.closed
            or time.monotonic() >= deadline
            or signal.sigpending() & TERMINATION_SIGNALS
        ):
            raise Refused("gate launch authority expired or was canceled")
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


@dataclass(frozen=True)
class Prepared:
    lease: Lease


@dataclass(frozen=True)
class Running:
    prepared: Prepared
    pid: int


@dataclass(frozen=True)
class Exited:
    prepared: Prepared
    code: int
    receipt: dict


@dataclass(frozen=True)
class Completed:
    receipt: dict


def prepare_gate(state, membership, gate, operator, acl):
    while True:
        owner = Lease.allocate(state, membership, gate)
        try:
            grp.getgrgid(owner.uid)
        except KeyError:
            break
        os.close(owner.lock_descriptor)
    owner.manifest.update({"kind": "gate", "child_uids": []})
    owner.write()
    # Keep the root broker's Unix socket below macOS's 104-byte sockaddr_un path.
    anchor = state / "work" / ("g" + str(owner.uid))
    anchor.mkdir(mode=0o755)
    for name in ("source", "toolchain"):
        new_staging_directory(anchor, name, operator, owner.uid, operator, acl)
    owner.manifest["staging"] = str(anchor)
    owner.write()
    return owner, anchor


def serve(state, membership, gate, operator):
    # Fresh traversal anchors must not inherit an operator's restrictive umask.
    # Writable data directories still use mode0700 plus the scoped inherited ACL.
    os.umask(0o022)
    acl = DarwinACL()
    owner, shared = prepare_gate(state, membership, gate, operator, acl)
    deadline = time.monotonic() + gate.duration
    owned, children, streams = {}, {}, {}
    selector = selectors.DefaultSelector()
    selector.register(0, selectors.EVENT_READ, "control")
    pending, disconnected, declared_bytes = b"", False, 0
    for number in TERMINATION_SIGNALS:
        signal.signal(number, lambda *_: owner.close())

    def retire(handle, code):
        current = owned[handle]
        prepared = current.prepared if isinstance(current, Running) else current
        if not isinstance(prepared, Prepared):
            return
        prepared.lease.close()
        for child_handle, child in tuple(owned.items()):
            child_prepared = child.prepared if isinstance(child, Running) else child
            if (
                isinstance(child_prepared, Prepared)
                and child_prepared.lease.manifest.get("parent_handle") == handle
            ):
                retire(child_handle, 125 if isinstance(child, Prepared) else -signal.SIGKILL)
        pids = {pid for pid, item in children.items() if item == handle}
        receipt = drain(prepared.lease, membership, pids)
        os.close(prepared.lease.lock_descriptor)
        for pid in tuple(children):
            if children[pid] == handle:
                del children[pid]
        owned[handle] = Exited(prepared, code, receipt)

    def require_running_parent(handle):
        current = owned.get(handle)
        if not isinstance(current, Running):
            raise Refused("parent launch no longer owns active authority")
        waited, status = os.waitpid(current.pid, os.WNOHANG)
        if waited:
            del children[current.pid]
            retire(handle, os.waitstatus_to_exitcode(status))
            raise Refused("parent launch exited before dependent admission")

    def command(raw):
        nonlocal declared_bytes
        if raw == {"kind": "close"}:
            owner.close()
            return
        if owner.closed or time.monotonic() >= deadline:
            raise Refused("gate admission is closed")
        if isinstance(raw, dict) and raw.get("kind") == "prepare":
            exact_keys(raw, ("kind", "parent_handle", "scratch"))
            if len(owned) >= MAX_LAUNCH_RECORDS:
                raise Refused("gate launch capacity exhausted")
            if sum(not isinstance(item, Completed) for item in owned.values()) >= MAX_LAUNCHES:
                raise Refused("concurrent launch capacity exhausted")
            if raw["parent_handle"] is None:
                if (
                    any(not isinstance(item, Completed) for item in owned.values())
                    or raw["scratch"] is not None
                ):
                    raise Refused("a root launch must follow completion of previous gate work")
                parent, parent_fd = (
                    shared,
                    os.open(shared, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW),
                )
            else:
                require_running_parent(raw["parent_handle"])
                allowed_owners = {0, operator, *owner.manifest["child_uids"]}
                parent_fd = anchored_directory(shared, raw["scratch"], allowed_owners)
                parent = shared / raw["scratch"]
            lease = Lease.allocate(state, membership, gate, owner.uid)
            handle = lease.manifest["nonce"]
            owned[handle] = Prepared(lease)
            # Bind recovery before a child has any authority to execute.
            owner.manifest["child_uids"].append(lease.uid)
            owner.write()
            lease.manifest.update(
                {"kind": "launch", "gate_uid": owner.uid, "parent_handle": raw["parent_handle"]}
            )
            try:
                lease.prepare_work(parent, owner.uid, operator, acl, parent_fd)
            finally:
                os.close(parent_fd)
            emit(
                {
                    "kind": "prepared",
                    "handle": handle,
                    "uid": lease.uid,
                    "gid": owner.uid,
                    "home": lease.manifest["home"],
                    "scratch": lease.manifest["scratch"],
                }
            )
            return
        if not isinstance(raw, dict) or raw.get("handle") not in owned:
            raise Refused("message does not select an owned launch occurrence")
        handle = raw["handle"]
        current = owned[handle]
        if raw.get("kind") in ("terminate", "cancel"):
            exact_keys(raw, ("kind", "handle"))
            if isinstance(current, Running) and raw["kind"] == "terminate":
                sweep_uid(current.prepared.lease.uid, signal.SIGTERM)
            elif isinstance(current, (Prepared, Running)):
                retire(handle, 125 if isinstance(current, Prepared) else -signal.SIGKILL)
            return
        if not isinstance(current, Prepared):
            raise Refused("launch occurrence cannot be started twice")
        request = dict(raw)
        del request["handle"]
        spec = LaunchSpec.parse(request, gate)
        requested_payload = spec.payload()
        requested_payload["profile"] = request["profile"]
        requested_digest = digest(requested_payload)
        current.lease.validate_environment(spec)
        # The host must stage source and relocated installed toolchains inside
        # the declared anchor. Root never resolves or walks the caller's cwd.
        if os.path.normpath(spec.cwd) != spec.cwd or os.path.commonpath(
            (spec.cwd, str(shared))
        ) != str(shared):
            raise Refused("launch cwd is outside the helper-created staging anchor")
        declared_bytes += len(canonical(spec.payload()))
        if declared_bytes > MAX_GATE_DECLARATION_BYTES:
            raise Refused("gate declaration byte capacity exhausted")
        spec_digest = current.lease.record_launch(spec)
        with admission(owner, deadline), admission(current.lease, deadline):
            parent_handle = current.lease.manifest["parent_handle"]
            if parent_handle is not None:
                require_running_parent(parent_handle)
            pid, stdout, stderr = fork_launch(
                spec,
                current.lease.uid,
                gate.process_limit,
                children,
                handle,
                owner.uid,
                str(shared),
            )
        owned[handle] = Running(current, pid)
        for fd, channel in ((stdout, "stdout"), (stderr, "stderr")):
            streams[fd] = (handle, channel)
            selector.register(fd, selectors.EVENT_READ, "output")
        emit(
            {
                "kind": "launched",
                "handle": handle,
                "uid": current.lease.uid,
                "pid": pid,
                "pgid": pid,
                "digest": spec_digest,
                "requested_digest": requested_digest,
            }
        )

    try:
        emit(
            {
                "kind": "opened",
                "gate_handle": owner.manifest["nonce"],
                "gid": owner.uid,
                "staging": str(shared),
                "source": str(shared / "source"),
                "toolchain": str(shared / "toolchain"),
            }
        )
        while not owner.closed and time.monotonic() < deadline:
            for key, _ in selector.select(min(POLL_SECONDS, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fd, 4096)
                if key.data == "control":
                    if not chunk:
                        disconnected = True
                        owner.close()
                        break
                    pending += chunk
                    if len(pending) > MAX_MESSAGE:
                        raise Refused("control message exceeds bounded protocol")
                    while b"\n" in pending and not owner.closed:
                        line, pending = pending.split(b"\n", 1)
                        command(json.loads(line))
                elif chunk:
                    handle, channel = streams[key.fd]
                    emit(
                        {
                            "kind": channel,
                            "handle": handle,
                            "data": base64.b64encode(chunk).decode(),
                        }
                    )
                else:
                    selector.unregister(key.fd)
                    os.close(key.fd)
                    del streams[key.fd]
            for pid in tuple(children):
                if pid not in children:
                    continue
                waited, status = os.waitpid(pid, os.WNOHANG)
                if waited:
                    handle = children.pop(pid)
                    retire(handle, os.waitstatus_to_exitcode(status))
            for handle, current in tuple(owned.items()):
                if isinstance(current, Exited) and not any(
                    item == handle for item, _ in streams.values()
                ):
                    emit(
                        {
                            "kind": "exit",
                            "handle": handle,
                            "code": current.code,
                            "receipt": current.receipt,
                        }
                    )
                    owned[handle] = Completed(current.receipt)
    finally:
        owner.close()
        selector.close()
        for fd in streams:
            os.close(fd)
        failures = []
        for handle, current in tuple(owned.items()):
            if isinstance(current, (Prepared, Running)):
                try:
                    retire(handle, 125 if isinstance(current, Prepared) else -signal.SIGKILL)
                except (Refused, OSError) as failure:
                    failures.append(str(failure))
        if failures:
            raise Refused("one or more launch UIDs remain quarantined: " + "; ".join(failures))
        receipts = [
            item.receipt for item in owned.values() if isinstance(item, (Exited, Completed))
        ]
        receipt = owner.receipt(membership)
        if not disconnected:
            for handle, current in owned.items():
                if isinstance(current, Exited):
                    emit(
                        {
                            "kind": "canceled",
                            "handle": handle,
                            "code": current.code,
                            "receipt": current.receipt,
                            "reason": "gate_closed",
                        }
                    )
            emit({"kind": "gate_closed", "receipt": receipt, "launch_receipts": receipts})


def installed_operator_configuration():
    """Authenticate fixed installed ownership without granting workload launch."""
    if os.geteuid() != 0 or sys.platform != "darwin":
        raise Refused("the installed helper requires authenticated macOS root launch")
    if not sys.flags.isolated or not sys.flags.no_site:
        raise Refused("the system interpreter must run in isolated no-site mode")
    root_owned(Path(__file__).absolute())
    root_owned(Path(sys.executable).resolve())
    root_owned(Path(sys.prefix).resolve())
    root_owned(CONFIG)
    with CONFIG.open("rb") as stream:
        configuration = json.load(stream)
    exact_keys(configuration, ("operator_uid", "qualification"))
    integer(configuration["operator_uid"], 1, FIRST_UID - 1)
    return configuration


def installed_configuration():
    configuration = installed_operator_configuration()
    # Installation is preparation. Only the reviewed native qualification may
    # activate the versioned credential/process/compatibility contract.
    qualification = configuration["qualification"]
    if not isinstance(qualification, dict):
        raise Refused("native credential and toolchain qualification has not passed")
    exact_keys(qualification, ("helper_sha256", "kernel_release", "checks"))
    if (
        qualification["helper_sha256"] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        or qualification["kernel_release"] != os.uname().release
        or not isinstance(qualification["checks"], list)
        or set(qualification["checks"]) != QUALIFICATION_CHECKS
    ):
        raise Refused("native qualification does not bind this helper, kernel and complete checks")
    return configuration


def read_declaration():
    # Do not mix buffered readline with os.read: it can hide prefetched commands.
    line = bytearray()
    deadline = time.monotonic() + 5
    while len(line) <= MAX_MESSAGE:
        if time.monotonic() >= deadline:
            raise Refused("gate admission timed out")
        if not select.select([0], [], [], max(0, deadline - time.monotonic()))[0]:
            raise Refused("gate admission timed out")
        try:
            part = os.read(0, 1)
        except BlockingIOError:
            continue
        if not part:
            raise Refused("missing bounded gate declaration")
        if part == b"\n":
            return json.loads(line)
        line.extend(part)
    raise Refused("gate declaration exceeds the protocol bound")


def read_lease(state, uid):
    uid = integer(uid, FIRST_UID, LAST_UID)
    directory = state / ("uid-" + str(uid))
    root_owned(directory)
    # Acquire ownership before reading. The previous owner may append a launch
    # immediately before exiting; a pre-lock snapshot could omit that identity.
    lease = Lease(directory, uid, {})
    try:
        root_owned(lease.path)
        with lease.path.open("rb") as stream:
            manifest = json.load(stream)
        if manifest.get("uid") != uid or manifest.get("schema") != SCHEMA:
            raise Refused("recovery identity differs from durable lease")
        try:
            pwd.getpwuid(uid)
        except KeyError:
            pass
        else:
            raise Refused(
                "reserved UID has been reassigned to an account; recovery authority is invalid"
            )
        lease.manifest = manifest
        return lease
    except BaseException:
        os.close(lease.lock_descriptor)
        raise


def recover_owned(state, membership, uid):
    lease = read_lease(state, uid)
    try:
        kind = lease.manifest.get("kind")
        if kind == "gate_reservation":
            return recover_reservation(lease, membership)
        if kind in ("launch", "launch_reservation"):
            parent = read_lease(state, lease.manifest.get("gate_uid"))
            try:
                if parent.manifest.get("kind") != "gate" or (
                    kind == "launch" and uid not in parent.manifest.get("child_uids", [])
                ):
                    raise Refused("launch lease is not bound to its gate")
                if parent.manifest.get("source_binding") != lease.manifest.get("source_binding"):
                    raise Refused("launch source binding differs from its gate")
            finally:
                os.close(parent.lock_descriptor)
            return (
                recover_reservation(lease, membership)
                if kind == "launch_reservation"
                else drain(lease, membership, set())
            )
        if kind != "gate":
            raise Refused("unknown lease kind cannot supply cleanup evidence")
        children = lease.manifest.get("child_uids")
        if (
            not isinstance(children, list)
            or len(set(children)) != len(children)
            or len(children) > MAX_LAUNCH_RECORDS
        ):
            raise Refused("gate child identity set is not valid")
        receipts, failures = [], []
        for child_uid in children:
            child = None
            try:
                child = read_lease(state, child_uid)
                if (
                    child.manifest.get("kind") not in ("launch", "launch_reservation")
                    or child.manifest.get("gate_uid") != lease.uid
                    or child.manifest.get("source_binding") != lease.manifest.get("source_binding")
                ):
                    raise Refused("linked launch does not belong to this gate and source")
                receipts.append(
                    recover_reservation(child, membership)
                    if child.manifest["kind"] == "launch_reservation"
                    else drain(child, membership, set())
                )
            except (Refused, OSError, ValueError) as failure:
                failures.append(str(failure))
            finally:
                if child is not None:
                    os.close(child.lock_descriptor)
        if failures:
            raise Refused("gate recovery remains quarantined: " + "; ".join(failures))
        return {
            "kind": "gate_closed",
            "receipt": drain(lease, membership, set()),
            "launch_receipts": receipts,
        }
    finally:
        os.close(lease.lock_descriptor)


def recover_reservation(lease, membership):
    """An unlaunched reservation has no authority to signal unexpected processes."""
    if lease.manifest.get("launches") != [] or not membership.empty(lease.uid):
        raise Refused("unlaunched reservation has unexpected execution or process evidence")
    lease.close()
    return lease.receipt(membership)


def recover(membership, uid):
    emit(recover_owned(STATE, membership, uid))


def authenticated_connection(operator):
    for fd in (0, 1):
        mode = os.fstat(fd).st_mode
        if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
            raise Refused("control must use bounded pipe or socket transport")
    raw = read_declaration()
    membership = DarwinMembership()
    if isinstance(raw, dict) and raw.get("kind") == "recover":
        exact_keys(raw, ("kind", "uid"))
        recover(membership, raw["uid"])
    else:
        serve(STATE, membership, GateSpec.parse(raw), operator)


def peer_uid(connection):
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    getpeereid = library.getpeereid
    getpeereid.argtypes = (
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
    )
    getpeereid.restype = ctypes.c_int
    uid, gid = ctypes.c_uint(), ctypes.c_uint()
    if getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        raise Refused("kernel peer identity is unavailable")
    return uid.value


def daemon(operator):
    """Optional unattended authority: fixed code plus kernel-authenticated peers.

    Gate users never own the socket or its containing directory. Each accepted
    host connection gets the identical one-gate core used by authenticated sudo.
    """
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # launchd supplies singleton ownership. A leftover socket has no authority;
    # its root-owned directory prevents an unprivileged replacement race.
    if SOCKET.exists():
        if not stat.S_ISSOCK(SOCKET.lstat().st_mode):
            raise Refused("control path is not the previous service socket")
        SOCKET.unlink()
    listener.bind(str(SOCKET))
    os.chown(SOCKET, operator, -1)
    os.chmod(SOCKET, 0o600)
    listener.listen(MAX_ACTIVE_GATES)
    listener.settimeout(POLL_SECONDS)
    workers = set()
    while True:
        for pid in tuple(workers):
            waited, _ = os.waitpid(pid, os.WNOHANG)
            if waited:
                workers.remove(pid)
        try:
            connection, _ = listener.accept()
        except socket.timeout:
            continue
        with connection:
            if peer_uid(connection) != operator or len(workers) >= MAX_ACTIVE_GATES:
                continue
            pid = os.fork()
            if pid:
                workers.add(pid)
                continue
            try:
                listener.close()
                os.dup2(connection.fileno(), 0)
                os.dup2(connection.fileno(), 1)
                os.set_blocking(1, False)
                authenticated_connection(operator)
            except BaseException:
                os._exit(125)
            os._exit(0)


def main():
    recovery_requested = len(sys.argv) == 3 and sys.argv[1] == "recover"
    configuration = (
        installed_operator_configuration() if recovery_requested else installed_configuration()
    )
    if sys.argv[1:] == ["serve"]:
        os.environ.clear()
        daemon(configuration["operator_uid"])
        return
    if os.environ.get("SUDO_UID") != str(configuration["operator_uid"]):
        raise Refused("sudo did not authenticate the configured operator")
    os.environ.clear()
    os.set_blocking(1, False)
    if recovery_requested:
        recover(DarwinMembership(), int(sys.argv[2]))
    elif len(sys.argv) == 1:
        authenticated_connection(configuration["operator_uid"])
    else:
        raise Refused("only gate ownership or exact quarantined-lease recovery is supported")


if __name__ == "__main__":
    try:
        main()
    except (Refused, OSError, ValueError) as failure:
        print(str(failure), file=sys.stderr)
        raise SystemExit(125) from None
