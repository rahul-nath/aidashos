#!/usr/bin/python3
# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixed native probes, executed only after the helper drops its privileged identity.

Spawn attribute ABIs are from Apple's libsyscall/wrappers/spawn/posix_spawn.c.
The installed SDK declares posix_spawnattr_t as void*, SYS_settid=285 and
SYS_settid_with_pid=311. A missing probe is a failed qualification, never a pass.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import resource
import signal
import subprocess
import time
from pathlib import Path


def require(condition, detail):
    if not condition:
        raise RuntimeError(detail)


def same_identity(expected):
    require(
        (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) == expected,
        "a credential probe changed the launch identity",
    )


def credential_probes():
    expected = (os.getuid(), os.geteuid(), os.getgid(), os.getegid())
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    outcomes = {}
    for name, arguments in (
        ("setuid", (0,)),
        ("seteuid", (0,)),
        ("setreuid", (0, 0)),
        ("setgid", (0,)),
        ("setegid", (0,)),
        ("setregid", (0, 0)),
    ):
        function = getattr(library, name)
        function.argtypes = [ctypes.c_uint] * len(arguments)
        function.restype = ctypes.c_int
        ctypes.set_errno(0)
        result = function(*arguments)
        error = ctypes.get_errno()
        same_identity(expected)
        require(result == -1 and error == errno.EPERM, name + " did not refuse privilege change")
        outcomes[name] = error
    library.syscall.restype = ctypes.c_long
    for name, number, arguments in (
        ("settid", 285, (0, 0)),
        ("settid_with_pid", 311, (1, 1)),
    ):
        ctypes.set_errno(0)
        result = library.syscall(
            ctypes.c_int(number), *(ctypes.c_int(value) for value in arguments)
        )
        error = ctypes.get_errno()
        same_identity(expected)
        require(result == -1 and error == errno.EPERM, name + " did not refuse thread credentials")
        outcomes[name] = error
    return outcomes


def spawn_credentials():
    """Exercise credential changes embedded in posix_spawn, outside setter syscalls."""
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    pointer = ctypes.POINTER(ctypes.c_void_p)
    for name in ("posix_spawnattr_init", "posix_spawnattr_destroy"):
        function = getattr(library, name)
        function.argtypes, function.restype = [pointer], ctypes.c_int
    spawn = library.posix_spawn
    spawn.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_char_p,
        ctypes.c_void_p,
        pointer,
        ctypes.POINTER(ctypes.c_char_p),
        ctypes.POINTER(ctypes.c_char_p),
    ]
    spawn.restype = ctypes.c_int
    argv = (ctypes.c_char_p * 2)(b"/usr/bin/true", None)
    environment = (ctypes.c_char_p * 1)(None)
    results = {}
    baseline = ctypes.c_int()
    require(
        spawn(ctypes.byref(baseline), b"/usr/bin/true", None, None, argv, environment) == 0,
        "ordinary posix_spawn is unavailable",
    )
    _, baseline_status = os.waitpid(baseline.value, 0)
    require(baseline_status == 0, "ordinary spawned executable failed")
    groups = (ctypes.c_uint * 1)(0)
    cases = (
        (("posix_spawnattr_set_uid_np", (0,)),),
        (("posix_spawnattr_set_gid_np", (0,)),),
        (("posix_spawnattr_set_groups_np", (1, groups, 0)),),
        (
            ("posix_spawnattr_set_persona_np", (0, 1)),
            ("posix_spawnattr_set_persona_uid_np", (0,)),
            ("posix_spawnattr_set_persona_gid_np", (0,)),
        ),
    )
    for setters in cases:
        attribute = ctypes.c_void_p()
        require(
            library.posix_spawnattr_init(ctypes.byref(attribute)) == 0,
            "spawn attribute init failed",
        )
        try:
            for name, arguments in setters:
                function = getattr(library, name)
                function.argtypes = (
                    [pointer, ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.c_uint]
                    if name == "posix_spawnattr_set_groups_np"
                    else [pointer] + [ctypes.c_uint] * len(arguments)
                )
                function.restype = ctypes.c_int
                require(function(ctypes.byref(attribute), *arguments) == 0, name + " unavailable")
            pid = ctypes.c_int()
            result = spawn(
                ctypes.byref(pid),
                b"/usr/bin/true",
                None,
                ctypes.byref(attribute),
                argv,
                environment,
            )
            if result == 0:
                os.waitpid(pid.value, 0)
            require(
                result in (errno.EPERM, errno.EACCES), "spawn credential request was not denied"
            )
            results[setters[0][0]] = result
        finally:
            library.posix_spawnattr_destroy(ctypes.byref(attribute))
    return results


def set_id_exec(directory):
    results = {}
    ordinary = directory / "ordinary-id"
    baseline = subprocess.run([str(ordinary), "-u"], capture_output=True, text=True, check=True)
    require(int(baseline.stdout.strip()) == os.geteuid(), "ordinary copied executable failed")
    for name in ("setuid-id", "setgid-id"):
        executable = directory / name
        for mechanism in ("exec", "spawn"):
            try:
                if mechanism == "exec":
                    read_end, write_end = os.pipe()
                    pid = os.fork()
                    if pid == 0:
                        os.close(read_end)
                        try:
                            os.execve(str(executable), (str(executable), "-u"), {})
                        except OSError as error:
                            os.write(write_end, str(error.errno).encode())
                        os._exit(0)
                    os.close(write_end)
                    try:
                        observed = os.read(read_end, 32)
                    finally:
                        os.close(read_end)
                        _, status = os.waitpid(pid, 0)
                    require(
                        status == 0 and observed == str(errno.EPERM).encode(),
                        "set-id execve was not refused with sandbox EPERM",
                    )
                    results[name + ":" + mechanism] = errno.EPERM
                    continue
                pid = os.posix_spawn(str(executable), (str(executable), "-u"), {})
                os.waitpid(pid, 0)
                raise RuntimeError("set-id posix_spawn was admitted")
            except OSError as error:
                require(error.errno == errno.EPERM, "set-id refusal was not sandbox EPERM")
                results[name + ":" + mechanism] = error.errno
    return results


def process_limit():
    limit = int(resource.getrlimit(resource.RLIMIT_NPROC)[0])
    require(2 <= limit <= 16, "qualification must use a small declared process limit")
    children, refusal = [], None
    try:
        for _ in range(limit + 1):
            try:
                pid = os.fork()
            except OSError as error:
                refusal = error.errno
                break
            if pid == 0:
                time.sleep(30)
                os._exit(0)
            children.append(pid)
        require(
            refusal == errno.EAGAIN and len(children) < limit,
            "untrusted fork count exceeded its inherited limit",
        )
        return {"inherited_limit": limit, "children": len(children), "refusal": refusal}
    finally:
        for pid in children:
            os.kill(pid, signal.SIGKILL)
        for pid in children:
            os.waitpid(pid, 0)


def detach():
    child = os.fork()
    if child == 0:
        os.setsid()
        grandchild = os.fork()
        if grandchild == 0:
            os.close(1)
            os.close(2)
            time.sleep(120)
            os._exit(0)
        os._exit(0)
    os.waitpid(child, 0)
    spawned = os.posix_spawn("/bin/sleep", ("/bin/sleep", "120"), {}, setpgroup=0)
    return {"detached_child": child, "spawned_group": spawned, "uid": os.getuid()}


def filesystem_probe(allowed, denied):
    data = allowed.read_text()
    require(data == "allowed canary\n", "declared read did not work")
    try:
        denied.read_bytes()
    except PermissionError:
        return {"allowed": True, "denied": True}
    raise RuntimeError("undeclared read was allowed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=(
            "credentials",
            "spawn-credentials",
            "set-id",
            "process-limit",
            "detach",
            "filesystem",
        ),
    )
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    require(
        os.getuid() == os.geteuid() and os.getuid() >= 55000,
        "qualification probes must execute under an exclusive unprivileged UID",
    )
    functions = {
        "credentials": credential_probes,
        "spawn-credentials": spawn_credentials,
        "set-id": set_id_exec,
        "process-limit": process_limit,
        "detach": detach,
        "filesystem": filesystem_probe,
    }
    result = functions[args.operation](*args.paths)
    print(
        json.dumps(
            {"operation": args.operation, "uid": os.getuid(), "result": result}, sort_keys=True
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
