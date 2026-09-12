"""Review-packet integrity checks without invoking sudo or installing system files."""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import os
import plistlib
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import prepare_install


class InstallerPacket(unittest.TestCase):
    def test_preparation_is_repeatable_and_binds_every_installed_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            for mode in ("per-launch", "service"):
                with self.subTest(mode=mode):
                    first = prepare_install.prepare(directory, mode)
                    bundle = Path(first["bundle"]).read_bytes()
                    command = Path(first["command"]).read_bytes()
                    second = prepare_install.prepare(directory, mode)
                    self.assertEqual(first, second)
                    self.assertEqual(bundle, Path(second["bundle"]).read_bytes())
                    self.assertEqual(command, Path(second["command"]).read_bytes())
                    self.assertEqual(first["sha256"], hashlib.sha256(bundle).hexdigest())
                    payload = json.loads(bundle)
                    for name, source_name in (
                        ("helper", "uid_gate.py"),
                        ("canaries", "qualification_canaries.py"),
                        ("qualifier", "qualify_native.py"),
                    ):
                        content = base64.b64decode(payload["files"][name], validate=True)
                        self.assertEqual(
                            content, Path(__file__).with_name(source_name).read_bytes()
                        )
                        self.assertEqual(
                            first["file_sha256"][name], hashlib.sha256(content).hexdigest()
                        )
                    invocation = shlex.split(command.decode())
                    self.assertEqual(
                        invocation[:5],
                        ["/usr/bin/sudo", first["python"]["executable"], "-I", "-S", "-c"],
                    )
                    self.assertEqual(invocation[5], prepare_install.BOOTSTRAP)
                    self.assertEqual(invocation[-1], first["sha256"])
                    self.assertEqual(payload["python"], first["python"])

    def test_mutable_runtime_ancestor_is_refused_even_with_root_owned_leaf(self):
        def metadata(path):
            return SimpleNamespace(
                st_uid=0,
                st_mode=stat.S_IFDIR | (0o775 if path == Path("/Applications") else 0o755),
            )

        with (
            patch.object(Path, "lstat", metadata),
            self.assertRaisesRegex(ValueError, r"/Applications .*mode=0775"),
        ):
            prepare_install._root_owned(Path("/Applications/ExamplePython/bin/python3"))

    def test_unsafe_runtime_is_refused_before_packet_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "must-not-exist"
            with (
                patch.object(
                    prepare_install, "probe_python", side_effect=ValueError("mutable runtime")
                ),
                self.assertRaisesRegex(ValueError, "mutable runtime"),
            ):
                prepare_install.prepare(destination, "service")
            self.assertFalse(destination.exists())

    def test_missing_runtime_never_falls_back_or_creates_packet(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "must-not-exist"
            with patch.object(subprocess, "run") as invoked, self.assertRaises(FileNotFoundError):
                prepare_install.prepare(destination, "service", Path(temporary) / "missing-python")
            invoked.assert_not_called()
            self.assertFalse(destination.exists())

    def test_pinned_probe_ignores_developer_and_python_environment_redirects(self):
        with patch.dict(
            os.environ,
            {
                "DEVELOPER_DIR": "/does-not-exist/developer",
                "PYTHONHOME": "/does-not-exist/home",
                "PYTHONPATH": "/does-not-exist/path",
            },
        ):
            runtime = prepare_install.probe_python(prepare_install.DEFAULT_PYTHON)
        expected = prepare_install.DEFAULT_PYTHON.resolve()
        self.assertEqual(runtime.executable, expected)
        self.assertTrue(runtime.executable.is_relative_to(runtime.prefix))
        self.assertTrue(str(runtime.prefix).startswith("/Library/Developer/CommandLineTools/"))

    def test_generated_service_uses_the_reviewed_runtime(self):
        runtime = prepare_install.probe_python(prepare_install.DEFAULT_PYTHON)
        tree = ast.parse(prepare_install.BOOTSTRAP)
        service_branch = next(
            node
            for node in tree.body
            if isinstance(node, ast.If) and ast.unparse(node.test) == "bundle['mode'] == 'service'"
        )
        writes = []
        namespace = {
            "bundle": {"mode": "service"},
            "Path": Path,
            "python": runtime.executable,
            "helper": Path("/Library/PrivilegedHelperTools/com.aidashos.verifier-uid.py"),
            "plistlib": plistlib,
            "write": lambda path, content: writes.append((path, content)),
        }
        exec(
            compile(ast.Module(body=[service_branch], type_ignores=[]), "bootstrap", "exec"),
            namespace,
        )
        self.assertEqual(len(writes), 1)
        document = plistlib.loads(writes[0][1])
        self.assertEqual(
            document["ProgramArguments"][:4],
            [str(runtime.executable), "-I", "-S", str(namespace["helper"])],
        )

    def test_bootstrap_rejects_another_runtime_identity(self):
        tree = ast.parse(prepare_install.BOOTSTRAP)
        binding_guard = next(
            node
            for node in tree.body
            if isinstance(node, ast.If) and "bundle['python']" in ast.unparse(node.test)
        )
        namespace = {
            "bundle": {"python": {"executable": "/another/python", "prefix": "/another"}},
            "python": Path("/selected/bin/python"),
            "prefix": Path("/selected"),
        }
        with self.assertRaisesRegex(SystemExit, "differs from the reviewed Python runtime"):
            exec(
                compile(ast.Module(body=[binding_guard], type_ignores=[]), "bootstrap", "exec"),
                namespace,
            )

    @unittest.skipIf(os.geteuid() == 0, "this check must never install privileged files")
    def test_real_system_bootstrap_refuses_before_opening_bundle_without_authentication(self):
        result = subprocess.run(
            [
                "/usr/bin/python3",
                "-I",
                "-S",
                "-c",
                prepare_install.BOOTSTRAP,
                "/does-not-exist/installer.json",
                "a" * 64,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("authenticated isolated system Python is required", result.stderr)
        self.assertNotIn("FileNotFoundError", result.stderr)


if __name__ == "__main__":
    unittest.main()
