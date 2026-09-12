"""Unprivileged contract checks; these do not qualify alternate-UID execution."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SOURCE = Path(__file__).with_name("uid_gate.py")
SPEC = importlib.util.spec_from_file_location("uid_gate", SOURCE)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


def declaration(**changes):
    raw = {
        "kind": "open",
        "schema": gate.SCHEMA,
        "source_binding": "a" * 64,
        "duration": 3600,
        "process_limit": 64,
    }
    raw.update(changes)
    return raw


def launch(**changes):
    raw = {
        "kind": "launch",
        "argv": ["/usr/bin/true"],
        "cwd": "/private/tmp",
        "environment": {"HOME": "/private/tmp/home", "TMPDIR": "/private/tmp/scratch"},
        "profile": "(version 1) (allow default)",
        "source_binding": "a" * 64,
    }
    raw.update(changes)
    return raw


class CredentialChecks(unittest.TestCase):
    def test_kernel_query_requires_exactly_the_assigned_group(self):
        for count, observed, expected, error in (
            (1, 55004, 55004, None),
            (1, 0, 55004, gate.Refused),
            (0, 0, 55004, gate.Refused),
            (2, 55004, 55004, gate.Refused),
            (-1, 55004, 55004, OSError),
        ):
            with self.subTest(count=count, observed=observed):

                def query(capacity, pointer, observed=observed, count=count):
                    self.assertEqual(capacity, 1)
                    ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint32))[0] = observed
                    ctypes.set_errno(errno.EINVAL)
                    return count

                library = SimpleNamespace(getgroups=query)
                with patch.object(gate.ctypes, "CDLL", return_value=library) as load:
                    if error is None:
                        gate.require_exclusive_kernel_group(expected)
                    else:
                        with self.assertRaises(error):
                            gate.require_exclusive_kernel_group(expected)
                    load.assert_called_once_with("/usr/lib/libSystem.B.dylib", use_errno=True)

    def test_drop_uses_kernel_groups_and_never_directory_membership(self):
        with ExitStack() as stack:
            for name, result in (
                ("getuid", 55004),
                ("geteuid", 55004),
                ("getgid", 55001),
                ("getegid", 55001),
            ):
                stack.enter_context(patch.object(gate.os, name, return_value=result))
            stack.enter_context(patch.object(gate.resource, "setrlimit"))
            groups = stack.enter_context(patch.object(gate.os, "setgroups"))
            stack.enter_context(patch.object(gate.os, "setgid"))
            uid = stack.enter_context(
                patch.object(
                    gate.os, "setuid", side_effect=[None, PermissionError(errno.EPERM, "refused")]
                )
            )
            stack.enter_context(
                patch.object(
                    gate.os, "seteuid", side_effect=PermissionError(errno.EPERM, "refused")
                )
            )
            stack.enter_context(
                patch.object(
                    gate.os,
                    "getgroups",
                    side_effect=AssertionError("directory membership is not a kernel credential"),
                )
            )
            kernel = stack.enter_context(patch.object(gate, "require_exclusive_kernel_group"))
            gate.drop_identity(55004, 64, 55001)
            groups.assert_called_once_with([])
            kernel.assert_called_once_with(55001)
            self.assertEqual(uid.call_args_list[-1].args, (0,))

    def test_drop_refuses_saved_root_and_scalar_identity_mismatch(self):
        for uid_matches in (False, True):
            with self.subTest(uid_matches=uid_matches), ExitStack() as stack:
                for name, result in (
                    ("getuid", 55004 if uid_matches else 0),
                    ("geteuid", 55004),
                    ("getgid", 55001),
                    ("getegid", 55001),
                ):
                    stack.enter_context(patch.object(gate.os, name, return_value=result))
                stack.enter_context(patch.object(gate.resource, "setrlimit"))
                for name in ("setgroups", "setgid", "setuid", "seteuid"):
                    stack.enter_context(patch.object(gate.os, name))
                stack.enter_context(patch.object(gate, "require_exclusive_kernel_group"))
                with self.assertRaises(gate.Refused):
                    gate.drop_identity(55004, 64, 55001)

    def test_exec_failure_preserves_phase_errno_without_argv_or_environment(self):
        spec = SimpleNamespace(
            argv=("/definitely-missing-private-argv-canary",),
            cwd="/private/tmp",
            profile="",
            environment={"SECRET": "environment-canary"},
        )
        with patch.object(gate, "drop_identity"), patch.object(gate, "apply_sandbox"):
            pid, out, error = gate.fork_launch(spec, 55004, 64, {}, "failure")
        try:
            _, status = os.waitpid(pid, 0)
            output = os.read(error, 4096)
            self.assertEqual(os.waitstatus_to_exitcode(status), 125)
            self.assertNotIn(b"private-argv-canary", output)
            self.assertNotIn(b"environment-canary", output)
            record = json.loads(output.split(b": ", 1)[1])
            self.assertEqual(
                record, {"phase": "exec", "error_type": "FileNotFoundError", "errno": errno.ENOENT}
            )
        finally:
            os.close(out)
            os.close(error)


class Contracts(unittest.TestCase):
    def test_identity_is_not_caller_selectable(self):
        for changes in (
            {"uid": 501},
            {"process_limit": True},
            {"duration": float("nan")},
            {"duration": 3601},
            {"source_binding": "wrong"},
        ):
            with self.subTest(changes=changes), self.assertRaises(gate.Refused):
                gate.GateSpec.parse(declaration(**changes))

    def test_launch_binds_exact_source_argv_profile_and_environment(self):
        spec = gate.GateSpec.parse(declaration())
        valid = gate.LaunchSpec.parse(launch(), spec)
        self.assertEqual(valid.argv, ("/usr/bin/true",))
        self.assertTrue(valid.profile.endswith(gate.IDENTITY_RULES))
        for changes in ({"source_binding": "b" * 64}, {"uid": 0}, {"argv": ["true"]}):
            with self.subTest(changes=changes), self.assertRaises(gate.Refused):
                gate.LaunchSpec.parse(launch(**changes), spec)
        altered = gate.LaunchSpec.parse(launch(argv=["/usr/bin/true", "argument"]), spec)
        self.assertNotEqual(gate.digest(valid.payload()), gate.digest(altered.payload()))

    def test_policy_cannot_remove_sandbox_or_evaluate_scheme(self):
        for extra in (
            "(allow process-exec (with no-sandbox))",
            '(import "system.sb")',
            '(load "/tmp/operator.py")',
            "(allow syscall-unix)",
            "(version 1))",
        ):
            with self.subTest(extra=extra), self.assertRaises(gate.Refused):
                gate.protected_profile("(version 1)(allow default)" + extra)
        self.assertIn(
            '"/tmp/no-sandbox"',
            gate.protected_profile('(version 1)(deny file-read-data (literal "/tmp/no-sandbox"))'),
        )

    def test_tcp_grant_retains_transport_endpoint_and_mandatory_guard(self):
        profile = (
            "(version 1)(allow default)(deny network*)"
            '(allow network-outbound (remote tcp "localhost:65432"))'
        )
        self.assertEqual(gate.protected_profile(profile), profile + " " + gate.IDENTITY_RULES)
        with self.assertRaises(gate.Refused):
            gate.protected_profile(profile.replace("remote tcp", "remote udp"))

    def test_zero_query_distinguishes_errno_and_occupied_result(self):
        membership = object.__new__(gate.DarwinMembership)

        def result(size, error=0):
            def query(_kind, _uid, pointer, length):
                self.assertIsNotNone(pointer)
                self.assertEqual(length, ctypes.sizeof(ctypes.c_int))
                self.assertEqual(ctypes.get_errno(), 0)
                ctypes.set_errno(error)
                return size

            return query

        membership._list = result(0)
        ctypes.set_errno(errno.ENOMEM)
        self.assertTrue(membership.empty(gate.FIRST_UID))
        membership._list = result(4)
        self.assertFalse(membership.empty(gate.FIRST_UID))
        for size, error in ((0, errno.ENOMEM), (-1, 0), (8, 0)):
            membership._list = result(size, error)
            with self.assertRaises(gate.Refused):
                membership.empty(gate.FIRST_UID)

    def test_zero_requires_real_and_effective_identity_empty(self):
        membership = object.__new__(gate.DarwinMembership)
        membership._list = Mock(side_effect=[0, 4])
        self.assertFalse(membership.empty(gate.FIRST_UID))
        self.assertEqual([call.args[0] for call in membership._list.call_args_list], [4, 5])

    def test_closed_expired_and_pending_cancel_forbid_final_fork(self):
        lease = Mock(closed=True)
        with self.assertRaises(gate.Refused), gate.admission(lease, time.monotonic() + 10):
            self.fail("closed lease entered launch critical section")
        lease.closed = False
        with self.assertRaises(gate.Refused), gate.admission(lease, time.monotonic() - 1):
            self.fail("expired lease entered launch critical section")
        with (
            patch.object(gate.signal, "sigpending", return_value={gate.signal.SIGTERM}),
            self.assertRaises(gate.Refused),
            gate.admission(lease, time.monotonic() + 10),
        ):
            self.fail("canceled lease entered launch critical section")

    def test_drop_order_and_failed_saved_identity_proof(self):
        uid, calls = gate.FIRST_UID, []
        with (
            patch.multiple(
                gate.os,
                getuid=lambda: uid,
                geteuid=lambda: uid,
                getgid=lambda: uid,
                getegid=lambda: uid,
            ),
            patch.object(gate.resource, "setrlimit", side_effect=lambda *_: calls.append("limit")),
            patch.object(gate.os, "setgroups", side_effect=lambda _: calls.append("groups")),
            patch.object(gate.os, "setgid", side_effect=lambda _: calls.append("gid")),
            patch.object(gate.os, "setuid", side_effect=lambda value: calls.append("uid")),
            patch.object(gate.os, "seteuid", side_effect=PermissionError),
            patch.object(
                gate, "require_exclusive_kernel_group", side_effect=lambda _: calls.append("kernel")
            ),
            self.assertRaisesRegex(gate.Refused, "saved privileged identity"),
        ):
            gate.drop_identity(uid, 64)
        self.assertEqual(calls[:6], ["limit", "limit", "groups", "gid", "uid", "kernel"])

    def test_invalidated_qualification_allows_only_authenticated_drain_request(self):
        operator = os.getuid()
        configuration = {"operator_uid": operator, "qualification": None}
        membership = object()
        with (
            patch.object(gate, "installed_operator_configuration", return_value=configuration),
            patch.object(gate, "installed_configuration", side_effect=gate.Refused("unqualified")),
            patch.object(gate, "DarwinMembership", return_value=membership),
            patch.object(gate, "recover") as recover,
            patch.object(gate.os, "set_blocking"),
        ):
            for arguments in (["uid_gate.py"], ["uid_gate.py", "serve"]):
                with patch.object(gate.sys, "argv", arguments), self.assertRaises(gate.Refused):
                    gate.main()
            recover.assert_not_called()
            with patch.object(gate.sys, "argv", ["uid_gate.py", "recover", str(gate.FIRST_UID)]):
                with (
                    patch.dict(os.environ, {"SUDO_UID": str(operator + 1)}),
                    self.assertRaisesRegex(gate.Refused, "authenticate"),
                ):
                    gate.main()
                recover.assert_not_called()
                with patch.dict(os.environ, {"SUDO_UID": str(operator)}):
                    gate.main()
            recover.assert_called_once_with(membership, gate.FIRST_UID)

    def test_qualified_launch_entry_rejects_stale_kernel_or_helper_binding(self):
        configuration = {
            "operator_uid": os.getuid(),
            "qualification": {
                "helper_sha256": "a" * 64,
                "kernel_release": "previous-kernel",
                "checks": sorted(gate.QUALIFICATION_CHECKS),
            },
        }
        with (
            patch.object(gate, "installed_operator_configuration", return_value=configuration),
            self.assertRaisesRegex(gate.Refused, "does not bind"),
        ):
            gate.installed_configuration()

    def test_preserved_predecessor_qualification_cannot_admit_repaired_helper(self):
        qualification = {
            "helper_sha256": "4694c1275c82fd8e9a859bc29a3ffa7e893eab7d34c950be83d9754ac2bcc06a",
            "kernel_release": os.uname().release,
            "checks": sorted(gate.QUALIFICATION_CHECKS),
        }
        configuration = {"operator_uid": os.getuid(), "qualification": qualification}
        before = json.dumps(configuration, sort_keys=True)
        with (
            patch.object(gate, "installed_operator_configuration", return_value=configuration),
            self.assertRaisesRegex(gate.Refused, "does not bind"),
        ):
            gate.installed_configuration()
        self.assertEqual(json.dumps(configuration, sort_keys=True), before)


class LeaseOwnership(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name)
        self.membership = Mock(empty=Mock(return_value=True))
        self.spec = gate.GateSpec.parse(declaration())
        self.leases = []

    def tearDown(self):
        for lease in self.leases:
            os.close(lease.lock_descriptor)
        self.temporary.cleanup()

    def allocate(self):
        with patch.object(gate.pwd, "getpwuid", side_effect=KeyError):
            lease = gate.Lease.allocate(self.state, self.membership, self.spec)
        self.leases.append(lease)
        return lease

    def test_live_owner_and_inherited_lock_exclude_recovery(self):
        lease = self.allocate()
        with self.assertRaisesRegex(gate.Refused, "live owner"):
            gate.Lease(lease.directory, lease.uid, lease.manifest)
        duplicate = os.dup(lease.lock_descriptor)
        os.close(lease.lock_descriptor)
        self.leases.remove(lease)
        try:
            with self.assertRaisesRegex(gate.Refused, "live owner"):
                gate.Lease(lease.directory, lease.uid, lease.manifest)
        finally:
            os.close(duplicate)
        recovered = gate.Lease(lease.directory, lease.uid, lease.manifest)
        self.leases.append(recovered)

    def test_receipt_requires_closed_authority_and_empty_uid(self):
        lease = self.allocate()
        with self.assertRaises(gate.Refused):
            lease.receipt(self.membership)
        lease.close()
        self.membership.empty.return_value = False
        with self.assertRaises(gate.Refused):
            lease.receipt(self.membership)
        self.membership.empty.return_value = True
        receipt = lease.receipt(self.membership)
        self.assertEqual(receipt["kind"], "cleaned")
        self.assertEqual(receipt["aggregate_memory"], "unqualified")
        with self.assertRaises(gate.Refused):
            lease.record_launch(gate.LaunchSpec.parse(launch(), self.spec))
        self.assertNotEqual(self.allocate().uid, lease.uid)

    def test_allocation_cursor_never_reuses_completed_or_interrupted_reservations(self):
        first, _ = gate.reserve_uid(self.state)
        second, _ = gate.reserve_uid(self.state)
        self.assertEqual((first, second), (gate.FIRST_UID, gate.FIRST_UID + 1))
        (self.state / ("uid-" + str(second + 1))).mkdir()
        third, _ = gate.reserve_uid(self.state)
        self.assertEqual(third, second + 2)
        self.assertEqual(
            json.loads((self.state / "allocation.json").read_text()), {"next_uid": third + 1}
        )

    def test_reservation_recovery_never_signals_unexpected_uid_members(self):
        reservation = self.allocate()
        with patch.object(gate, "sweep_uid") as sweep:
            self.membership.empty.return_value = False
            with self.assertRaisesRegex(gate.Refused, "unexpected execution or process"):
                gate.recover_reservation(reservation, self.membership)
            sweep.assert_not_called()
            self.membership.empty.return_value = True
            self.assertEqual(
                gate.recover_reservation(reservation, self.membership)["kind"], "cleaned"
            )

    def test_drain_stops_inflight_launcher_before_its_delayed_identity_transition(self):
        lease = self.allocate()
        marker = self.state / "transition-must-not-run"

        def delayed_drop(*_):
            time.sleep(1)
            marker.write_text("late transition")
            raise gate.Refused("test must never reach real identity transition")

        children = {}
        spec = gate.LaunchSpec.parse(launch(), self.spec)
        with patch.object(gate, "drop_identity", side_effect=delayed_drop):
            pid, out, error = gate.fork_launch(spec, lease.uid, 64, children, "delayed")
        try:
            receipt = gate.drain(lease, self.membership, set(children))
            self.assertEqual(receipt["kind"], "cleaned")
            with self.assertRaises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
            self.assertFalse(marker.exists())
        finally:
            os.close(out)
            os.close(error)

    def test_stopped_cleanup_signaler_cannot_block_its_root_owner(self):
        def stopped_drop(*_):
            os.kill(os.getpid(), gate.signal.SIGSTOP)
            raise gate.Refused("test must never reach actual cleanup signal")

        started = time.monotonic()
        with patch.object(gate, "drop_identity", side_effect=stopped_drop):
            gate.sweep_uid(gate.FIRST_UID)
        self.assertLess(time.monotonic() - started, 2)

    def make_crashed_gate(self):
        owner, child = self.allocate(), self.allocate()
        owner.manifest.update({"kind": "gate", "child_uids": [child.uid]})
        child.manifest.update({"kind": "launch", "gate_uid": owner.uid})
        owner.write()
        child.write()
        for lease in (owner, child):
            os.close(lease.lock_descriptor)
            self.leases.remove(lease)
        return owner, child

    def test_gate_recovery_drains_every_bound_child_before_aggregate_receipt(self):
        owner, child = self.make_crashed_gate()
        observed = []
        self.membership.empty.side_effect = lambda uid: observed.append(uid) or True
        with (
            patch.object(gate, "root_owned"),
            patch.object(gate.pwd, "getpwuid", side_effect=KeyError),
        ):
            result = gate.recover_owned(self.state, self.membership, owner.uid)
        self.assertEqual(result["kind"], "gate_closed")
        self.assertEqual(result["launch_receipts"][0]["uid"], child.uid)
        self.assertLess(observed.index(child.uid), observed.index(owner.uid))

    def test_recovery_reads_child_set_only_after_acquiring_dead_owner_lock(self):
        owner, child = self.make_crashed_gate()
        original = gate.Lease
        owner.manifest["child_uids"] = []
        owner.write()

        def acquire(directory, uid, manifest):
            locked = original(directory, uid, manifest)
            if uid == owner.uid:
                owner.manifest["child_uids"] = [child.uid]
                owner.write()
            return locked

        with (
            patch.object(gate, "root_owned"),
            patch.object(gate.pwd, "getpwuid", side_effect=KeyError),
            patch.object(gate, "Lease", side_effect=acquire),
        ):
            result = gate.recover_owned(self.state, self.membership, owner.uid)
        self.assertEqual([item["uid"] for item in result["launch_receipts"]], [child.uid])

    def test_missing_or_foreign_child_never_credits_gate_cleanup(self):
        owner, child = self.make_crashed_gate()
        child.manifest["gate_uid"] = owner.uid + 100
        child.write()
        with (
            patch.object(gate, "root_owned"),
            patch.object(gate.pwd, "getpwuid", side_effect=KeyError),
        ):
            with self.assertRaisesRegex(gate.Refused, "quarantined"):
                gate.recover_owned(self.state, self.membership, owner.uid)
            child.path.unlink()
            with self.assertRaisesRegex(gate.Refused, "quarantined"):
                gate.recover_owned(self.state, self.membership, owner.uid)
        self.assertEqual(json.loads(owner.path.read_text())["status"], "quarantined")

    @unittest.skipIf(os.geteuid() == 0, "this is specifically an unprivileged launch refusal test")
    def test_actual_fork_drop_failure_reaped_without_executing_command(self):
        lease = self.allocate()
        marker = self.state / "must-not-exist"
        spec = gate.LaunchSpec.parse(launch(argv=["/usr/bin/touch", str(marker)]), self.spec)
        children = {}
        pid, out, error = gate.fork_launch(spec, lease.uid, 64, children, "launch-proof")
        self.assertEqual(children, {pid: "launch-proof"})
        try:
            self.assertEqual(os.read(out, 4096), b"")
            failure = os.read(error, 4096)
            self.assertIn(b"launch failed", failure)
            self.assertEqual(json.loads(failure.split(b": ", 1)[1])["phase"], "identity")
            receipt = gate.drain(lease, self.membership, set(children))
            self.assertEqual(receipt["kind"], "cleaned")
            with self.assertRaises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
        finally:
            os.close(out)
            os.close(error)
        self.assertFalse(marker.exists())


@unittest.skipUnless(sys.platform == "darwin", "macOS native read-only query")
class NativeReadOnly(unittest.TestCase):
    def test_empty_uid_kernel_query(self):
        self.assertIsInstance(gate.DarwinMembership().empty(gate.LAST_UID), bool)

    def test_anchored_directory_refuses_symlink_and_parent_traversal(self):
        with tempfile.TemporaryDirectory() as raw:
            anchor = Path(raw)
            (anchor / "real").mkdir()
            (anchor / "alias").symlink_to(anchor / "real", target_is_directory=True)
            fd = gate.anchored_directory(anchor, "real", {os.getuid()})
            os.close(fd)
            for value in ("../real", "/real", "real/..", "alias"):
                with self.subTest(value=value), self.assertRaises((gate.Refused, OSError)):
                    gate.anchored_directory(anchor, value, {os.getuid()})

    def test_inherited_numeric_gid_acl_survives_standard_tempfile_modes(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw)
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                gate.DarwinACL().initialize(fd, gate.LAST_UID, os.getuid())
            finally:
                os.close(fd)
            child = path / "child"
            child.mkdir(mode=0o700)
            file = child / "file"
            file.write_text("fixture")
            file.chmod(0o600)
            child.chmod(0o700)
            report = subprocess.run(
                ["/bin/ls", "-lde", str(child), str(file)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertGreaterEqual(report.count("inherited allow"), 2)
            self.assertNotIn("writesecurity", report)

    def test_closes_inherited_descriptor_above_lowered_soft_limit(self):
        descriptor = os.open("/dev/null", os.O_RDONLY)
        high = fcntl.fcntl(descriptor, fcntl.F_DUPFD_CLOEXEC, 512)
        os.close(descriptor)
        script = """import importlib.util,os,resource,sys
spec=importlib.util.spec_from_file_location("uid_gate",sys.argv[1])
module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
resource.setrlimit(resource.RLIMIT_NOFILE,(64,resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
module.close_descriptors(3)
try:os.fstat(int(sys.argv[2]))
except OSError:print("closed")
else:raise SystemExit("high descriptor leaked")
"""
        try:
            result = subprocess.run(
                ["/usr/bin/python3", "-I", "-S", "-c", script, str(SOURCE), str(high)],
                pass_fds=(high,),
                capture_output=True,
                text=True,
                timeout=10,
            )
        finally:
            os.close(high)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "closed")

    @unittest.skipIf(os.geteuid() == 0, "must not invoke privileged entrypoint during these checks")
    def test_system_interpreter_rejects_unprivileged_entrypoint(self):
        result = subprocess.run(
            ["/usr/bin/python3", "-I", "-S", str(SOURCE)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 125)
        self.assertIn("authenticated macOS root", result.stderr)


if __name__ == "__main__":
    unittest.main()
