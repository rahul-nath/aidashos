#!/usr/bin/python3 -I -S
# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixed installed native qualification; never accept caller-supplied check results.

The privileged parent imports only installed root-owned code and the system
standard library. Toolchain staging runs after dropping to the operator UID.
Every probe process runs through the installed exclusive-UID helper core.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import hashlib
import importlib.util
import json
import os
import pwd
import select
import signal
import socket
import stat
import subprocess
import sys
import time
from enum import Enum
from pathlib import Path

HELPER = Path("/Library/PrivilegedHelperTools/com.aidashos.verifier-uid.py")
CANARIES = Path("/Library/PrivilegedHelperTools/com.aidashos.verifier-uid-canaries.py")
MAX_OUTPUT = 4 * 1024 * 1024


def require(condition, detail):
    if not condition:
        raise RuntimeError(detail)


class OwnedChild:
    """A direct child remains signalable only until this object reaps it."""

    def __init__(self, pid):
        self.pid, self.status = pid, None

    def signal(self, number, group=False):
        if self.status is not None:
            return
        try:
            if group and os.getpgid(self.pid) == self.pid:
                os.killpg(self.pid, number)
            else:
                os.kill(self.pid, number)
        except ProcessLookupError:
            # An exited direct child is still owned until waitpid reaps it.
            pass

    def wait(self, seconds=30):
        if self.status is not None:
            return self.status
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            waited, status = os.waitpid(self.pid, os.WNOHANG)
            if waited:
                self.status = os.waitstatus_to_exitcode(status)
                return self.status
            time.sleep(0.02)
        self.signal(signal.SIGKILL)
        _, status = os.waitpid(self.pid, 0)
        self.status = os.waitstatus_to_exitcode(status)
        raise RuntimeError("owned qualification process exceeded its deadline")


def as_operator(operator, operation):
    """Execute user-selected staging paths only after an irreversible UID drop."""
    account = pwd.getpwuid(operator)
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_end)
        try:
            os.setsid()
            os.setgroups([])
            os.setgid(account.pw_gid)
            os.setuid(operator)
            require(os.getuid() == os.geteuid() == operator, "operator staging identity mismatch")
            value = operation(account)
            payload = json.dumps({"ok": True, "value": value}).encode()
        except BaseException as failure:
            payload = json.dumps({"ok": False, "error": str(failure)}).encode()
        if len(payload) > 65536:
            payload = b'{"ok":false,"error":"operator staging report exceeded its bound"}'
        try:
            while payload:
                written = os.write(write_end, payload)
                payload = payload[written:]
        finally:
            os._exit(0)
    os.close(write_end)
    child = OwnedChild(pid)
    data, deadline = bytearray(), time.monotonic() + 180
    try:
        while time.monotonic() < deadline:
            if not select.select([read_end], [], [], 1)[0]:
                continue
            chunk = os.read(read_end, 65536)
            if not chunk:
                break
            data.extend(chunk)
            require(len(data) <= 65536, "operator staging output exceeded its bound")
        else:
            raise RuntimeError("operator staging timed out")
    finally:
        os.close(read_end)
        # The still-unreaped leader reserves this process-group identifier.
        # Close any trusted staging subprocess before reaping that leader.
        child.signal(signal.SIGKILL, group=True)
        child.wait()
    require(bool(data), "operator staging returned no report")
    report = json.loads(data)
    require(report["ok"], report.get("error", "operator staging failed"))
    return report["value"]


class Gate:
    """Parent-side protocol oracle for a real root-owned helper worker."""

    def __init__(self, helper, operator, binding, limit=128):
        self.helper, self.operator, self.binding = helper, operator, binding
        parent, child = socket.socketpair()
        self.pid = os.fork()
        if self.pid == 0:
            parent.close()
            try:
                os.setsid()
                os.dup2(child.fileno(), 0)
                os.dup2(child.fileno(), 1)
                if child.fileno() > 2:
                    child.close()
                helper.close_descriptors(3)
                os.set_blocking(1, False)
                os.environ.clear()
                spec = helper.GateSpec.parse(
                    {
                        "kind": "open",
                        "schema": helper.SCHEMA,
                        "source_binding": binding,
                        "duration": 300,
                        "process_limit": limit,
                    }
                )
                helper.serve(helper.STATE, helper.DarwinMembership(), spec, operator)
            except BaseException as failure:
                os.write(2, ("qualification helper: " + str(failure) + "\n").encode())
                os._exit(125)
            os._exit(0)
        child.close()
        self.process = OwnedChild(self.pid)
        self.socket, self.pending = parent, b""
        self.outputs, self.terminals, self.launches, self.prepared = {}, {}, {}, {}
        self.closed = False
        try:
            self.opened = self.receive()
            require(
                self.opened.get("kind") == "opened", "helper did not open an owned staging gate"
            )
        except BaseException:
            self.socket.close()
            self.process.signal(signal.SIGKILL)
            self.process.wait()
            self.closed = True
            raise

    def send(self, payload):
        self.socket.sendall(json.dumps(payload, sort_keys=True).encode() + b"\n")

    def receive(self, seconds=30):
        deadline = time.monotonic() + seconds
        while b"\n" not in self.pending:
            remaining = deadline - time.monotonic()
            require(remaining > 0, "helper protocol deadline expired")
            require(select.select([self.socket], [], [], remaining)[0], "helper stopped responding")
            chunk = self.socket.recv(65536)
            require(bool(chunk), "helper closed without a terminal cleanup report")
            self.pending += chunk
            require(len(self.pending) <= MAX_OUTPUT, "helper protocol frame exceeded its bound")
        line, self.pending = self.pending.split(b"\n", 1)
        message = json.loads(line)
        kind, handle = message["kind"], message.get("handle")
        if kind in ("stdout", "stderr"):
            output = self.outputs.setdefault(handle, {"stdout": bytearray(), "stderr": bytearray()})
            output[kind].extend(base64.b64decode(message["data"], validate=True))
            require(sum(map(len, output.values())) <= MAX_OUTPUT, "probe output exceeded its bound")
        elif kind in ("exit", "canceled"):
            require(message["receipt"]["kind"] == "cleaned", "probe lacks its UID cleanup receipt")
            require(
                self.helper.DarwinMembership().empty(message["receipt"]["uid"]),
                "receipt was emitted while its UID still had processes",
            )
            self.terminals[handle] = message
        return message

    def until(self, kind, handle=None):
        while True:
            message = self.receive()
            if message["kind"] == kind and (handle is None or message.get("handle") == handle):
                return message

    def prepare(self, parent=None):
        scratch = (
            None
            if parent is None
            else str(Path(self.prepared[parent]["scratch"]).relative_to(self.opened["staging"]))
        )
        self.send({"kind": "prepare", "parent_handle": parent, "scratch": scratch})
        prepared = self.until("prepared")
        self.prepared[prepared["handle"]] = prepared
        return prepared

    def launch(self, prepared, argv, profile, environment):
        handle = prepared["handle"]
        env = dict(environment, HOME=prepared["home"], TMPDIR=prepared["scratch"])
        self.send(
            {
                "kind": "launch",
                "handle": handle,
                "argv": argv,
                "cwd": self.opened["source"],
                "environment": env,
                "profile": profile,
                "source_binding": self.binding,
            }
        )
        message = self.until("launched", handle)
        self.launches[handle] = message
        return handle

    def result(self, handle, expected=0):
        while handle not in self.terminals:
            self.receive()
        terminal = self.terminals[handle]
        output = self.outputs.get(handle, {"stdout": b"", "stderr": b""})
        require(
            terminal["code"] == expected,
            "native probe failed: " + bytes(output["stderr"]).decode(errors="replace"),
        )
        return bytes(output["stdout"]).decode(), terminal

    def cancel(self, handle):
        self.send({"kind": "cancel", "handle": handle})
        return self.result(handle, -signal.SIGKILL)

    def close(self):
        if self.closed:
            return
        self.send({"kind": "close"})
        receipt = self.until("gate_closed")
        self.socket.close()
        require(self.process.wait() == 0, "helper failed during normal gate closure")
        require(receipt["receipt"]["kind"] == "cleaned", "aggregate cleanup missing")
        self.closed = True
        return receipt

    def interrupt(self, crash):
        if crash:
            self.process.signal(signal.SIGKILL)
        self.socket.close()
        status = self.process.wait()
        if not crash:
            require(status == 0, "disconnect cleanup failed")
            for prepared in self.prepared.values():
                require(
                    self.helper.DarwinMembership().empty(prepared["uid"]),
                    "normal disconnect left a UID alive before recovery",
                )
        recovered = self.helper.recover_owned(
            self.helper.STATE, self.helper.DarwinMembership(), self.opened["gid"]
        )
        require(recovered["kind"] == "gate_closed", "recovery did not close every linked UID")
        for receipt in recovered["launch_receipts"]:
            require(
                self.helper.DarwinMembership().empty(receipt["uid"]), "recovered UID remains alive"
            )
        self.closed = True
        return recovered


def stage(gate, project, python):
    def operation(account):
        environment = {
            "HOME": account.pw_dir,
            "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
            "PYTHONPATH": str(project / "src"),
            "UV_OFFLINE": "1",
            "UV_PYTHON_DOWNLOADS": "never",
        }
        result = subprocess.run(
            [
                str(python),
                "-m",
                "local_first_agent_os.verification_toolchain_staging",
                "--project",
                str(project),
                "--destination",
                gate.opened["toolchain"],
                "--source",
                gate.opened["source"],
            ],
            env=environment,
            cwd=project,
            capture_output=True,
            text=True,
            timeout=150,
        )
        require(result.returncode == 0, result.stderr)
        return json.loads(result.stdout)

    staged = as_operator(gate.operator, operation)
    anchor = Path(gate.opened["toolchain"])
    for path in (staged["python"], *staged["executables"]):
        require(Path(path).is_relative_to(anchor), "staged executable escaped its declared anchor")
    return staged


def environment(staged=None):
    values = {"PATH": "/usr/bin:/bin", "UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never"}
    if staged is not None:
        values["PATH"] = ":".join(str(Path(path).parent) for path in staged["executables"])
        values["VIRTUAL_ENV"] = staged["environment"]
        values["UV_PROJECT_ENVIRONMENT"] = staged["environment"]
        values["UV_PYTHON"] = staged["python"]
    return values


def run_probe(gate, argv, profile, staged=None):
    prepared = gate.prepare()
    handle = gate.launch(prepared, argv, profile, environment(staged))
    stdout, terminal = gate.result(handle)
    return {"stdout": stdout, "terminal": terminal, "launch": gate.launches[handle]}


def checked_canary(gate, operation, profile, *arguments):
    evidence = run_probe(
        gate,
        [
            str(Path(sys.executable).resolve()),
            "-I",
            "-S",
            str(CANARIES),
            operation,
            *map(str, arguments),
        ],
        profile,
    )
    observed = json.loads(evidence["stdout"])
    require(observed["operation"] == operation, "probe returned another operation's evidence")
    require(
        observed["uid"] == evidence["terminal"]["receipt"]["uid"],
        "probe identity differs from its cleanup identity",
    )
    return evidence


def clear_fixture_privileges(directory):
    for name in ("setuid-id", "setgid-id"):
        try:
            fd = os.open(directory / name, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            continue
        try:
            os.fchmod(fd, 0o755)
            require(os.fstat(fd).st_mode & 0o6000 == 0, "fixture privilege bits remain set")
        finally:
            os.close(fd)


def sign_fixture(executable, signer, environment):
    """Sign only the disposable copy with a local identity, retaining tool errors."""
    operations = (
        (
            "sign",
            [
                "--force",
                "--sign",
                "-",
                "--timestamp=none",
                "--identifier",
                "com.aidashos.verifier.identity-fixture",
            ],
            20,
        ),
        ("verify", ["--verify", "--strict"], 10),
    )
    for phase, arguments, timeout in operations:
        result = subprocess.run(
            [str(signer), *arguments, str(executable)],
            env=environment,
            cwd=executable.parent,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        require(
            result.returncode == 0,
            "fixture signature " + phase + " failed: " + result.stderr[:2048],
        )


def fixed_files(helper, operator):
    directory = helper.STATE / ("qualification-" + os.urandom(8).hex())
    directory.mkdir(mode=0o700)
    helper.root_owned(directory)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        signer = Path("/usr/bin/codesign")
        allocator = Path("/Library/Developer/CommandLineTools/usr/bin/codesign_allocate")
        helper.root_owned(signer)
        helper.root_owned(allocator)
        signing_environment = {"PATH": "/usr/bin:/bin", "CODESIGN_ALLOCATE": str(allocator)}
        binary = Path("/usr/bin/id").read_bytes()
        for name, mode in (("ordinary-id", 0o755), ("setuid-id", 0o4755), ("setgid-id", 0o2755)):
            executable = directory / name
            fd = os.open(executable, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755)
            with os.fdopen(fd, "wb") as stream:
                stream.write(binary)
                stream.flush()
                os.fsync(stream.fileno())
            # Apple's system-binary signature can constrain its original launch
            # location. Give only the private disposable copy a local signature,
            # without a signing identity, timestamp service, or inherited options.
            sign_fixture(executable, signer, signing_environment)
            # Signing can replace the file. Pin the final inode before installing
            # privilege bits; no set-id fixture is exposed before signing succeeds.
            fd = os.open(executable, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                require(
                    stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                    "signed fixture is not a single regular file",
                )
                os.fchmod(stream.fileno(), mode)
        forbidden = directory / "forbidden-canary"
        fd = os.open(forbidden, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666)
        with os.fdopen(fd, "w") as stream:
            stream.write("outside the declared read grant\n")
            stream.flush()
            os.fchmod(stream.fileno(), 0o666)
        # Creation modes are filtered by the operator's umask. Publish search
        # access on this pinned root-owned fixture directory only after every
        # fixture is complete; otherwise a DAC denial masks the sandbox test.
        os.fchmod(descriptor, 0o755)
        return directory, forbidden
    except BaseException:
        clear_fixture_privileges(directory)
        raise
    finally:
        os.close(descriptor)


def cross_uid_acl(gate, staged, profile):
    """Create mode0700/0600 state as one launch UID and read it as its child UID."""
    path = Path(gate.opened["source"]) / "cross-uid"
    source = (
        "import os,pathlib,time; p=pathlib.Path(" + repr(str(path)) + "); "
        "p.mkdir(mode=0o700); f=p/'state'; f.write_text('inherited access'); f.chmod(0o600); "
        "print('ready',flush=True); time.sleep(120)"
    )
    parent = gate.prepare()
    parent_handle = gate.launch(
        parent, [staged["python"], "-c", source], profile, environment(staged)
    )
    while b"ready\n" not in gate.outputs.get(parent_handle, {}).get("stdout", b""):
        require(
            parent_handle not in gate.terminals,
            "ACL fixture parent exited before preparing its state",
        )
        gate.receive()
    child = gate.prepare(parent_handle)
    code = (
        "from pathlib import Path; assert Path("
        + repr(str(path / "state"))
        + ").read_text()=='inherited access'"
    )
    child_handle = gate.launch(child, [staged["python"], "-c", code], profile, environment(staged))
    _, child_receipt = gate.result(child_handle)
    require(parent["uid"] != child["uid"], "ACL probe reused the parent UID")
    os.kill(gate.launches[parent_handle]["pid"], 0)
    canceled = gate.prepare(parent_handle)
    canceled_handle = gate.launch(canceled, ["/bin/sleep", "120"], profile, environment())
    _, canceled_receipt = gate.cancel(canceled_handle)
    os.kill(gate.launches[parent_handle]["pid"], 0)
    _, parent_receipt = gate.cancel(parent_handle)
    return {
        "read": child_receipt,
        "child_canceled": canceled_receipt,
        "parent_canceled": parent_receipt,
    }


class JobSubmission(Enum):
    CREATED = "created"
    PERMISSION_DENIED = "permission_denied"
    OTHER_FAILURE = "other_failure"


def launchd_request(operation, label):
    """Use launch.h's tagged responses, never launchctl's diagnostic wording."""
    require(operation in {"SubmitJob", "GetJob", "RemoveJob"}, "unknown fixed job operation")
    prefix = "com.aidashos.qualify."
    require(
        label.startswith(prefix)
        and len(label) == len(prefix) + 24
        and all(character in "0123456789abcdef" for character in label[len(prefix) :]),
        "job probe requires its fresh qualification label",
    )
    library = ctypes.CDLL("/usr/lib/system/liblaunch.dylib", use_errno=True)
    pointer = ctypes.c_void_p
    signatures = (
        ("launch_data_alloc", [ctypes.c_int], pointer),
        ("launch_data_new_string", [ctypes.c_char_p], pointer),
        ("launch_data_new_bool", [ctypes.c_bool], pointer),
        ("launch_data_dict_insert", [pointer, pointer, ctypes.c_char_p], ctypes.c_bool),
        ("launch_data_array_set_index", [pointer, pointer, ctypes.c_size_t], ctypes.c_bool),
        ("launch_data_get_type", [pointer], ctypes.c_int),
        ("launch_data_get_errno", [pointer], ctypes.c_int),
        ("launch_data_free", [pointer], None),
        ("launch_msg", [pointer], pointer),
    )
    for name, arguments, result in signatures:
        function = getattr(library, name)
        function.argtypes, function.restype = arguments, result

    def allocate(value):
        if isinstance(value, str):
            item = library.launch_data_new_string(value.encode())
        elif isinstance(value, bool):
            item = library.launch_data_new_bool(value)
        else:
            item = library.launch_data_alloc(1 if isinstance(value, dict) else 2)
        require(bool(item), "liblaunch allocation failed")
        try:
            entries = (
                value.items()
                if isinstance(value, dict)
                else enumerate(value)
                if isinstance(value, list)
                else ()
            )
            for key, child in entries:
                nested = allocate(child)
                if isinstance(value, dict):
                    assert isinstance(key, str), "fixed job dictionary keys must be strings"
                    inserted = library.launch_data_dict_insert(item, nested, key.encode())
                else:
                    inserted = library.launch_data_array_set_index(item, nested, key)
                if not inserted:
                    library.launch_data_free(nested)
                    raise RuntimeError("liblaunch insertion failed")
            return item
        except BaseException:
            library.launch_data_free(item)
            raise

    value = (
        {"Label": label, "ProgramArguments": ["/usr/bin/true"], "RunAtLoad": True}
        if operation == "SubmitJob"
        else label
    )
    request = allocate({operation: value})
    try:
        ctypes.set_errno(0)
        response = library.launch_msg(request)
        transport_error = ctypes.get_errno()
    finally:
        library.launch_data_free(request)
    if not response:
        return {"kind": "transport_error", "errno": transport_error}
    try:
        kind = library.launch_data_get_type(response)
        if kind == 9:  # LAUNCH_DATA_ERRNO from the installed Apple launch.h.
            return {"kind": "errno", "errno": library.launch_data_get_errno(response)}
        if kind == 1:  # LAUNCH_DATA_DICTIONARY: GetJob found the exact label.
            return {"kind": "job"}
        return {"kind": "unexpected_type", "type": kind}
    finally:
        library.launch_data_free(response)


def native_job_canary(label):
    """Query and clean in the submitting process's exact UID/bootstrap domain."""
    report = {"label": label, "uid": os.getuid(), "euid": os.geteuid(), "steps": {}, "errors": []}
    steps, attempted = report["steps"], False
    try:
        steps["before"] = launchd_request("GetJob", label)
        require(steps["before"] == {"kind": "errno", "errno": errno.ESRCH}, "job label exists")
        attempted = True
        steps["submit"] = launchd_request("SubmitJob", label)
        steps["after"] = launchd_request("GetJob", label)
    except BaseException as failure:
        report["errors"].append(
            {"phase": "submit", "type": type(failure).__name__, "detail": str(failure)}
        )
    finally:
        try:
            if attempted:
                # A failed/ambiguous submit may still have created the job. Only
                # this fresh, preflight-absent label belongs to this cleanup.
                for phase, operation in (("remove", "RemoveJob"), ("final", "GetJob")):
                    try:
                        steps[phase] = launchd_request(operation, label)
                        if phase == "final":
                            deadline = time.monotonic() + 3
                            while steps[phase] == {"kind": "job"} and time.monotonic() < deadline:
                                time.sleep(0.02)
                                steps[phase] = launchd_request(operation, label)
                    except BaseException as failure:
                        report["errors"].append(
                            {"phase": phase, "type": type(failure).__name__, "detail": str(failure)}
                        )
        finally:
            print(json.dumps(report, sort_keys=True), flush=True)


def job_probe_argv(label):
    return [
        str(Path(sys.executable).resolve()),
        "-I",
        "-S",
        "-c",
        "import runpy,signal,sys; "
        "module=runpy.run_path(sys.argv[1],run_name='job_probe'); "
        "signal.signal(signal.SIGTERM,module['interrupt_job_probe']); "
        "module['native_job_canary'](sys.argv[2])",
        str(Path(__file__).resolve()),
        label,
    ]


def interrupt_job_probe(number, _frame):
    raise InterruptedError("job probe interrupted by signal " + str(number))


def captured_process(status, stdout, stderr):
    return {
        "status": status,
        "stdout_base64": base64.b64encode(stdout).decode(),
        "stderr_base64": base64.b64encode(stderr).decode(),
    }


def operator_job_probe(label):
    with subprocess.Popen(
        job_probe_argv(label), env=environment(), stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as child:
        try:
            stdout, stderr = child.communicate(timeout=10)
            status = {"kind": "exited", "code": child.returncode}
        except subprocess.TimeoutExpired:
            # TERM enters the fixed canary's cleanup; SIGKILL is only the final
            # bound. A timeout never certifies submission or cleanup success.
            child.terminate()
            try:
                stdout, stderr = child.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                stdout, stderr = child.communicate()
            status = {"kind": "timeout", "code": child.returncode, "cleanup": "unproven"}
        return captured_process(status, stdout, stderr)


def job_submission(evidence, label, uid):
    if not isinstance(evidence, dict):
        return JobSubmission.OTHER_FAILURE
    status = evidence.get("status")
    if (
        not isinstance(status, dict)
        or status != {"kind": "exited", "code": 0}
        or type(status["code"]) is not int
    ):
        return JobSubmission.OTHER_FAILURE
    encoded = evidence.get("stdout_base64")
    if not isinstance(encoded, str):
        return JobSubmission.OTHER_FAILURE
    try:
        observed = json.loads(base64.b64decode(encoded, validate=True))
    except (ValueError, UnicodeError, TypeError):
        return JobSubmission.OTHER_FAILURE
    if not isinstance(observed, dict) or set(observed) != {
        "label",
        "uid",
        "euid",
        "steps",
        "errors",
    }:
        return JobSubmission.OTHER_FAILURE
    if (
        observed["errors"] != []
        or observed["label"] != label
        or type(observed["uid"]) is not int
        or type(observed["euid"]) is not int
        or observed["uid"] != uid
        or observed["euid"] != uid
    ):
        return JobSubmission.OTHER_FAILURE
    steps = observed["steps"]
    absent, success = {"kind": "errno", "errno": errno.ESRCH}, {"kind": "errno", "errno": 0}
    if not isinstance(steps, dict) or set(steps) != {
        "before",
        "submit",
        "after",
        "remove",
        "final",
    }:
        return JobSubmission.OTHER_FAILURE
    for value in steps.values():
        if value == {"kind": "job"}:
            continue
        if (
            not isinstance(value, dict)
            or set(value) != {"kind", "errno"}
            or value["kind"] != "errno"
            or type(value["errno"]) is not int
        ):
            return JobSubmission.OTHER_FAILURE
    if steps["before"] != absent or steps["final"] != absent:
        return JobSubmission.OTHER_FAILURE
    if (
        steps["submit"] == success
        and steps["after"] == {"kind": "job"}
        and steps["remove"] in (success, {"kind": "errno", "errno": errno.EINPROGRESS})
    ):
        return JobSubmission.CREATED
    if (
        steps["submit"]
        in ({"kind": "errno", "errno": errno.EPERM}, {"kind": "errno", "errno": errno.EACCES})
        and steps["after"] == absent
        and steps["remove"] == absent
    ):
        return JobSubmission.PERMISSION_DENIED
    return JobSubmission.OTHER_FAILURE


def closed_job_probe(gate, profile, evidence):
    baseline_label = "com.aidashos.qualify." + os.urandom(12).hex()
    evidence["baseline"] = {
        "label": baseline_label,
        "uid": gate.operator,
        "process": captured_process({"kind": "incomplete"}, b"", b""),
    }
    evidence["baseline"]["process"] = as_operator(
        gate.operator, lambda _account: operator_job_probe(baseline_label)
    )
    require(
        job_submission(evidence["baseline"]["process"], baseline_label, gate.operator)
        is JobSubmission.CREATED,
        "job-creation positive baseline failed; retained native response and process evidence",
    )
    label = "com.aidashos.qualify." + os.urandom(12).hex()
    prepared = gate.prepare()
    handle = prepared["handle"]
    evidence["contained"] = {"label": label, "prepared": prepared}
    try:
        gate.launch(prepared, job_probe_argv(label), profile, environment())
        evidence["contained"]["launch"] = gate.launches[handle]
        while handle not in gate.terminals:
            gate.receive()
    finally:
        terminal = gate.terminals.get(handle)
        output = gate.outputs.get(handle, {})
        evidence["contained"].update(
            terminal=terminal,
            process=captured_process(
                {"kind": "exited", "code": terminal["code"]}
                if terminal
                else {"kind": "incomplete"},
                bytes(output.get("stdout", b"")),
                bytes(output.get("stderr", b"")),
            ),
        )
    outcome = job_submission(evidence["contained"]["process"], label, prepared["uid"])
    evidence["outcome"] = outcome.value
    require(
        outcome is JobSubmission.PERMISSION_DENIED
        and terminal["receipt"]["uid"] == prepared["uid"],
        "job-creation probe lacks typed permission refusal and exact-domain absence; "
        "evidence retained",
    )


def qualify(helper, operator, project, python):
    binding = hashlib.sha256(
        HELPER.read_bytes() + Path(__file__).read_bytes() + CANARIES.read_bytes()
    ).hexdigest()
    files, forbidden = fixed_files(helper, operator)
    profile = (
        "(version 1)(allow default)(deny network*)(deny file-read* file-write* (literal "
        + json.dumps(str(forbidden))
        + "))"
    )
    proofs, gates = {}, []
    try:
        gate = Gate(helper, operator, binding)
        gates.append(gate)
        staged = stage(gate, project, python)
        executables = {Path(path).name: path for path in staged["executables"]}
        require(
            "uv" in executables and "node" in executables, "installed uv and Node were not staged"
        )
        proofs["normal-uv-python-node"] = [
            run_probe(gate, [executables["uv"], "--version"], profile, staged),
            run_probe(
                gate,
                [staged["python"], "-c", "import ssl,sqlite3; print('python ready')"],
                profile,
                staged,
            ),
            run_probe(
                gate,
                [
                    executables["node"],
                    "-e",
                    "const r=require('node:child_process').spawnSync('/usr/bin/true'); "
                    "if(r.error || r.status!==0) throw r.error || Error('spawn failed'); "
                    "console.log('node spawn ready')",
                ],
                profile,
                staged,
            ),
            run_probe(
                gate,
                [
                    executables["uv"],
                    "run",
                    "--offline",
                    "--no-project",
                    "--python",
                    staged["python"],
                    "python",
                    "-c",
                    "print('uv spawned Python')",
                ],
                profile,
                staged,
            ),
        ]
        proofs["relocated-installed-toolchain"] = {
            "manifest_digest": staged["manifest_digest"],
            "python": staged["python"],
            "normal_tools_passed": True,
        }
        proofs["thread-credentials"] = checked_canary(gate, "credentials", profile)
        proofs["persona-spawn"] = checked_canary(gate, "spawn-credentials", profile)
        proofs["set-id-exec-and-spawn"] = checked_canary(gate, "set-id", profile, files)
        proofs["detached-descendant-cleanup"] = checked_canary(gate, "detach", profile)
        allowed = Path(gate.opened["source"]) / "allowed-canary"
        as_operator(operator, lambda _account: allowed.write_text("allowed canary\n"))
        proofs["declared-read-denials"] = checked_canary(
            gate, "filesystem", profile, allowed, forbidden
        )
        proofs["cross-uid-inherited-staging-acl"] = cross_uid_acl(gate, staged, profile)
        proofs["external-job-creation"] = {}
        closed_job_probe(gate, profile, proofs["external-job-creation"])
        gate.close()

        limited = Gate(helper, operator, binding, limit=8)
        gates.append(limited)
        proofs["uid-process-limit"] = checked_canary(limited, "process-limit", profile)
        limited.close()

        for check, crash in (("parent-disconnect", False), ("helper-crash-recovery", True)):
            interrupted = Gate(helper, operator, binding)
            gates.append(interrupted)
            prepared = interrupted.prepare()
            interrupted.launch(prepared, ["/bin/sleep", "120"], profile, environment())
            proofs[check] = interrupted.interrupt(crash)
        require(set(proofs) == helper.QUALIFICATION_CHECKS, "native qualification is incomplete")
        report = {
            "binding": binding,
            "helper_sha256": hashlib.sha256(HELPER.read_bytes()).hexdigest(),
            "kernel_release": os.uname().release,
            "proofs": proofs,
        }
        helper.persist(files / "observed-results.json", report)
        return {
            "qualified": True,
            "evidence": str(files / "observed-results.json"),
            "checks": sorted(proofs),
            "helper_sha256": report["helper_sha256"],
            "kernel_release": report["kernel_release"],
        }
    except BaseException as failure:
        try:
            helper.persist(
                files / "observed-results.json",
                {
                    "qualified": False,
                    "binding": binding,
                    "proofs": proofs,
                    "error_type": type(failure).__name__,
                    "error": str(failure),
                },
            )
        except BaseException as persistence_failure:
            print(
                "Qualification evidence persistence also failed: " + str(persistence_failure),
                file=sys.stderr,
            )
        raise
    finally:
        original_failure = sys.exc_info()[1]
        cleanup_failures = []
        for gate in reversed(gates):
            if not gate.closed:
                try:
                    try:
                        gate.close()
                    except BaseException:
                        gate.interrupt(True)
                except BaseException as failure:
                    cleanup_failures.append(failure)
        try:
            clear_fixture_privileges(files)
        except BaseException as failure:
            cleanup_failures.append(failure)
        if cleanup_failures:
            if original_failure is None:
                raise RuntimeError("qualification cleanup failed") from cleanup_failures[0]
            for failure in cleanup_failures:
                print("Qualification cleanup also failed: " + str(failure), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--operator-python", type=Path, required=True)
    args = parser.parse_args()
    require(
        os.geteuid() == 0 and sys.platform == "darwin" and sys.flags.isolated and sys.flags.no_site,
        "run the fixed installed qualifier with authenticated system Python -I -S",
    )
    require(
        args.project.is_absolute() and args.operator_python.is_absolute(),
        "staging paths must be absolute",
    )
    # Check ownership before importing the only non-stdlib privileged module.
    for path in (
        HELPER,
        CANARIES,
        Path(__file__).absolute(),
        Path(sys.executable).resolve(),
        Path(sys.prefix).resolve(),
    ):
        for part in (path, *path.parents):
            info = part.lstat()
            require(
                not stat.S_ISLNK(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                "native qualification requires immutable root-owned installed code",
            )
    spec = importlib.util.spec_from_file_location("aidashos_uid_helper", HELPER)
    if spec is None or spec.loader is None:
        raise RuntimeError("installed helper cannot be loaded")
    helper = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helper
    spec.loader.exec_module(helper)
    helper.root_owned(helper.CONFIG)
    config = json.loads(helper.CONFIG.read_bytes())
    helper.exact_keys(config, ("operator_uid", "qualification"))
    operator = helper.integer(config["operator_uid"], 1, helper.FIRST_UID - 1)
    require(
        os.environ.get("SUDO_UID") == str(operator), "sudo must authenticate the installed operator"
    )
    service = subprocess.run(
        ["/bin/launchctl", "print", "system/com.aidashos.verifier-uid"],
        capture_output=True,
        timeout=5,
    )
    require(service.returncode != 0, "stop the installed helper service before qualifying its core")
    report = qualify(helper, operator, args.project, args.operator_python)
    # qualify returns only after every probe, UID closure, and set-id fixture
    # cleanup succeeded. The installed host cannot activate a partial run.
    config["qualification"] = {
        key: report[key] for key in ("helper_sha256", "kernel_release", "checks")
    }
    helper.persist(helper.CONFIG, config)
    helper.installed_configuration()
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
