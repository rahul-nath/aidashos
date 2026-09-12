"""Failure-path checks for the fixed qualifier; native proof remains mandatory."""

from __future__ import annotations

import base64
import contextlib
import errno
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SOURCE = Path(__file__).with_name("qualify_native.py")
SPEC = importlib.util.spec_from_file_location("native_qualification", SOURCE)
assert SPEC is not None and SPEC.loader is not None
qualification = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = qualification
SPEC.loader.exec_module(qualification)


class ProcessOwnership(unittest.TestCase):
    def test_reaped_child_is_never_signaled_again(self):
        pid = os.fork()
        if pid == 0:
            os._exit(7)
        child = qualification.OwnedChild(pid)
        self.assertEqual(child.wait(), 7)
        with patch.object(qualification.os, "kill") as kill:
            child.signal(signal.SIGKILL)
            self.assertEqual(child.wait(), 7)
        kill.assert_not_called()

    def test_timeout_reaps_child_and_closes_signal_authority(self):
        pid = os.fork()
        if pid == 0:
            time.sleep(60)
            os._exit(0)
        child = qualification.OwnedChild(pid)
        with self.assertRaisesRegex(RuntimeError, "deadline"):
            child.wait(seconds=0)
        self.assertEqual(child.status, -signal.SIGKILL)
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        with patch.object(qualification.os, "kill") as kill:
            child.signal(signal.SIGKILL)
        kill.assert_not_called()

    def test_group_signal_is_scoped_to_owned_session_leader(self):
        child = qualification.OwnedChild(123)
        with (
            patch.object(qualification.os, "getpgid", return_value=12),
            patch.object(qualification.os, "kill") as kill,
            patch.object(qualification.os, "killpg") as killpg,
        ):
            child.signal(signal.SIGKILL, group=True)
        kill.assert_called_once_with(123, signal.SIGKILL)
        killpg.assert_not_called()


class JobCreationProof(unittest.TestCase):
    label = "com.aidashos.qualify." + "a" * 24
    uid = 55001

    def report(self, created=False):
        absent = {"kind": "errno", "errno": errno.ESRCH}
        success = {"kind": "errno", "errno": 0}
        return {
            "label": self.label,
            "uid": self.uid,
            "euid": self.uid,
            "errors": [],
            "steps": {
                "before": absent,
                "final": absent,
                "submit": dict(success) if created else {"kind": "errno", "errno": errno.EPERM},
                "after": {"kind": "job"} if created else absent,
                "remove": dict(success) if created else absent,
            },
        }

    def evidence(self, report, code=0):
        return qualification.captured_process(
            {"kind": "exited", "code": code}, json.dumps(report).encode(), b""
        )

    def test_only_native_permission_errno_with_bound_identity_and_absence_is_denied(self):
        variants = {
            "created": (self.report(True), 0, qualification.JobSubmission.CREATED),
            "removal_in_progress": (self.report(True), 0, qualification.JobSubmission.CREATED),
            "eperm": (self.report(), 0, qualification.JobSubmission.PERMISSION_DENIED),
            "eacces": (self.report(), 0, qualification.JobSubmission.PERMISSION_DENIED),
            **{
                name: (self.report(), 0, qualification.JobSubmission.OTHER_FAILURE)
                for name in (
                    "other_errno",
                    "bool_errno",
                    "transport_errno",
                    "missing_query",
                    "job_present",
                    "cleanup_failed",
                    "different_label",
                    "different_uid",
                    "canary_error",
                    "cli_nonzero",
                )
            },
        }
        variants["eacces"][0]["steps"]["submit"]["errno"] = errno.EACCES
        variants["removal_in_progress"][0]["steps"]["remove"]["errno"] = errno.EINPROGRESS
        variants["other_errno"][0]["steps"]["submit"]["errno"] = errno.EINVAL
        variants["bool_errno"][0]["steps"]["submit"]["errno"] = True
        variants["transport_errno"][0]["steps"]["submit"] = {
            "kind": "transport_error",
            "errno": errno.EPERM,
        }
        del variants["missing_query"][0]["steps"]["after"]
        variants["job_present"][0]["steps"]["after"] = {"kind": "job"}
        variants["cleanup_failed"][0]["steps"]["final"] = {"kind": "job"}
        variants["different_label"][0]["label"] = "another-label"
        variants["different_uid"][0]["euid"] = self.uid + 1
        variants["canary_error"][0]["errors"] = [{"phase": "remove", "detail": "failed"}]
        variants["cli_nonzero"] = (self.report(), 1, qualification.JobSubmission.OTHER_FAILURE)
        for name, (report, code, expected) in variants.items():
            with self.subTest(name=name):
                evidence = self.evidence(report, code)
                evidence["stderr_base64"] = base64.b64encode(b"Operation not permitted").decode()
                self.assertIs(
                    qualification.job_submission(evidence, self.label, self.uid), expected
                )
        for output in (b"", b"not-json", b"null", b"[]"):
            with self.subTest(output=output):
                evidence = qualification.captured_process(
                    {"kind": "exited", "code": 0}, output, b""
                )
                self.assertIs(
                    qualification.job_submission(evidence, self.label, self.uid),
                    qualification.JobSubmission.OTHER_FAILURE,
                )

    def test_failed_native_calls_keep_original_and_cleanup_errors_and_still_query_absence(self):
        absent = {"kind": "errno", "errno": errno.ESRCH}
        with (
            patch.object(
                qualification,
                "launchd_request",
                side_effect=[
                    absent,
                    RuntimeError("submit fault"),
                    RuntimeError("cleanup fault"),
                    absent,
                ],
            ) as request,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            qualification.native_job_canary(self.label)
        observed = json.loads(output.getvalue())
        self.assertEqual(
            [error["detail"] for error in observed["errors"]], ["submit fault", "cleanup fault"]
        )
        self.assertEqual(request.call_args_list[-1].args, ("GetJob", self.label))
        self.assertEqual(observed["steps"]["final"], absent)
        with (
            patch.object(qualification, "launchd_request", return_value={"kind": "job"}) as request,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            qualification.native_job_canary(self.label)
        request.assert_called_once_with("GetJob", self.label)

    def test_interrupted_gate_observation_retains_exact_process_bytes(self):
        baseline = self.report(True)
        baseline["uid"] = baseline["euid"] = 501
        baseline["label"] = self.label
        gate = SimpleNamespace(
            operator=501,
            prepare=Mock(return_value={"uid": self.uid, "handle": "child"}),
            launch=Mock(return_value="child"),
            launches={"child": {"pid": 123}},
            receive=Mock(side_effect=RuntimeError("protocol interrupted")),
            terminals={},
            outputs={"child": {"stdout": b"partial\xff", "stderr": b"native detail\x00"}},
        )
        evidence = {}
        with (
            patch.object(qualification.os, "urandom", return_value=bytes.fromhex("a" * 24)),
            patch.object(qualification, "as_operator", return_value=self.evidence(baseline)),
            self.assertRaisesRegex(RuntimeError, "protocol interrupted"),
        ):
            qualification.closed_job_probe(gate, "fixed profile", evidence)
        process = evidence["contained"]["process"]
        self.assertEqual(process["status"], {"kind": "incomplete"})
        self.assertEqual(base64.b64decode(process["stdout_base64"]), b"partial\xff")
        self.assertEqual(base64.b64decode(process["stderr_base64"]), b"native detail\x00")
        self.assertEqual(evidence["baseline"]["process"], self.evidence(baseline))
        self.assertEqual(evidence["baseline"]["label"], self.label)
        self.assertEqual(evidence["baseline"]["uid"], 501)
        for phase in ("baseline", "launch"):
            with self.subTest(phase=phase):
                evidence = {}
                operator = Mock(return_value=self.evidence(baseline))
                if phase == "baseline":
                    operator.side_effect = RuntimeError("no operator response")
                else:
                    gate.launch.side_effect = RuntimeError("no launch response")
                with (
                    patch.object(qualification.os, "urandom", return_value=bytes.fromhex("a" * 24)),
                    patch.object(qualification, "as_operator", operator),
                    self.assertRaises(RuntimeError),
                ):
                    qualification.closed_job_probe(gate, "fixed profile", evidence)
                self.assertEqual(evidence["baseline"]["label"], self.label)
                self.assertEqual(evidence["baseline"]["uid"], 501)
                if phase == "launch":
                    self.assertEqual(evidence["contained"]["label"], self.label)
                    self.assertEqual(evidence["contained"]["prepared"]["uid"], self.uid)

    @unittest.skipUnless(
        sys.platform == "darwin" and os.geteuid() != 0, "ordinary-user Darwin control"
    )
    def test_real_native_submit_permission_and_absence_with_unchanged_identity_rules(self):
        import uid_gate

        positive_label = "com.aidashos.qualify." + os.urandom(12).hex()
        positive = qualification.operator_job_probe(positive_label)
        self.assertIs(
            qualification.job_submission(positive, positive_label, os.getuid()),
            qualification.JobSubmission.CREATED,
            positive,
        )
        label = "com.aidashos.qualify." + os.urandom(12).hex()
        profile = "(version 1)(allow default)(deny network*)" + uid_gate.IDENTITY_RULES
        result = subprocess.run(
            ["/usr/bin/sandbox-exec", "-p", profile, *qualification.job_probe_argv(label)],
            env=qualification.environment(),
            capture_output=True,
            timeout=15,
        )
        denied = qualification.captured_process(
            {"kind": "exited", "code": result.returncode}, result.stdout, result.stderr
        )
        self.assertIs(
            qualification.job_submission(denied, label, os.getuid()),
            qualification.JobSubmission.PERMISSION_DENIED,
            denied,
        )
        self.assertEqual(
            qualification.launchd_request("GetJob", positive_label),
            {"kind": "errno", "errno": errno.ESRCH},
        )
        self.assertEqual(
            qualification.launchd_request("GetJob", label), {"kind": "errno", "errno": errno.ESRCH}
        )

    @unittest.skipUnless(
        sys.platform == "darwin" and os.geteuid() != 0, "ordinary-user Darwin control"
    )
    def test_real_timeout_enters_cleanup_but_cannot_certify_a_submission(self):
        label = "com.aidashos.qualify." + os.urandom(12).hex()
        script = """
import runpy,signal,sys,time
module=runpy.run_path(sys.argv[1],run_name='job_probe')
signal.signal(signal.SIGTERM,module['interrupt_job_probe'])
canary=module['native_job_canary']
request=canary.__globals__['launchd_request']
def delayed(operation,label):
    response=request(operation,label)
    if operation=='SubmitJob' and response=={'kind':'errno','errno':0}:
        print('created before timeout',file=sys.stderr,flush=True)
        time.sleep(60)
    return response
canary.__globals__['launchd_request']=delayed
canary(sys.argv[2])
"""
        argv = [sys.executable, "-I", "-S", "-c", script, str(SOURCE), label]
        original_popen = subprocess.Popen

        def shorter_first_deadline(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            communicate = child.communicate
            child.communicate = lambda input=None, timeout=None: communicate(
                input=input, timeout=1 if timeout == 10 else timeout
            )
            return child

        with (
            patch.object(qualification, "job_probe_argv", return_value=argv),
            patch.object(qualification.subprocess, "Popen", side_effect=shorter_first_deadline),
        ):
            evidence = qualification.operator_job_probe(label)
        self.assertEqual(evidence["status"], {"kind": "timeout", "code": 0, "cleanup": "unproven"})
        self.assertIn(b"created before timeout", base64.b64decode(evidence["stderr_base64"]))
        observed = json.loads(base64.b64decode(evidence["stdout_base64"]))
        self.assertEqual(observed["errors"][0]["type"], "InterruptedError")
        self.assertEqual(observed["steps"]["final"], {"kind": "errno", "errno": errno.ESRCH})
        self.assertIs(
            qualification.job_submission(evidence, label, os.getuid()),
            qualification.JobSubmission.OTHER_FAILURE,
        )
        self.assertEqual(
            qualification.launchd_request("GetJob", label), {"kind": "errno", "errno": errno.ESRCH}
        )


class QualificationFailures(unittest.TestCase):
    def test_signature_failure_retains_detail_and_keeps_fixtures_private_and_inert(self):
        with tempfile.TemporaryDirectory() as temporary:
            helper = SimpleNamespace(STATE=Path(temporary), root_owned=Mock())
            calls = []

            def signing(argv, **options):
                calls.append((argv, options))
                fails = len(calls) == 5
                return subprocess.CompletedProcess(
                    argv, 1 if fails else 0, "", "signing canary" if fails else ""
                )

            with (
                patch.object(qualification.subprocess, "run", side_effect=signing),
                self.assertRaisesRegex(RuntimeError, "signature sign failed: signing canary"),
            ):
                qualification.fixed_files(helper, os.getuid())
            (directory,) = Path(temporary).iterdir()
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            for path in directory.iterdir():
                self.assertEqual(path.stat().st_mode & 0o6000, 0)
            for argv, options in calls:
                self.assertEqual(argv[0], "/usr/bin/codesign")
                self.assertEqual(
                    options["env"],
                    {
                        "PATH": "/usr/bin:/bin",
                        "CODESIGN_ALLOCATE": (
                            "/Library/Developer/CommandLineTools/usr/bin/codesign_allocate"
                        ),
                    },
                )
                self.assertEqual(options["cwd"], directory)
                self.assertEqual(options["stdin"], subprocess.DEVNULL)

    def test_fixture_directory_is_traversable_despite_private_umask(self):
        with tempfile.TemporaryDirectory() as temporary:
            helper = SimpleNamespace(STATE=Path(temporary), root_owned=Mock())
            previous = os.umask(0o077)
            try:
                directory, forbidden = qualification.fixed_files(helper, os.getuid())
            finally:
                os.umask(previous)
            try:
                self.assertEqual(directory.stat().st_mode & 0o777, 0o755)
                for name, mode in (
                    ("ordinary-id", 0o755),
                    ("setuid-id", 0o4755),
                    ("setgid-id", 0o2755),
                ):
                    self.assertEqual((directory / name).stat().st_mode & 0o7777, mode)
                self.assertEqual(forbidden.stat().st_mode & 0o777, 0o666)
                result = subprocess.run(
                    [str(directory / "ordinary-id"), "-u"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                self.assertEqual(int(result.stdout.strip()), os.geteuid())
            finally:
                qualification.clear_fixture_privileges(directory)

    @unittest.skipUnless(
        sys.platform == "darwin" and os.geteuid() != 0, "ordinary-user Darwin control"
    )
    def test_actual_fixture_setid_denials_follow_a_successful_ordinary_baseline(self):
        import uid_gate

        with tempfile.TemporaryDirectory() as temporary:
            helper = SimpleNamespace(STATE=Path(temporary), root_owned=Mock())
            previous = os.umask(0o077)
            try:
                directory, forbidden = qualification.fixed_files(helper, os.getuid())
            finally:
                os.umask(previous)
            profile = (
                "(version 1)(allow default)(deny network*)"
                "(deny file-read* file-write* (literal "
                + json.dumps(str(forbidden))
                + "))"
                + uid_gate.IDENTITY_RULES
            )
            script = (
                "import importlib.util,json,sys; from pathlib import Path; "
                'spec=importlib.util.spec_from_file_location("canaries",sys.argv[1]); '
                "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
                "print(json.dumps(module.set_id_exec(Path(sys.argv[2]))))"
            )
            try:
                result = subprocess.run(
                    [
                        "/usr/bin/sandbox-exec",
                        "-p",
                        profile,
                        sys.executable,
                        "-I",
                        "-S",
                        "-c",
                        script,
                        str(SOURCE.with_name("qualification_canaries.py")),
                        str(directory),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(result.stdout),
                    {
                        "setuid-id:exec": 1,
                        "setuid-id:spawn": 1,
                        "setgid-id:exec": 1,
                        "setgid-id:spawn": 1,
                    },
                )
            finally:
                qualification.clear_fixture_privileges(directory)

    def test_normal_disconnect_cannot_use_recovery_to_hide_a_leaked_uid(self):
        gate = object.__new__(qualification.Gate)
        gate.process = Mock()
        gate.process.wait.return_value = 0
        gate.socket = Mock()
        gate.prepared = {"child": {"uid": 55001}}
        membership = Mock()
        membership.empty.return_value = False
        gate.helper = SimpleNamespace(DarwinMembership=lambda: membership, recover_owned=Mock())
        with self.assertRaisesRegex(RuntimeError, "before recovery"):
            gate.interrupt(False)
        gate.helper.recover_owned.assert_not_called()

    def test_failed_probe_and_cleanup_keep_original_failure_and_clear_fixture_modes(self):
        original = RuntimeError("native probe failed")
        gate = Mock(closed=False)
        gate.close.side_effect = RuntimeError("normal close failed")
        gate.interrupt.side_effect = RuntimeError("recovery failed")
        helper = SimpleNamespace(persist=Mock())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            code = root / "installed.py"
            code.write_text("fixed installed code")
            with (
                patch.object(qualification, "HELPER", code),
                patch.object(qualification, "CANARIES", code),
                patch.object(qualification, "fixed_files", return_value=(root, root / "forbidden")),
                patch.object(qualification, "Gate", return_value=gate),
                patch.object(qualification, "stage", side_effect=original),
                patch.object(qualification, "clear_fixture_privileges") as clear,
                patch.object(qualification.sys, "stderr"),
                self.assertRaises(RuntimeError) as caught,
            ):
                qualification.qualify(helper, 501, root, code)
        self.assertIs(caught.exception, original)
        gate.interrupt.assert_called_once_with(True)
        clear.assert_called_once_with(root)
        self.assertFalse(helper.persist.call_args.args[1]["qualified"])

    def test_partial_fixture_cleanup_removes_both_privilege_bits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            item = root / "setuid-id"
            item.write_bytes(b"inert fixture")
            item.chmod(0o6755)
            qualification.clear_fixture_privileges(root)
            self.assertEqual(item.stat().st_mode & 0o7777, 0o755)

    def test_child_prepare_reuses_parent_declared_scratch(self):
        gate = object.__new__(qualification.Gate)
        gate.opened = {"staging": "/fixed/g1"}
        gate.prepared = {"parent": {"scratch": "/fixed/g1/scratch/l1"}}
        gate.send = Mock()
        gate.until = Mock(return_value={"handle": "child", "scratch": "/fixed/g1/scratch/l1"})
        gate.prepare("parent")
        gate.send.assert_called_once_with(
            {"kind": "prepare", "parent_handle": "parent", "scratch": "scratch/l1"}
        )

    def test_child_prepare_refuses_scratch_outside_owned_staging(self):
        gate = object.__new__(qualification.Gate)
        gate.opened = {"staging": "/fixed/g1"}
        gate.prepared = {"parent": {"scratch": "/somewhere/else"}}
        gate.send = Mock()
        with self.assertRaises(ValueError):
            gate.prepare("parent")
        gate.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
