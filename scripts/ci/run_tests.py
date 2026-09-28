# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run declared CI test lanes and refuse empty or skipped native evidence."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import assert_never

import pytest


class Lane(StrEnum):
    PORTABLE = "portable"
    MACOS_CONTAINMENT = "native"


@dataclass(frozen=True)
class Shard:
    index: int = 0
    count: int = 1

    def __post_init__(self) -> None:
        if self.count < 1 or not 0 <= self.index < self.count:
            raise ValueError(
                "shard count must be positive and index must satisfy 0 <= index < count"
            )

    def select(self, files: tuple[str, ...]) -> tuple[str, ...]:
        selected = tuple(sorted(set(files)))[self.index :: self.count]
        if not selected:
            raise ValueError("CI shard selected no test files")
        return selected


@dataclass(frozen=True)
class Collection:
    files: tuple[str, ...]
    nodeids: tuple[str, ...]


UNSHARDED = Shard()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--ci-collect-json", type=Path, default=None)


def pytest_collection_finish(session: pytest.Session) -> None:
    """Machine-readable discovery uses pytest's real configuration and collection hooks."""
    destination = session.config.getoption("--ci-collect-json")
    if destination is not None:
        destination.write_text(
            json.dumps(
                {
                    "files": sorted(
                        {
                            str(item.path.relative_to(session.config.rootpath))
                            for item in session.items
                        }
                    ),
                    "nodeids": [item.nodeid for item in session.items],
                }
            )
        )


def collect(root: Path, selected: tuple[str, ...]) -> Collection:
    with tempfile.TemporaryDirectory(prefix="aidashos-ci-collection-") as scratch:
        manifest = Path(scratch) / "collection.json"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            (str(Path(__file__).resolve().parent), environment.get("PYTHONPATH", ""))
        )
        completed = subprocess.run(
            (
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "--quiet",
                "--strict-markers",
                "-p",
                "run_tests",
                "--ci-collect-json",
                str(manifest),
                *selected,
            ),
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            raise ValueError(
                f"pytest collection failed ({completed.returncode}):\n"
                f"{completed.stdout[-4000:]}\n{completed.stderr[-2000:]}"
            )
        payload = json.loads(manifest.read_text())
    if not isinstance(payload, dict) or set(payload) != {"files", "nodeids"}:
        raise ValueError("pytest collection manifest has an invalid shape")
    for values in payload.values():
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(v, str) for v in values)
        ):
            raise ValueError("pytest collection manifest must contain nonempty string lists")
    return Collection(files=tuple(payload["files"]), nodeids=tuple(payload["nodeids"]))


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


def run(lane: Lane, root: Path, output: Path, shard: Shard = UNSHARDED) -> int:
    if lane is Lane.MACOS_CONTAINMENT and shard.count != 1:
        raise ValueError("native containment qualification cannot be sharded")
    if lane is Lane.MACOS_CONTAINMENT and platform.system() != "Darwin":
        raise ValueError("macOS containment must run on Darwin")
    selected = targets(lane)
    if shard.count > 1:
        discovered = collect(root, selected)
        selected = shard.select(discovered.files)
        print(
            f"{lane.value} shard {shard.index}/{shard.count}: "
            f"{len(selected)} of {len(discovered.files)} test files"
        )
    output.mkdir(parents=True, exist_ok=True)
    suffix = f"-{shard.index}-of-{shard.count}" if shard.count > 1 else ""
    report_path = output / f"{lane.value}{suffix}.xml"
    # Refuse stale reports: interrupted pytest must never validate an earlier run.
    report_path.unlink(missing_ok=True)
    result = subprocess.run(
        (
            sys.executable,
            "-m",
            "pytest",
            "-ra",
            "--tb=short",
            "--maxfail=5",
            "--strict-markers",
            f"--junitxml={report_path}",
            *selected,
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
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        return run(
            args.lane,
            args.root.resolve(strict=True),
            args.output.resolve(),
            Shard(index=args.shard_index, count=args.shard_count),
        )
    except (OSError, ValueError, ET.ParseError) as error:
        print(f"CI test lane refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
