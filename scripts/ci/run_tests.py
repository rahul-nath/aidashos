# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run declared CI test lanes and refuse empty or skipped native evidence."""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import assert_never


class Lane(StrEnum):
    PORTABLE = "portable"
    MACOS_CONTAINMENT = "native"


def targets(lane: Lane) -> tuple[str, ...]:
    match lane:
        case Lane.PORTABLE:
            return ("tests", "scripts/verifier_uid", "scripts/ci/tests")
        case Lane.MACOS_CONTAINMENT:
            # Provider login and installed privileged-helper qualification are separate.
            # Each selected target executes without provider credentials or model calls.
            process_tests = (
                "test_symlinked_interpreter_exposes_only_its_runtime_library_subtree",
                "test_frontier_process_can_run_a_child_through_a_pseudoterminal",
                "test_frontier_environment_carries_context_but_no_control_plane_authority",
                "test_implementation_can_write_only_its_leased_worktree",
                "test_read_only_process_cannot_write_its_checkout",
                "test_agent_process_cannot_read_the_operator_token",
                "test_agent_process_cannot_read_an_undeclared_host_file",
                "test_only_the_reader_database_endpoint_enters_the_network_boundary",
            )
            return (
                "tests/test_native_verification_broker.py",
                *(f"tests/test_process_containment.py::{name}" for name in process_tests),
                "tests/test_verification_toolchain_staging.py::"
                "test_native_link_following_does_not_prove_readlink_permission",
                "tests/test_uid_verifier_client.py::test_kernel_peer_must_be_root",
            )
        case _:
            assert_never(lane)


@dataclass(frozen=True)
class Report:
    tests: int
    failures: int
    skipped: int


def read_report(path: Path) -> Report:
    root = ET.parse(path).getroot()
    cases = tuple(root.iter("testcase"))
    return Report(
        tests=len(cases),
        failures=sum(
            case.find("failure") is not None or case.find("error") is not None for case in cases
        ),
        skipped=sum(case.find("skipped") is not None for case in cases),
    )


def validate_report(lane: Lane, report: Report) -> None:
    if report.tests == 0:
        raise ValueError("CI lane produced no test cases")
    if report.failures:
        raise ValueError(f"CI lane retained {report.failures} failures or errors")
    if lane is Lane.MACOS_CONTAINMENT and report.skipped:
        raise ValueError(f"macOS containment requires every proof: {report.skipped} skipped")


def run(lane: Lane, root: Path, output: Path) -> int:
    if lane is Lane.MACOS_CONTAINMENT and platform.system() != "Darwin":
        raise ValueError("macOS containment must run on Darwin")
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / f"{lane.value}.xml"
    # Refuse stale reports: interrupted pytest must never validate an earlier run.
    report_path.unlink(missing_ok=True)
    result = subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            "-ra",
            "--strict-markers",
            f"--junitxml={report_path}",
            *targets(lane),
        ),
        cwd=root,
        check=False,
    )
    if result.returncode:
        return result.returncode if result.returncode > 0 else 1
    report = read_report(report_path)
    validate_report(lane, report)
    print(f"{lane.value}: {report.tests} tests, {report.skipped} explicitly reported skips")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lane", type=Lane, choices=tuple(Lane))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, default=Path("artifacts"))
    args = parser.parse_args(argv)
    try:
        return run(args.lane, args.root.resolve(strict=True), args.output.resolve())
    except (OSError, ValueError, ET.ParseError) as error:
        print(f"CI test lane refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
