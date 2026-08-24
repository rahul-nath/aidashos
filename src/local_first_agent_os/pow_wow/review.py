# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Typed task-purpose and review decision operations."""

from __future__ import annotations

from .protocol import ReviewDisposition, ReviewVerdict, TaskPurpose
from .types import PowWowTaskResult, PowWowTaskSpec


def is_implementation_task(task: PowWowTaskSpec) -> bool:
    return task.purpose is TaskPurpose.IMPLEMENTATION


def is_agent_task(task: PowWowTaskSpec) -> bool:
    """A task runs a live agent if it carries a JudgmentRole (implementer,
    reviewer, ...) or, for legacy tasks with no role, matches the old
    implementation-name heuristic."""
    return task.judgment is not None or task.purpose in {
        TaskPurpose.IMPLEMENTATION,
        TaskPurpose.RECOVERY_REVISION,
        TaskPurpose.REVIEW,
    }


def is_review_task(task: PowWowTaskSpec) -> bool:
    return task.purpose is TaskPurpose.REVIEW


def extract_review_verdict_text(task_result: PowWowTaskResult) -> str | None:
    for artifact in task_result.artifacts:
        verdict = artifact.content.get("verdict")
        if isinstance(verdict, str) and verdict.strip():
            return verdict.strip()
    return None


def review_verdict_disposition(verdict: str) -> ReviewDisposition:
    return ReviewVerdict.parse(verdict).disposition


def review_verdict_requests_changes(verdict: str) -> bool:
    return review_verdict_disposition(verdict).requests_changes
