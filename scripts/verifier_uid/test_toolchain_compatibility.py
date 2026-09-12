"""The composed command plan is shared by independent ordinary compatibility runs."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import qualify_native
import toolchain_compatibility as compatibility


def staged_fixture(anchor):
    directory = anchor / "toolchain"
    directory.mkdir()
    python = directory / "python"
    python.touch()
    executables = []
    for name in ("uv", "git", "node"):
        path = directory / name
        path.touch()
        executables.append(str(path))
    return {"python": str(python), "environment": str(directory), "executables": executables}


def successful_output(probe, staged):
    return {
        compatibility.ProbeKind.UV: "uv 0.12.3\n",
        compatibility.ProbeKind.PYTHON: "python ready\n",
        compatibility.ProbeKind.NODE: "node spawn ready\n",
        compatibility.ProbeKind.UV_PYTHON: "uv spawned Python\n",
        compatibility.ProbeKind.GIT: "git version 2.55.0\n",
        compatibility.ProbeKind.PYTHON_IDENTITY: json.dumps({"executable": staged["python"]}),
    }[probe.kind]


class Compatibility(unittest.TestCase):
    def test_first_four_commands_match_observed_frozen_qualifier_calls(self):
        class StopAfterCompatibility(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            anchor = Path(temporary)
            staged = staged_fixture(anchor)
            staged["manifest_digest"] = "fixture-only"
            canary = anchor / "inert-installed-code"
            canary.write_text("fixture only")
            gate = Mock(closed=False)
            helper = SimpleNamespace(persist=Mock())
            calls = []

            def capture(actual_gate, argv, profile, actual_staged=None):
                self.assertIs(actual_gate, gate)
                self.assertIs(actual_staged, staged)
                calls.append(tuple(argv))
                return {"fixture": True}

            with (
                patch.object(qualify_native, "HELPER", canary),
                patch.object(qualify_native, "CANARIES", canary),
                patch.object(
                    qualify_native, "fixed_files", return_value=(anchor, anchor / "denied")
                ),
                patch.object(qualify_native, "Gate", return_value=gate),
                patch.object(qualify_native, "stage", return_value=staged),
                patch.object(qualify_native, "run_probe", side_effect=capture),
                patch.object(qualify_native, "checked_canary", side_effect=StopAfterCompatibility),
                patch.object(qualify_native, "clear_fixture_privileges"),
                self.assertRaises(StopAfterCompatibility),
            ):
                qualify_native.qualify(helper, os.getuid(), anchor, Path(sys.executable))
            self.assertEqual(
                calls, [probe.argv for probe in compatibility.command_plan(staged)[:4]]
            )
            self.assertEqual(len(calls), 4)

    def test_independent_runner_reuses_staged_bytes_and_removes_only_its_scratch(self):
        with tempfile.TemporaryDirectory() as temporary:
            anchor = Path(temporary).resolve()
            source = anchor / "source"
            source.mkdir()
            staged = staged_fixture(anchor)
            plan = compatibility.command_plan(staged)
            original = {path: path.read_bytes() for path in (anchor / "toolchain").iterdir()}
            homes = []
            seen = []

            def execute(argv, **options):
                probe = plan[len(seen) % len(plan)]
                self.assertEqual(tuple(argv), probe.argv)
                self.assertNotIn("--destination", argv)
                self.assertEqual(options["cwd"], source)
                self.assertEqual(
                    {
                        key: value
                        for key, value in options["env"].items()
                        if key not in ("HOME", "TMPDIR")
                    },
                    compatibility.environment(staged),
                )
                self.assertTrue(Path(options["env"]["HOME"]).is_dir())
                homes.append(Path(options["env"]["HOME"]).parent)
                seen.append(tuple(argv))
                return subprocess.CompletedProcess(argv, 0, successful_output(probe, staged), "")

            log = io.BytesIO()
            with patch.object(compatibility.subprocess, "run", side_effect=execute):
                for _ in range(2):
                    results = compatibility.run_compatibility(staged, source, anchor, log)
                    self.assertEqual(
                        tuple(result.kind for result in results), tuple(p.kind for p in plan)
                    )
            self.assertEqual(len(seen), 12)
            self.assertEqual(len(set(homes)), 2)
            self.assertTrue(all(not home.exists() for home in homes))
            self.assertTrue(source.is_dir())
            self.assertEqual({path: path.read_bytes() for path in original}, original)
            self.assertIn(b"Starting compatibility command: uv-python", log.getvalue())

    def test_zero_exit_without_success_output_is_refused_for_every_probe(self):
        with tempfile.TemporaryDirectory() as temporary:
            anchor = Path(temporary)
            staged = staged_fixture(anchor)
            for probe in compatibility.command_plan(staged):
                with self.subTest(kind=probe.kind), self.assertRaises((ValueError, RuntimeError)):
                    compatibility.validate_result(
                        probe,
                        subprocess.CompletedProcess(probe.argv, 0, "", ""),
                        Path(staged["python"]),
                    )

    def test_uv_run_false_zero_fails_after_three_positive_baselines(self):
        with tempfile.TemporaryDirectory() as temporary:
            anchor = Path(temporary).resolve()
            source = anchor / "source"
            source.mkdir()
            staged = staged_fixture(anchor)
            plan = compatibility.command_plan(staged)
            responses = [
                subprocess.CompletedProcess(probe.argv, 0, successful_output(probe, staged), "")
                for probe in plan[:3]
            ] + [subprocess.CompletedProcess(plan[3].argv, 0, "", "did not start Python")]
            log = io.BytesIO()
            with (
                patch.object(compatibility.subprocess, "run", side_effect=responses) as run,
                self.assertRaisesRegex(RuntimeError, "uv-python.*required success output"),
            ):
                compatibility.run_compatibility(staged, source, anchor, log)
            self.assertEqual(run.call_count, 4)
            self.assertIn(b"did not start Python", log.getvalue())
            self.assertFalse(list(anchor.glob("compatibility-*")))
            self.assertTrue(Path(staged["python"]).exists())

    def test_timeout_retains_both_streams_without_removing_source_or_toolchain(self):
        with tempfile.TemporaryDirectory() as temporary:
            anchor = Path(temporary).resolve()
            source = anchor / "source"
            source.mkdir()
            staged = staged_fixture(anchor)
            log = io.BytesIO()
            failure = subprocess.TimeoutExpired(
                "probe", 20, output=b"partial out", stderr=b"partial err"
            )
            with (
                patch.object(compatibility.subprocess, "run", side_effect=failure),
                self.assertRaises(subprocess.TimeoutExpired),
            ):
                compatibility.run_compatibility(staged, source, anchor, log)
            self.assertIn(b"partial out", log.getvalue())
            self.assertIn(b"partial err", log.getvalue())
            self.assertTrue(source.is_dir())
            self.assertTrue(Path(staged["python"]).exists())
            self.assertFalse(list(anchor.glob("compatibility-*")))

    def test_independent_runner_rejects_privilege_or_path_escape_before_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            anchor = Path(temporary).resolve()
            source = anchor / "source"
            source.mkdir()
            staged = staged_fixture(anchor)
            with (
                patch.object(compatibility.os, "geteuid", return_value=0),
                patch.object(compatibility.subprocess, "run") as run,
                self.assertRaises(PermissionError),
            ):
                compatibility.run_compatibility(staged, source, anchor, io.BytesIO())
            run.assert_not_called()
            Path(staged["python"]).unlink()
            Path(staged["python"]).symlink_to(sys.executable)
            with (
                patch.object(compatibility.subprocess, "run") as run,
                self.assertRaisesRegex(ValueError, "escaped"),
            ):
                compatibility.run_compatibility(staged, source, anchor, io.BytesIO())
            run.assert_not_called()
            self.assertFalse(list(anchor.glob("compatibility-*")))

    def test_virtual_environment_cannot_escape_independent_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            anchor = Path(temporary).resolve()
            source = anchor / "source"
            source.mkdir()
            staged = staged_fixture(anchor)
            outside = anchor.parent
            linked = anchor / "external-environment"
            linked.symlink_to(outside)
            for path in (str(outside), "relative/environment", str(anchor / ".."), str(linked)):
                with self.subTest(path=path):
                    staged["environment"] = path
                    with (
                        patch.object(compatibility.subprocess, "run") as run,
                        self.assertRaisesRegex(ValueError, "escaped"),
                    ):
                        compatibility.run_compatibility(staged, source, anchor, io.BytesIO())
                    run.assert_not_called()
                    self.assertFalse(list(anchor.glob("compatibility-*")))


if __name__ == "__main__":
    unittest.main()
