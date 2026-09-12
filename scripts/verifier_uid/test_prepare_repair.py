"""Exercise the fixed repair guards with every privileged write captured."""

import ast
import base64
import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import prepare_repair


def definitions():
    tree = ast.parse(prepare_repair.BOOTSTRAP)
    tree.body = [node for node in tree.body if not isinstance(node, ast.If)]
    namespace: dict[str, Any] = {"__name__": "repair_review"}
    exec(compile(tree, "fixed-repair-bootstrap", "exec"), namespace)
    return namespace


class RepairTests(unittest.TestCase):
    def test_configuration_accepts_only_exact_predecessor_qualification(self):
        namespace = definitions()
        contract = types.SimpleNamespace(QUALIFICATION_CHECKS={"first", "second"})
        qualification = {
            "helper_sha256": prepare_repair.HELPER_PLAN["predecessor"],
            "kernel_release": os.uname().release,
            "checks": ["first", "second"],
        }
        config = {"operator_uid": 501, "qualification": qualification}
        namespace["validate_configuration"](config, 501, contract)
        for change in (
            {"helper_sha256": "0" * 64},
            {"kernel_release": "stale"},
            {"checks": []},
            {"checks": [[]]},
            {"extra": True},
        ):
            with self.subTest(change=change), self.assertRaises(SystemExit):
                namespace["validate_configuration"](
                    {"operator_uid": 501, "qualification": {**qualification, **change}},
                    501,
                    contract,
                )
        for invalid in (None, {}, [], True):
            with self.subTest(invalid=invalid), self.assertRaises(SystemExit):
                namespace["validate_configuration"](
                    {"operator_uid": 501, "qualification": invalid},
                    501,
                    contract,
                )

    def test_closed_reservations_require_bound_cleanup_and_kernel_zero(self):
        namespace = definitions()
        membership = types.SimpleNamespace(empty=Mock(return_value=True))
        contract = types.SimpleNamespace(
            FIRST_UID=55000,
            LAST_UID=1048575,
            SCHEMA="aidashos.uid-gate.v1",
            MAX_MANIFEST_BYTES=8388608,
            DarwinMembership=lambda: membership,
        )
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            namespace["STATE"] = state
            namespace["protected"] = Mock()
            directory = state / "uid-55001"
            directory.mkdir()
            lease = directory / "lease.json"
            manifest = {"uid": 55001, "schema": contract.SCHEMA, "status": "cleaned"}
            for change in ({}, {"uid": 55002}, {"schema": "unknown"}, {"status": "quarantined"}):
                lease.write_text(json.dumps({**manifest, **change}))
                if not change:
                    namespace["require_closed_reservations"](contract)
                    membership.empty.assert_called_once_with(55001)
                else:
                    with self.subTest(change=change), self.assertRaises(SystemExit):
                        namespace["require_closed_reservations"](contract)
            lease.write_text(json.dumps(manifest))
            for result in (False, OSError("kernel query unavailable")):
                membership.empty = Mock(
                    return_value=result,
                    side_effect=result if isinstance(result, OSError) else None,
                )
                with self.subTest(result=result), self.assertRaises((SystemExit, OSError)):
                    namespace["require_closed_reservations"](contract)
            self.assertEqual(json.loads(lease.read_text()), manifest)

    def test_cleanup_contract_rejects_unpinned_code_before_evaluating_it(self):
        namespace = definitions()
        with self.assertRaises(SystemExit):
            namespace["predecessor_contract"](b"raise AssertionError('must not execute')")

    def fixture(self):
        namespace = definitions()
        original, replacement = b"reviewed predecessor", b"reviewed repaired helper"
        plan = namespace["HELPER_PLAN"]
        plan["predecessor"] = hashlib.sha256(original).hexdigest()
        namespace["TARGET"] = namespace["HELPER"].with_name(plan["installed"])
        namespace["BACKUP"] = namespace["TARGET"].with_name(
            namespace["TARGET"].name + ".previous-" + plan["predecessor"]
        )
        python, prefix = Path(sys.executable).resolve(), Path(sys.prefix).resolve()
        payload = {
            "schema": 2,
            "component": "helper",
            "operator_uid": 501,
            "python": {"executable": str(python), "prefix": str(prefix)},
            "predecessor_sha256": plan["predecessor"],
            "replacement_sha256": hashlib.sha256(replacement).hexdigest(),
            "replacement": base64.b64encode(replacement).decode(),
        }
        qualifier = b"unchanged installed qualifier"
        plan["frozen"] = {
            "qualify_native.py": ["fixed-qualifier.py", hashlib.sha256(qualifier).hexdigest()],
            "canary.py": ["fixed-canary.py", hashlib.sha256(b"canary").hexdigest()],
        }
        files = {
            namespace["TARGET"]: original,
            namespace["HELPER"].with_name("fixed-qualifier.py"): qualifier,
            namespace["CONFIG"]: json.dumps(
                {
                    "operator_uid": 501,
                    "qualification": {
                        "helper_sha256": plan["predecessor"],
                        "kernel_release": "test-kernel",
                        "checks": ["fixed-check"],
                    },
                }
            ).encode(),
            namespace["HELPER"].with_name("fixed-canary.py"): b"canary",
            namespace["STATE"] / "allocation.lock": b"allocator state",
            namespace["STATE"] / "uid-55000" / "manifest.json": b"retained lease",
            namespace["STATE"] / "work" / "staged.py": b"retained staging",
        }
        packet = Path("/reviewed/repair.json")
        events = []
        namespace["os"] = types.SimpleNamespace(
            geteuid=lambda: 0,
            uname=lambda: types.SimpleNamespace(release="test-kernel"),
            environ={"SUDO_UID": "501"},
            close=lambda descriptor: events.append(("close", descriptor)),
        )
        namespace["sys"] = types.SimpleNamespace(
            platform="darwin",
            executable=str(python),
            prefix=str(prefix),
            flags=types.SimpleNamespace(isolated=1, no_site=1),
        )
        namespace["predecessor_contract"] = Mock(
            return_value=types.SimpleNamespace(
                QUALIFICATION_CHECKS={"fixed-check"},
            )
        )
        namespace["require_closed_reservations"] = Mock()
        namespace["protected"] = Mock()
        namespace["service_inactive"] = Mock()
        namespace["no_installed_processes"] = Mock()
        namespace["lock_existing_owners"] = Mock(return_value=[100, 101])
        namespace["read_file"] = lambda path, privileged=True: files[path]

        def backup(target, content):
            self.assertEqual(target, namespace["BACKUP"])
            events.append(("backup", content))
            files[target] = content

        def replace(target, content):
            self.assertEqual(target, namespace["TARGET"])
            events.append(("replace", content))
            files[target] = content

        namespace["ensure_backup"] = backup
        namespace["replace_component"] = replace
        namespace["sync_directory"] = lambda path: events.append(("sync", path))

        def run():
            raw = json.dumps(payload).encode()
            files[packet] = raw
            with patch("builtins.print"):
                namespace["run"](packet, hashlib.sha256(raw).hexdigest())

        return namespace, payload, files, events, run

    def test_exact_repair_preserves_state_and_repeat_is_idempotent(self):
        namespace, payload, files, events, run = self.fixture()
        state = dict(files)
        run()
        self.assertEqual(files[namespace["TARGET"]], base64.b64decode(payload["replacement"]))
        self.assertEqual(files[namespace["BACKUP"]], state[namespace["TARGET"]])
        self.assertEqual([event[0] for event in events], ["backup", "replace", "close", "close"])
        for path, content in state.items():
            if path != namespace["TARGET"]:
                self.assertEqual(files[path], content)
        namespace["no_installed_processes"].assert_called()
        self.assertEqual(namespace["no_installed_processes"].call_count, 2)
        events.clear()
        run()
        self.assertEqual(
            events,
            [("sync", namespace["TARGET"].parent), ("close", 101), ("close", 100)],
        )

    def test_guard_failures_never_write_privileged_files(self):
        changes = {
            "sudo operator": lambda ns, p, f: ns["os"].environ.clear(),
            "runtime": lambda ns, p, f: p.update(
                python={"executable": "/wrong", "prefix": "/wrong"}
            ),
            "predecessor": lambda ns, p, f: f.update({ns["TARGET"]: b"unreviewed"}),
            "frozen qualifier": lambda ns, p, f: f.update(
                {ns["HELPER"].with_name("fixed-qualifier.py"): b"unreviewed qualifier"}
            ),
            "malformed qualification": lambda ns, p, f: f.update(
                {ns["CONFIG"]: b'{"operator_uid":501,"qualification":{}}'}
            ),
            "config operator": lambda ns, p, f: f.update(
                {ns["CONFIG"]: b'{"operator_uid":502,"qualification":null}'}
            ),
            "frozen canary": lambda ns, p, f: f.update(
                {ns["HELPER"].with_name("fixed-canary.py"): b"changed"}
            ),
            "active service": lambda ns, p, f: ns.update(
                service_inactive=Mock(side_effect=SystemExit("active service"))
            ),
            "unclean reservation": lambda ns, p, f: ns.update(
                require_closed_reservations=Mock(side_effect=SystemExit("unclean UID"))
            ),
            "active owner": lambda ns, p, f: ns.update(
                lock_existing_owners=Mock(side_effect=BlockingIOError("live owner"))
            ),
            "running qualifier": lambda ns, p, f: ns.update(
                no_installed_processes=Mock(side_effect=SystemExit("running qualifier"))
            ),
            "writable ancestry": lambda ns, p, f: ns.update(
                protected=Mock(side_effect=SystemExit("writable ancestor"))
            ),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                namespace, payload, files, events, run = self.fixture()
                change(namespace, payload, files)
                before = dict(files)
                with self.assertRaises((SystemExit, BlockingIOError)):
                    run()
                for path, content in before.items():
                    self.assertEqual(files[path], content)
                self.assertFalse(any(event[0] in ("backup", "replace") for event in events))

    def test_retry_completes_sync_after_interrupted_post_rename_boundary(self):
        namespace, _, _, _, run = self.fixture()
        replace = namespace["replace_component"]

        def interrupted(target, content):
            replace(target, content)
            raise OSError("interrupted after rename before directory sync")

        namespace["replace_component"] = interrupted
        with self.assertRaisesRegex(OSError, "interrupted after rename"):
            run()
        namespace["replace_component"] = Mock(side_effect=AssertionError("must not replace again"))
        namespace["sync_directory"] = Mock()
        run()
        namespace["sync_directory"].assert_called_once_with(namespace["TARGET"].parent)
        namespace["replace_component"].assert_not_called()

    def test_late_owner_refusal_keeps_original_and_protected_backup(self):
        namespace, _, files, events, run = self.fixture()
        original = files[namespace["TARGET"]]
        namespace["no_installed_processes"].side_effect = [None, SystemExit("new owner")]
        with self.assertRaisesRegex(SystemExit, "new owner"):
            run()
        self.assertEqual(files[namespace["TARGET"]], original)
        self.assertEqual(files[namespace["BACKUP"]], original)
        self.assertNotIn("replace", [event[0] for event in events])

    def test_exact_repaired_qualifier_requires_original_backup(self):
        namespace, payload, files, events, run = self.fixture()
        files[namespace["TARGET"]] = base64.b64decode(payload["replacement"])
        files[namespace["BACKUP"]] = b"not the predecessor"
        with self.assertRaisesRegex(SystemExit, "predecessor backup"):
            run()
        self.assertFalse(any(event[0] in ("backup", "replace") for event in events))

    def test_script_argument_detection_preserves_real_boundaries(self):
        namespace = definitions()
        detect = namespace["installed_script"]
        helper = str(namespace["HELPER"])
        qualifier = "/Library/PrivilegedHelperTools/com.aidashos.verifier-uid-qualify.py"
        for arguments in (
            ["/fixed/python3.9", "-I", "-S", helper, "recover", "55000"],
            ["/fixed/python3.9", "-IS", qualifier],
            ["python3", "-W", "ignore", "--", qualifier],
        ):
            self.assertIn(detect(arguments), (Path(helper).name, Path(qualifier).name))
        for arguments in (
            ["sudo", "/fixed/python3.9", "-I", "-S", qualifier],
            ["python3", "-I", "-S", "-c", "print('" + qualifier + "')\n"],
            ["python3", "-m", "module", qualifier],
            ["/bin/sh", "-c", qualifier],
            ["python3", "other.py", qualifier],
        ):
            self.assertIsNone(detect(arguments))

    def test_service_probe_requires_precise_absence(self):
        namespace = definitions()
        absent = (
            'Bad request.\nCould not find service "com.aidashos.verifier-uid" '
            "in domain for system\n"
        )
        for code, stderr, accepted in ((113, absent, True), (0, "", False), (1, "denied", False)):
            with self.subTest(code=code):
                namespace["subprocess"] = types.SimpleNamespace(
                    run=Mock(return_value=types.SimpleNamespace(returncode=code, stderr=stderr))
                )
                if accepted:
                    namespace["service_inactive"]()
                else:
                    with self.assertRaises(SystemExit):
                        namespace["service_inactive"]()

    def test_lock_failure_releases_only_owned_descriptors(self):
        namespace = definitions()
        state = Mock()
        allocator, reservation, owner = Mock(), Mock(), Mock()
        state.__truediv__ = Mock(return_value=allocator)
        reservation.name = "uid-55000"
        reservation.is_dir.return_value = True
        reservation.__truediv__ = Mock(return_value=owner)
        state.iterdir.return_value = [reservation]
        namespace["STATE"] = state
        namespace["protected"] = Mock()
        namespace["acquire_lock"] = Mock(side_effect=[44, BlockingIOError("busy")])
        namespace["os"] = types.SimpleNamespace(close=Mock())
        with self.assertRaises(BlockingIOError):
            namespace["lock_existing_owners"]()
        namespace["os"].close.assert_called_once_with(44)

    @unittest.skipUnless(sys.platform == "darwin", "native read-only Darwin process API")
    def test_native_argument_reader_observes_only_this_unprivileged_process(self):
        if os.geteuid() == 0:
            self.skipTest("read-only native check is intentionally unprivileged")
        namespace = definitions()
        arguments = namespace["process_arguments"](os.getpid())
        self.assertTrue(arguments)
        self.assertTrue(Path(arguments[0]).name.lower().startswith("python"))
        self.assertGreater(len(arguments), 1)
        result = subprocess.run(
            ["/bin/ps", "-axo", "pid=,uid=,comm="], capture_output=True, text=True, check=True
        )
        self.assertIn((os.getpid(), os.getuid()), namespace["python_processes"](result.stdout))

    def test_process_scan_refuses_root_installed_script_but_not_inline_bootstrap(self):
        namespace = definitions()
        namespace["subprocess"] = types.SimpleNamespace(
            run=Mock(
                return_value=types.SimpleNamespace(
                    returncode=0,
                    stdout="10 0 /fixed/python3.9\n11 0 /fixed/Python\n12 501 /fixed/python3\n",
                )
            )
        )
        helper = str(namespace["HELPER"])
        namespace["process_arguments"] = Mock(
            side_effect=[("python3", "-I", "-S", "-c", helper), ("python3", helper)]
        )
        with self.assertRaisesRegex(SystemExit, "pid=11"):
            namespace["no_installed_processes"]()
        self.assertEqual(
            [call.args for call in namespace["process_arguments"].call_args_list], [(10,), (11,)]
        )

    def test_process_scan_skips_kernel_sip_and_other_non_python_processes(self):
        namespace = definitions()
        namespace["subprocess"] = types.SimpleNamespace(
            run=Mock(
                return_value=types.SimpleNamespace(
                    returncode=0,
                    stdout="0 0 kernel_task\n1 0 /sbin/launchd\n2 0 /fixed/SIP daemon\n"
                    "3 501 /fixed/Python\n4 0 /fixed/python3.9\n",
                )
            )
        )
        namespace["process_arguments"] = Mock(return_value=("python3.9", "-c", "pass"))
        namespace["no_installed_processes"]()
        namespace["process_arguments"].assert_called_once_with(4)

    def test_atomic_replacement_writes_only_fixed_parent_and_fsyncs(self):
        namespace = definitions()
        events = []
        namespace["os"] = types.SimpleNamespace(
            urandom=lambda _: b"token",
            replace=lambda source, target: events.append(("rename", source, target)),
        )
        namespace["write_new"] = lambda path, data, mode: events.append(("write", path, data, mode))
        namespace["sync_directory"] = lambda path: events.append(("sync", path))
        target = namespace["HELPER"].with_name(namespace["HELPER_PLAN"]["installed"])
        namespace["replace_component"](target, b"replacement")
        self.assertEqual([event[0] for event in events], ["write", "rename", "sync"])
        self.assertEqual(events[0][1].parent, target.parent)
        self.assertEqual(events[0][2:], (b"replacement", 0o644))
        self.assertEqual(events[1][2], target)
        self.assertEqual(events[2][1], target.parent)

    def test_protected_rejects_writable_ancestor(self):
        namespace = definitions()

        def metadata(path):
            return types.SimpleNamespace(
                st_uid=0, st_mode=stat.S_IFDIR | (0o775 if path == Path("/protected") else 0o755)
            )

        with (
            patch.object(Path, "lstat", metadata),
            self.assertRaisesRegex(SystemExit, "/protected"),
        ):
            namespace["protected"](Path("/protected/framework/python"))

    def test_real_unprivileged_bootstrap_refuses_before_bundle_read(self):
        if os.geteuid() == 0:
            self.skipTest("never run this test with root authority")
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", prepare_repair.BOOTSTRAP, "/missing", "0" * 64],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("authenticated isolated macOS Python", result.stderr)
        self.assertNotIn("FileNotFoundError", result.stderr)

    def test_prepare_binds_reviewed_bytes_and_does_not_execute_command(self):
        runtime = types.SimpleNamespace(
            executable=Path("/protected/python3.9"),
            payload=lambda: {"executable": "/protected/python3.9", "prefix": "/protected"},
        )
        plan = json.loads(json.dumps(prepare_repair.HELPER_PLAN))
        for frozen in plan["frozen"].values():
            frozen[1] = hashlib.sha256(b"frozen sibling").hexdigest()
        source = Path(prepare_repair.__file__).parent
        real_read = Path.read_bytes

        def read(path, source=source, plan=plan, real_read=real_read):
            if path == source / plan["source"]:
                return b"test reviewed repair"
            if path.parent == source and path.name in plan["frozen"]:
                return b"frozen sibling"
            return real_read(path)

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "packet"
            with (
                patch.object(prepare_repair, "probe_python", return_value=runtime),
                patch.object(prepare_repair, "HELPER_PLAN", plan),
                patch.object(Path, "read_bytes", read),
                patch.object(subprocess, "run", side_effect=AssertionError("must not execute")),
            ):
                result = prepare_repair.prepare(destination)
            raw = Path(result["bundle"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), result["sha256"])
            payload = json.loads(raw)
            self.assertEqual(base64.b64decode(payload["replacement"]), b"test reviewed repair")
            self.assertEqual(payload["schema"], 2)
            self.assertEqual(payload["component"], "helper")
            self.assertEqual(payload["predecessor_sha256"], plan["predecessor"])
            self.assertEqual(payload["replacement_sha256"], result["replacement_sha256"])
            command = shlex.split(Path(result["command"]).read_text())
            self.assertEqual(
                command[:6],
                [
                    "/usr/bin/sudo",
                    str(runtime.executable),
                    "-I",
                    "-S",
                    "-c",
                    prepare_repair.BOOTSTRAP,
                ],
            )
            self.assertEqual(command[-2:], [str(destination / "repair.json"), result["sha256"]])
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o700)
            for path in destination.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_fixed_helper_plan_pins_all_authority(self):
        plan = definitions()["HELPER_PLAN"]
        self.assertEqual(plan, prepare_repair.HELPER_PLAN)
        self.assertEqual(
            plan,
            {
                "source": "uid_gate.py",
                "installed": "com.aidashos.verifier-uid.py",
                "predecessor": "4694c1275c82fd8e9a859bc29a3ffa7e893eab7d34c950be83d9754ac2bcc06a",
                "frozen": {
                    "qualify_native.py": [
                        "com.aidashos.verifier-uid-qualify.py",
                        "091385e251331d964a56af900f069cc4b3c840edff4a239c0814f912dc6632fd",
                    ],
                    "qualification_canaries.py": [
                        "com.aidashos.verifier-uid-canaries.py",
                        "eadeca5ade0ab6e162e246e95c0632bc800c42ae58dbe01118505911db7b51f9",
                    ],
                },
            },
        )

    def test_retired_packet_formats_refuse_before_ownership_or_writes(self):
        for format_name in ("schema1-helper", "schema2-qualifier", "unknown-version"):
            with self.subTest(format=format_name):
                namespace, payload, files, events, run = self.fixture()
                if format_name == "schema1-helper":
                    payload["schema"] = 1
                    payload.pop("component")
                    payload["helper"] = payload.pop("replacement")
                    payload["helper_sha256"] = payload.pop("replacement_sha256")
                elif format_name == "schema2-qualifier":
                    payload["component"] = "qualifier"
                else:
                    payload["schema"] = 3
                before = dict(files)
                with self.assertRaises(SystemExit):
                    run()
                self.assertEqual(events, [])
                for name in (
                    "protected",
                    "no_installed_processes",
                    "lock_existing_owners",
                    "service_inactive",
                ):
                    namespace[name].assert_not_called()
                for path, content in before.items():
                    self.assertEqual(files[path], content)

    def test_helper_packet_cannot_choose_paths_or_rewrite_frozen_authority(self):
        changes = (
            {"component": "canary"},
            {"component": "../uid_gate.py"},
            {"component": {}},
            {"schema": True},
            {"target": "/arbitrary/root/file"},
            {"frozen": {}},
            {"predecessor_sha256": "0" * 64},
            {"replacement_sha256": "0" * 64},
        )
        for change in changes:
            with self.subTest(change=change):
                namespace, payload, _, events, run = self.fixture()
                payload.update(change)
                with self.assertRaises(SystemExit):
                    run()
                self.assertEqual(events, [])
                namespace["lock_existing_owners"].assert_not_called()
                namespace["no_installed_processes"].assert_not_called()

    def test_retired_qualifier_selector_refuses_before_packet_preparation(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "packet"
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(prepare_repair.__file__)),
                    str(destination),
                    "--component",
                    "qualifier",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("invalid choice", result.stderr)
            self.assertFalse(destination.exists())

    def test_frozen_source_refusal_creates_no_helper_packet(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "packet"
            with (
                patch.object(prepare_repair, "probe_python", return_value=Mock()),
                patch.object(Path, "read_bytes", return_value=b"wrong sibling bytes"),
                self.assertRaisesRegex(ValueError, "sibling source"),
            ):
                prepare_repair.prepare(destination)
            self.assertFalse(destination.exists())

    def test_runtime_refusal_creates_no_packet(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "packet"
            with (
                patch.object(
                    prepare_repair, "probe_python", side_effect=ValueError("mutable runtime")
                ),
                self.assertRaisesRegex(ValueError, "mutable runtime"),
            ):
                prepare_repair.prepare(destination)
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
