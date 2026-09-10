# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Select a registered gate's immutable source from completed dependency evidence.

Only an integrated source-patch record nominates a candidate. Git can prove
ancestry between those recorded candidates, but mutable HEAD cannot nominate one.
Agent checkpoints describe proposed work and do not override integration evidence.
"""

from __future__ import annotations

import re
import subprocess
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from ..project_center import load_project_center
from ..verification_git import verification_git_environment
from . import repository as repo
from .events import ArtifactKind
from .lifecycle import MilestoneExecutionStatus

if TYPE_CHECKING:
    from .execution import MilestoneContext
    from .plan import CompiledWorkPlan


class VerificationSourceFailure(StrEnum):
    IDENTITY_MISMATCH = "VERIFICATION_SOURCE_IDENTITY_MISMATCH"
    DEPENDENCY_INCOMPLETE = "VERIFICATION_SOURCE_DEPENDENCY_INCOMPLETE"
    INTEGRATION_EVIDENCE_MISSING = "VERIFICATION_SOURCE_INTEGRATION_EVIDENCE_MISSING"
    INTEGRATION_EVIDENCE_INVALID = "VERIFICATION_SOURCE_INTEGRATION_EVIDENCE_INVALID"
    COMMIT_UNAVAILABLE = "VERIFICATION_SOURCE_COMMIT_UNAVAILABLE"
    DEPENDENCY_BASES_DIVERGED = "MILESTONE_DEPENDENCY_BASES_DIVERGED"


class VerificationSourceRefused(ValueError):
    def __init__(self, code: VerificationSourceFailure, reason: str) -> None:
        self.code = code
        super().__init__(reason)


def _dependency_closure(plan: CompiledWorkPlan, milestone_key: str) -> frozenset[str]:
    pending = list(plan.milestone(milestone_key).dependencies)
    dependencies: set[str] = set()
    while pending:
        dependency = pending.pop()
        if dependency == milestone_key:
            raise VerificationSourceRefused(
                VerificationSourceFailure.IDENTITY_MISMATCH,
                "compiled verification dependency graph contains a cycle",
            )
        if dependency not in dependencies:
            dependencies.add(dependency)
            pending.extend(plan.milestone(dependency).dependencies)
    return frozenset(dependencies)


def _integration_commit(artifact: repo.ArtifactRow) -> str:
    # These two spellings are emitted by the two governed integration owners.
    # Both present is acceptable only when they assert the same immutable fact.
    candidates = tuple(
        artifact.metadata[key]
        for key in ("integration_commit_sha", "integrated_commit_sha")
        if key in artifact.metadata
    )
    if not candidates or any(
        not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None
        for value in candidates
    ):
        raise VerificationSourceRefused(
            VerificationSourceFailure.INTEGRATION_EVIDENCE_INVALID,
            f"source artifact {artifact.artifact_id} lacks a valid integrated commit identity",
        )
    first = candidates[0]
    if not isinstance(first, str) or any(value != first for value in candidates):
        raise VerificationSourceRefused(
            VerificationSourceFailure.INTEGRATION_EVIDENCE_INVALID,
            f"source artifact {artifact.artifact_id} contains conflicting integration identities",
        )
    return first


def _contains_commit(repository: Path, ancestor: str, descendant: str) -> bool:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True,
            timeout=10,
            check=False,
            env=verification_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VerificationSourceRefused(
            VerificationSourceFailure.COMMIT_UNAVAILABLE,
            "cannot inspect retained verification source ancestry",
        ) from exc
    match completed.returncode:
        case 0:
            return True
        case 1:
            return False
        case _:
            raise VerificationSourceRefused(
                VerificationSourceFailure.COMMIT_UNAVAILABLE,
                "a recorded integration commit is unavailable in the registered repository",
            )


def resolve_registered_verification_source(context: MilestoneContext) -> str:
    unit = repo.get_work_unit(context.work_unit_id)
    plan = repo.get_compiled_plan_revision(unit.compiled_plan_revision_id).plan
    if (
        unit.compiled_plan_hash != context.compiled_plan_hash
        or unit.design_doc_revision_id != context.design_doc_revision_id
        or plan.plan_hash() != context.compiled_plan_hash
        or plan.target_project_id != context.target_project_id
        or plan.milestone(context.milestone.stable_key) != context.milestone
    ):
        raise VerificationSourceRefused(
            VerificationSourceFailure.IDENTITY_MISMATCH,
            "verification context does not match its retained compiled plan",
        )
    dependencies = _dependency_closure(plan, context.milestone.stable_key)
    executions = {
        row.stable_key: row
        for row in repo.list_milestone_executions(context.work_unit_id)
        if row.stable_key in dependencies
    }
    if len(executions) != len(dependencies) or any(
        row.status is not MilestoneExecutionStatus.SUCCEEDED for row in executions.values()
    ):
        raise VerificationSourceRefused(
            VerificationSourceFailure.DEPENDENCY_INCOMPLETE,
            "verification requires every compiled dependency to have succeeded",
        )
    source_dependencies = {
        row.milestone_execution_id: key
        for key, row in executions.items()
        if ArtifactKind.SOURCE_PATCH in plan.milestone(key).required_artifacts
    }
    source_artifacts = tuple(
        artifact
        for artifact in repo.list_work_unit_artifacts(context.work_unit_id)
        if artifact.artifact_type.value == ArtifactKind.SOURCE_PATCH.value
        and artifact.milestone_execution_id in source_dependencies
    )
    if not source_dependencies or {
        artifact.milestone_execution_id for artifact in source_artifacts
    } != set(source_dependencies):
        raise VerificationSourceRefused(
            VerificationSourceFailure.INTEGRATION_EVIDENCE_MISSING,
            "verification lacks an integrated source record for every source-producing dependency",
        )
    commits = frozenset(_integration_commit(artifact) for artifact in source_artifacts)
    project = load_project_center().project_by_id(context.target_project_id)
    candidates = tuple(
        candidate
        for candidate in sorted(commits)
        if all(_contains_commit(project.expanded_path, required, candidate) for required in commits)
    )
    if len(candidates) != 1:
        raise VerificationSourceRefused(
            VerificationSourceFailure.DEPENDENCY_BASES_DIVERGED,
            "no recorded integrated commit contains every required dependency; "
            "integrate them first",
        )
    return candidates[0]
