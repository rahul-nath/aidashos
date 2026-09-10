# SPDX-License-Identifier: AGPL-3.0-or-later
"""Explicit native acceptance admission and skip-fails-release semantics."""

from __future__ import annotations

import os
import platform
import shutil
import tempfile
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-native-codex-srt",
        action="store_true",
        default=False,
        help="Require the installed macOS Codex/SRT acceptance profile, with no skipped proofs.",
    )


def pytest_configure(config: pytest.Config) -> None:
    if not config.getoption("--run-native-codex-srt"):
        return
    source = os.environ.get("LOCAL_AGENT_SRT_PROBE_ROOT")
    node = os.environ.get("LOCAL_AGENT_SRT_PROBE_NODE")
    codex = shutil.which("codex")
    if platform.system() != "Darwin" or not source or not node or not codex:
        raise pytest.UsageError(
            "native Codex/SRT acceptance requires macOS, installed Codex, and explicit "
            "LOCAL_AGENT_SRT_PROBE_ROOT / LOCAL_AGENT_SRT_PROBE_NODE; missing prerequisites "
            "are not a passing or skipped acceptance result"
        )
    from local_first_agent_os.codex_review_launch import _validate_codex
    from local_first_agent_os.sandbox_runtime import SandboxRuntimeInstallation

    try:
        SandboxRuntimeInstallation.inspect(Path(source), Path(node))
        with tempfile.TemporaryDirectory(prefix="aidashos-native-acceptance-") as scratch:
            _validate_codex(Path(codex), Path(scratch).resolve())
    except Exception as exc:
        raise pytest.UsageError(f"native Codex/SRT acceptance prerequisite failed: {exc}") from exc


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    native = [item for item in items if item.get_closest_marker("native_codex_srt")]
    if config.getoption("--run-native-codex-srt"):
        if not native:
            raise pytest.UsageError(
                "native Codex/SRT profile selected without acceptance scenarios"
            )
        return
    for item in native:
        item.add_marker(
            pytest.mark.skip(reason="native acceptance not requested; use --run-native-codex-srt")
        )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):
    report = yield
    if (
        item.config.getoption("--run-native-codex-srt")
        and item.get_closest_marker("native_codex_srt")
        and report.skipped
    ):
        report.outcome = "failed"
        report.longrepr = "Explicit native Codex/SRT acceptance cannot count a skipped proof."
    return report
