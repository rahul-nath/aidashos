"""The preflight reproduces the frozen operator call, without a privileged launch."""

from __future__ import annotations

import json
import os
import pwd
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import qualify_native
import stage_preflight
import toolchain_compatibility


def staging_fixture(mode):
    """Executable shell stubs exercise orchestration, not installed-tool correctness."""
    programs = {
        "uv": "#!/bin/sh\ncase \"$1\" in\n--version) printf 'uv 0.0.0 fixture\\n';;\n"
        "run) printf 'uv spawned Python\\n';;\nesac\n",
        "git": "#!/bin/sh\nprintf 'git version 0.0.0 fixture\\n'\n",
        "node": "#!/bin/sh\nprintf 'node spawn ready\\n'\n",
    }
    if mode == "empty_uv":
        programs["uv"] = "#!/bin/sh\nexit 0\n"
    elif mode == "empty_uv_run":
        programs["uv"] = (
            "#!/bin/sh\nif [ \"$1\" = --version ]; then printf 'uv 0.0.0 fixture\\n'; fi\n"
        )
    python_prefix = "#!/bin/sh\ncase \"$2\" in\n*sys.executable*) printf '%s\\n' "
    python_suffix = ";;\n*) printf 'python ready\\n';;\nesac\n"
    return "\n".join(
        [
            "import json,pathlib,shlex,sys",
            f"mode={mode!r}",
            f"programs={programs!r}",
            "destination=pathlib.Path(sys.argv[sys.argv.index('--destination')+1])",
            "(destination/'partial-copy').write_text('owned fixture')",
            "print('stderr diagnostic canary',file=sys.stderr)",
            "if mode=='failure':",
            " print('stdout diagnostic canary'); raise RuntimeError('failure canary')",
            "python=destination/'python'",
            "if mode=='parent_escape': python=destination/'..'/'outside-python'",
            "if mode=='symlink_escape': python.symlink_to(sys.executable)",
            "else:",
            " reported=sys.executable if mode=='wrong_interpreter' else str(python)",
            f" python.write_text({python_prefix!r}"
            "+shlex.quote(json.dumps({'executable':reported}))"
            f"+{python_suffix!r})",
            " python.chmod(0o755)",
            "executables=[]",
            "for name in ('uv','git','node'):",
            " p=destination/name; p.write_text(programs[name])",
            " p.chmod(0o755); executables.append(str(p))",
            "print(json.dumps({'python':str(python),'environment':str(destination),'executables':executables}))",
        ]
    )


class StagePreflight(unittest.TestCase):
    def test_call_and_environment_match_the_frozen_qualifier(self):
        account = pwd.getpwuid(os.getuid())
        project, python = Path("/fixture/project"), Path("/fixture/python")
        destination, source = Path("/fixture/toolchain"), Path("/fixture/source")
        payload = json.dumps({"python": str(destination / "python"), "executables": []})
        result = subprocess.CompletedProcess([], 0, payload, "")
        gate = SimpleNamespace(
            operator=os.getuid(),
            opened={"toolchain": str(destination), "source": str(source)},
        )
        with (
            patch.object(
                qualify_native, "as_operator", side_effect=lambda _uid, call: call(account)
            ),
            patch.object(qualify_native.subprocess, "run", return_value=result) as invoked,
        ):
            qualify_native.stage(gate, project, python)
        expected = invoked.call_args
        with patch.object(stage_preflight.subprocess, "run", return_value=result) as actual:
            stage_preflight.stage(project, python, destination, source, account)
        self.assertEqual(actual.call_args, expected)

    def test_smoke_environment_matches_the_frozen_qualifier(self):
        staged = {
            "python": "/fixture/environment/bin/python",
            "environment": "/fixture/environment",
            "executables": ["/fixture/bin/uv", "/fixture/git/bin/git", "/fixture/bin/node"],
        }
        self.assertEqual(
            toolchain_compatibility.environment(staged), qualify_native.environment(staged)
        )

    def test_actual_cli_logs_both_streams_and_cleans_its_workspace(self):
        for mode in (
            "failure",
            "success",
            "symlink_escape",
            "parent_escape",
            "wrong_interpreter",
            "empty_uv",
            "empty_uv_run",
        ):
            succeeds = mode == "success"
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                package = root / "src/local_first_agent_os"
                package.mkdir(parents=True)
                (package / "__init__.py").write_text("")
                module = package / "verification_toolchain_staging.py"
                module.write_text(staging_fixture(mode))
                log = root / "preflight.log"
                result = subprocess.run(
                    [
                        sys.executable,
                        str(Path(stage_preflight.__file__)),
                        "--project",
                        str(root),
                        "--operator-python",
                        sys.executable,
                        "--log",
                        str(log),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0 if succeeds else 1, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["status"], "passed" if succeeds else "failed")
                self.assertEqual(report["native_qualification"], "not_run")
                self.assertNotIn("diagnostic canary", result.stdout + result.stderr)
                recorded = log.read_text()
                self.assertIn("=== staging stdout ===", recorded)
                self.assertIn("stderr diagnostic canary", recorded)
                if succeeds:
                    for name in ("uv", "python", "node", "uv-python", "git", "python-identity"):
                        self.assertIn("=== compatibility " + name + " stdout ===", recorded)
                    self.assertIn("=== staging outcome ===\nstaging passed", recorded)
                    self.assertIn("=== compatibility outcome ===\ncompatibility passed", recorded)
                if mode == "failure":
                    self.assertIn("stdout diagnostic canary", recorded)
                    self.assertIn("RuntimeError: failure canary", recorded)
                elif mode in ("symlink_escape", "parent_escape"):
                    self.assertIn("staged executable escaped its declared anchor", recorded)
                    self.assertNotIn("Starting compatibility command", recorded)
                elif mode == "wrong_interpreter":
                    self.assertIn(
                        "Python compatibility probe reported a different interpreter", recorded
                    )
                elif mode in ("empty_uv", "empty_uv_run"):
                    self.assertIn("omitted its required success output", recorded)
                    self.assertIn("staging passed", recorded)
                    self.assertNotIn("compatibility passed", recorded)
                self.assertEqual(log.stat().st_mode & 0o777, 0o600)
                workspace = Path(recorded.splitlines()[0].split("workspace: ", 1)[1])
                self.assertFalse(workspace.exists())
                self.assertTrue(module.is_file())
                before = log.read_bytes()
                repeated = subprocess.run(result.args, capture_output=True, text=True, timeout=10)
                self.assertEqual(repeated.returncode, 1)
                self.assertEqual(json.loads(repeated.stdout)["status"], "refused")
                self.assertEqual(log.read_bytes(), before)

    def test_timeout_retains_partial_streams_and_cleans_only_its_workspace(self):
        workspaces = []

        def timeout(argv, **_kwargs):
            destination = Path(argv[argv.index("--destination") + 1])
            (destination / "partial-copy").write_text("owned fixture")
            workspaces.append(destination.parent)
            raise subprocess.TimeoutExpired(argv, 150, output=b"partial out", stderr=b"partial err")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "preflight.log"
            with patch.object(stage_preflight.subprocess, "run", side_effect=timeout):
                result = stage_preflight.preflight(root, Path(sys.executable), log)
            self.assertEqual(result, 1)
            self.assertIn("partial out", log.read_text())
            self.assertIn("partial err", log.read_text())
            self.assertIn("TimeoutExpired", log.read_text())
            self.assertTrue(root.exists())
            self.assertTrue(all(not path.exists() for path in workspaces))

    def test_privileged_invocation_is_refused_before_creating_a_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "preflight.log"
            with (
                patch.object(stage_preflight.os, "geteuid", return_value=0),
                self.assertRaises(PermissionError),
            ):
                stage_preflight.preflight(root, Path(sys.executable), log)
            self.assertFalse(log.exists())

    def test_failed_smoke_is_logged_and_cleaned_before_reporting_failure(self):
        workspaces = []

        def invoke(argv, **_kwargs):
            if "--destination" in argv:
                destination = Path(argv[argv.index("--destination") + 1])
                workspaces.append(destination.parent)
                payload = {
                    "python": str(destination / "python"),
                    "environment": str(destination),
                    "executables": [str(destination / name) for name in ("uv", "git", "node")],
                }
                for path in (payload["python"], *payload["executables"]):
                    Path(path).touch()
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
            return subprocess.CompletedProcess(argv, 3, "smoke stdout", "smoke stderr")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            log = root / "preflight.log"
            with patch.object(stage_preflight.subprocess, "run", side_effect=invoke) as run:
                result = stage_preflight.preflight(root, Path(sys.executable), log)
            self.assertEqual(result, 1)
            self.assertEqual(run.call_count, 2)
            self.assertIn("smoke stderr", log.read_text())
            self.assertIn("uv compatibility command exited 3", log.read_text())
            self.assertTrue(all(not path.exists() for path in workspaces))


if __name__ == "__main__":
    unittest.main()
