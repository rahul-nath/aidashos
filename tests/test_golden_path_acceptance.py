# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Acceptance evidence must contain the selected scenario and its successful run."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from local_first_agent_os.verification_git import verification_git_environment
from scripts import accept_golden_path
from scripts.accept_golden_path import coverage


@pytest.mark.parametrize(
    ("case", "accepted"),
    [
        ('<testcase classname="tests.test_flow" name="test_resident"/>', True),
        ('<testcase classname="tests.test_flow" name="test_resident"><skipped/></testcase>', False),
        ('<testcase classname="tests.test_flow" name="test_resident"><failure/></testcase>', False),
        ('<testcase classname="tests.test_flow" name="test_simulated_alias"/>', False),
        ("", False),
    ],
)
def test_missing_skipped_or_unsuccessful_scenario_cannot_certify_acceptance(
    tmp_path: Path, case: str, accepted: bool
) -> None:
    report = tmp_path / "cases.xml"
    report.write_text(f"<testsuites><testsuite>{case}</testsuite></testsuites>")
    observed = coverage(report, ("tests/test_flow.py::test_resident",))
    assert (not observed["missing"] and not observed["unsuccessful"]) is accepted


def test_acceptance_identity_ignores_foreign_git_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, foreign = tmp_path / "source", tmp_path / "foreign"
    for root in (source, foreign):
        root.mkdir()
        (root / "tracked.txt").write_text(root.name)
        for command in (
            ("init", "-q"),
            ("add", "tracked.txt"),
            (
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "initial",
            ),
        ):
            subprocess.run(
                ["git", "-C", str(root), *command],
                env=verification_git_environment(),
                check=True,
                capture_output=True,
            )
    monkeypatch.setattr(accept_golden_path, "ROOT", source)
    expected = accept_golden_path.source_identity()
    for name, value in {
        "GIT_DIR": str(foreign / ".git"),
        "GIT_WORK_TREE": str(foreign),
        "GIT_INDEX_FILE": str(foreign / ".git" / "index"),
    }.items():
        monkeypatch.setenv(name, value)
    assert accept_golden_path.source_identity() == expected
    (foreign / "tracked.txt").write_text("foreign edit")
    assert accept_golden_path.source_identity() == expected
    (source / "tracked.txt").write_text("source edit")
    changed = accept_golden_path.source_identity()
    assert changed["commit"] == expected["commit"]
    assert changed["tree_sha256"] != expected["tree_sha256"]


def test_post_run_identity_failure_cannot_leave_a_passed_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "acceptance"
    monkeypatch.setattr(accept_golden_path.sys, "argv", ["accept", "--report", str(directory)])
    monkeypatch.setattr(accept_golden_path, "CORE", ("tests/test_flow.py::test_resident",))
    calls = 0

    def identity() -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise subprocess.CalledProcessError(128, ["git", "rev-parse", "HEAD"])
        return {"commit": "fixture", "tree_sha256": "fixture"}

    def passed_pytest(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        (directory / "cases.xml").write_text(
            '<testsuites><testsuite><testcase classname="tests.test_flow" '
            'name="test_resident"/></testsuite></testsuites>'
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(accept_golden_path, "source_identity", identity)
    monkeypatch.setattr(accept_golden_path.subprocess, "run", passed_pytest)
    previous_umask = os.umask(0o077)
    try:
        assert accept_golden_path.main() == 1
    finally:
        os.umask(previous_umask)
    report = json.loads((directory / "result.json").read_text())
    assert calls == 2
    assert report["status"] == "failed"
    assert "CalledProcessError" in report["error"]
