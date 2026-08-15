# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Independent-before-junior planning contract and durable evidence barrier."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from ..constants import (
    CLI_AGENT_RUN_ARTIFACT_TYPE,
    DELEGATED_TASK_RUN_ARTIFACT_TYPE,
    FRONTIER_FALLBACK_RUN_ARTIFACT_TYPE,
    REPO_AUDIT_ARTIFACT_TYPE,
)
from ..coordination.contracts import AcknowledgementResult, SubmitArtifact
from ..staffing import Tier
from .protocol import PlanningPhase, TaskPurpose
from .repo_audit import (
    REPO_AUDIT_SCHEMA_VERSION,
    RepoAuditError,
    contains_repo_audit_block,
    extract_repo_audit,
)
from .types import (
    CoordinationCommandFn,
    PowWowArtifact,
    PowWowExecutionContext,
    PowWowTaskResult,
    PowWowTaskSpec,
)

SCHEMA_VERSION_PLANNING_EVIDENCE = "planning_evidence.v1"


class PlanningContractError(ValueError):
    """Raised before execution when model visibility ordering is invalid."""


_REQUIRED_PHASES = frozenset(PlanningPhase)
_PHASE_TIER = {
    PlanningPhase.SENIOR_INDEPENDENT_READING: Tier.SENIOR,
    PlanningPhase.JUNIOR_VERIFICATION_PLAN: Tier.JUNIOR,
    PlanningPhase.SENIOR_OWNED_PLAN: Tier.SENIOR,
    PlanningPhase.STAFF_INDEPENDENT_READING: Tier.STAFF,
    PlanningPhase.STAFF_FINAL_REVIEW: Tier.STAFF,
}
_ARTIFACT_TYPE = {
    PlanningPhase.SENIOR_INDEPENDENT_READING: "senior_independent_reading",
    PlanningPhase.JUNIOR_VERIFICATION_PLAN: "junior_verification_plan",
    PlanningPhase.SENIOR_OWNED_PLAN: "senior_owned_plan",
    PlanningPhase.STAFF_INDEPENDENT_READING: "staff_independent_reading",
    PlanningPhase.STAFF_FINAL_REVIEW: "staff_final_review",
}

# Which phases produce a durable repository audit, and which tier's most recent
# audit a phase may inherit. Together they are the whole routing rule: audits
# flow forward along same-tier edges only. The staff final review is absent
# from the consumer map on purpose - reviewer independence means the reviewer
# re-reads the worktree itself rather than inheriting anyone's map of it, which
# is exactly how the 2026-08-10 review caught a false success. The junior phase
# is absent because its output is hypothesis generation over the senior
# reading, not a repository walk of its own.
AUDIT_PRODUCER_PHASES = frozenset(
    {
        PlanningPhase.SENIOR_INDEPENDENT_READING,
        PlanningPhase.STAFF_INDEPENDENT_READING,
    }
)
_AUDIT_CONSUMER_TIER = {
    PlanningPhase.SENIOR_INDEPENDENT_READING: Tier.SENIOR,
    PlanningPhase.SENIOR_OWNED_PLAN: Tier.SENIOR,
    PlanningPhase.STAFF_INDEPENDENT_READING: Tier.STAFF,
}


def audit_consumer_tier(phase: PlanningPhase | None) -> Tier | None:
    """The producing tier whose latest audit this phase may receive, if any."""

    if phase is None:
        return None
    return _AUDIT_CONSUMER_TIER.get(phase)


def validate_planning_visibility_contract(
    tasks: Sequence[PowWowTaskSpec],
    *,
    required: bool,
) -> None:
    """Validate the only legal visibility graph for a planning-enabled run."""

    phased = [task for task in tasks if task.planning_phase is not None]
    if not phased:
        if required:
            raise PlanningContractError(
                "code decomposition requires the independent planning phase contract"
            )
        return
    unphased_judgment = [
        task.task_name
        for task in tasks
        if task.judgment is not None and task.planning_phase is None
    ]
    if unphased_judgment:
        raise PlanningContractError(
            "planning contract has unphased judgment tasks: " + ", ".join(unphased_judgment)
        )
    by_phase: dict[PlanningPhase, PowWowTaskSpec] = {}
    for task in phased:
        assert task.planning_phase is not None
        if task.planning_phase in by_phase:
            raise PlanningContractError(f"duplicate planning phase: {task.planning_phase.value}")
        by_phase[task.planning_phase] = task
        if task.judgment is None or task.judgment.tier is not _PHASE_TIER[task.planning_phase]:
            required_tier = _PHASE_TIER[task.planning_phase]
            raise PlanningContractError(
                f"{task.planning_phase.value} requires {required_tier.value} tier"
            )
    missing = sorted(phase.value for phase in _REQUIRED_PHASES - by_phase.keys())
    if missing:
        raise PlanningContractError(f"planning contract missing phases: {', '.join(missing)}")

    senior_read = by_phase[PlanningPhase.SENIOR_INDEPENDENT_READING]
    junior = by_phase[PlanningPhase.JUNIOR_VERIFICATION_PLAN]
    senior_plan = by_phase[PlanningPhase.SENIOR_OWNED_PLAN]
    staff_read = by_phase[PlanningPhase.STAFF_INDEPENDENT_READING]
    staff_review = by_phase[PlanningPhase.STAFF_FINAL_REVIEW]

    if senior_read.blocked_by:
        raise PlanningContractError("senior independent reading cannot consume dependency output")
    if staff_read.blocked_by:
        raise PlanningContractError("staff independent reading cannot consume dependency output")
    for task in (senior_read, junior, staff_read):
        if task.dispatch_kind != "advisory" or task.purpose is not TaskPurpose.ADVISORY:
            phase = task.planning_phase
            assert phase is not None
            raise PlanningContractError(f"{phase.value} must be an advisory task")
    if senior_plan.dispatch_kind != "code" or senior_plan.purpose is not TaskPurpose.IMPLEMENTATION:
        raise PlanningContractError("senior-owned plan must be the code implementation task")
    if staff_review.dispatch_kind != "code" or staff_review.purpose is not TaskPurpose.REVIEW:
        raise PlanningContractError("staff final review must be a code review task")
    if not senior_plan.worktree_group or staff_review.worktree_group != senior_plan.worktree_group:
        raise PlanningContractError(
            "senior-owned plan and staff final review must share one code worktree group"
        )
    if junior.blocked_by != (senior_read.task_name,):
        raise PlanningContractError(
            "junior verification plan must wait for persisted senior independent reading"
        )
    if set(senior_plan.blocked_by) != {senior_read.task_name, junior.task_name}:
        raise PlanningContractError(
            "senior-owned plan must consume both independent reading and junior hypotheses"
        )
    browser_tasks = [task for task in tasks if task.purpose is TaskPurpose.BROWSER_ACCEPTANCE]
    if browser_tasks:
        if len(browser_tasks) != 1:
            raise PlanningContractError(
                "marketing-site planning requires exactly one browser acceptance task"
            )
        browser_task = browser_tasks[0]
        if browser_task.blocked_by != (senior_plan.task_name,):
            raise PlanningContractError(
                "browser acceptance must wait for the senior-owned implementation"
            )
        expected_review_dependencies = {staff_read.task_name, browser_task.task_name}
    else:
        expected_review_dependencies = {staff_read.task_name, senior_plan.task_name}
    if set(staff_review.blocked_by) != expected_review_dependencies:
        raise PlanningContractError(
            "staff final review must consume its independent reading and the final host evidence"
        )


def build_planning_evidence_artifact(
    task: PowWowTaskSpec,
    result: PowWowTaskResult,
) -> PowWowArtifact | None:
    phase = task.planning_phase
    if phase is None or result.status != "completed":
        return None
    output = _extract_planning_model_output(result)
    if not output.strip():
        raise PlanningContractError(f"{phase.value} completed without model output to persist")
    return PowWowArtifact(
        artifact_type=_ARTIFACT_TYPE[phase],
        schema_version=SCHEMA_VERSION_PLANNING_EVIDENCE,
        task_name=task.task_name,
        content={
            "schema_version": SCHEMA_VERSION_PLANNING_EVIDENCE,
            "phase": phase.value,
            "task_name": task.task_name,
            "tier": task.judgment.tier.value if task.judgment else None,
            "non_exhaustive": phase is PlanningPhase.JUNIOR_VERIFICATION_PLAN,
            "senior_owns_final_plan": phase is PlanningPhase.SENIOR_OWNED_PLAN,
            "model_output": output,
        },
    )


def persist_planning_evidence(
    *,
    pow_wow_id: str,
    task: PowWowTaskSpec,
    result: PowWowTaskResult,
    context: PowWowExecutionContext,
    coordination_command: CoordinationCommandFn | None,
) -> PowWowTaskResult:
    """Cross the durable artifact barrier before a dependent model may start."""

    artifact = build_planning_evidence_artifact(task, result)
    if artifact is None:
        return result
    phase = task.planning_phase
    assert phase is not None
    task_id = (context.task_ids_by_name or {}).get(task.task_name)
    if coordination_command is None or not task_id:
        raise PlanningContractError(f"{phase.value} requires a coordination-backed task artifact")
    submitted = coordination_command(
        SubmitArtifact(
            pow_wow_id=pow_wow_id,
            artifact_type=artifact.artifact_type,
            content=json.dumps(artifact.to_payload(), indent=2, sort_keys=True),
            schema_version=artifact.schema_version,
            task_id=task_id,
        )
    )
    if not isinstance(submitted, AcknowledgementResult):
        raise PlanningContractError("planning evidence persistence returned malformed result")
    artifact_id = submitted.payload.values.get("artifact_id")
    if not artifact_id:
        raise PlanningContractError("planning evidence persistence returned no artifact id")
    durable = replace(
        artifact,
        persisted_artifact_id=str(artifact_id),
    )
    return replace(result, artifacts=(*result.artifacts, durable))


def persist_repo_audit(
    *,
    pow_wow_id: str,
    task: PowWowTaskSpec,
    result: PowWowTaskResult,
    context: PowWowExecutionContext,
    resolve_head_sha: Callable[[], str],
    coordination_command: CoordinationCommandFn | None,
) -> PowWowTaskResult:
    """Persist a completed reading's repository audit beside its evidence.

    Unlike planning evidence, an audit accelerates the next agent rather than
    gating this one, so every failure here degrades to a risk note on the
    completed result instead of an exception: a malformed block, an
    unresolvable HEAD, or a persistence refusal must not retroactively fail a
    reading that succeeded. A missing block changes nothing at all, which is
    why the block's presence is checked before the host spends a git call on
    the commit sha.
    """

    phase = task.planning_phase
    if phase is None or phase not in AUDIT_PRODUCER_PHASES or result.status != "completed":
        return result
    output = _extract_planning_model_output(result)
    if not contains_repo_audit_block(output):
        return result

    def _with_risk(note: str) -> PowWowTaskResult:
        return replace(result, risks=(*result.risks, note))

    try:
        head_sha = resolve_head_sha().strip()
    except Exception as exc:  # noqa: BLE001 - audit is acceleration, not a gate
        return _with_risk(f"repo audit not persisted: HEAD is unresolvable: {exc}")
    try:
        audit = extract_repo_audit(
            output,
            target_project_id=context.target_project_id,
            commit_sha=head_sha,
        )
    except RepoAuditError as exc:
        return _with_risk(f"repo_audit.v1 block is malformed and was not persisted: {exc}")
    if audit is None:
        return result
    content = {
        **audit.to_payload(),
        "phase": phase.value,
        "tier": _PHASE_TIER[phase].value,
        "task_name": task.task_name,
    }
    task_id = (context.task_ids_by_name or {}).get(task.task_name)
    if coordination_command is None or not task_id:
        return _with_risk("repo audit not persisted: no coordination-backed task to attach it to")
    try:
        submitted = coordination_command(
            SubmitArtifact(
                pow_wow_id=pow_wow_id,
                artifact_type=REPO_AUDIT_ARTIFACT_TYPE,
                content=json.dumps(content, indent=2, sort_keys=True),
                schema_version=REPO_AUDIT_SCHEMA_VERSION,
                task_id=task_id,
            )
        )
        artifact_id = (
            submitted.payload.values.get("artifact_id")
            if isinstance(submitted, AcknowledgementResult)
            else None
        )
        if not artifact_id:
            raise RepoAuditError("persistence returned no artifact id")
    except Exception as exc:  # noqa: BLE001 - audit is acceleration, not a gate
        return _with_risk(f"repo audit persistence failed: {exc}")
    durable = PowWowArtifact(
        artifact_type=REPO_AUDIT_ARTIFACT_TYPE,
        schema_version=REPO_AUDIT_SCHEMA_VERSION,
        task_name=task.task_name,
        content=content,
        persisted_artifact_id=str(artifact_id),
    )
    return replace(result, artifacts=(*result.artifacts, durable))


def _extract_planning_model_output(result: PowWowTaskResult) -> str:
    for artifact in reversed(result.artifacts):
        content: Mapping[str, object] = artifact.content
        if artifact.artifact_type == DELEGATED_TASK_RUN_ARTIFACT_TYPE:
            output = content.get("output")
        elif artifact.artifact_type in {
            CLI_AGENT_RUN_ARTIFACT_TYPE,
            FRONTIER_FALLBACK_RUN_ARTIFACT_TYPE,
        }:
            output = content.get("verdict") or content.get("output")
        else:
            continue
        if output is not None:
            return str(output)
    return ""


__all__ = [
    "AUDIT_PRODUCER_PHASES",
    "PlanningContractError",
    "SCHEMA_VERSION_PLANNING_EVIDENCE",
    "audit_consumer_tier",
    "build_planning_evidence_artifact",
    "persist_planning_evidence",
    "persist_repo_audit",
    "validate_planning_visibility_contract",
]
