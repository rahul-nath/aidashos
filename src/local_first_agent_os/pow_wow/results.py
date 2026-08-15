# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Pow-wow task capture inspection and aggregate status."""

from __future__ import annotations

from collections.abc import Sequence

from .types import PowWowRunStatus, PowWowTaskResult


def _task_command_has_failed(task_result: PowWowTaskResult) -> bool:
    for artifact in task_result.artifacts:
        command = artifact.content.get("command")
        if isinstance(command, dict) and command.get("exit_code") != 0:
            return True
    return False


def _task_verification_has_failed(task_result: PowWowTaskResult) -> bool:
    for artifact in task_result.artifacts:
        verification = artifact.content.get("verification") or []
        if not isinstance(verification, list):
            continue
        for capture in verification:
            if isinstance(capture, dict) and capture.get("exit_code") != 0:
                return True
    return False


def derive_pow_wow_run_status(
    task_results: Sequence[PowWowTaskResult],
) -> PowWowRunStatus:
    if any(_task_command_has_failed(task_result) for task_result in task_results):
        return "FAILED"
    if any(_task_verification_has_failed(task_result) for task_result in task_results):
        return "VERIFICATION_FAILED"
    if any(task_result.status == "failed" for task_result in task_results):
        return "FAILED"
    return "COMPLETED"
