# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Adopt reviewed or already-integrated work into a blocked WorkUnit milestone."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..contracts import (
    ApprovalStatus,
    DispatchIntentStatus,
    DispatchProgress,
    classify_dispatch_progress,
)
from ..coordination.outcomes import (
    DispatchPromotionState,
    DispatchResultOrigin,
    DispatchResultState,
)
from ..coordination.store import connect, decode_json_object, rowdict
from ..project_center import LinkedProject, load_project_center
from ..review_recovery import diagnose_staff_review_provenance
from ..settings import Settings, get_settings
from . import repository as repo
from .events import ArtifactKind, MilestoneTransition, RequirableArtifact
from .execution import (
    DispatchBackedExecutorRuntime,
    MilestoneContext,
    MilestoneFailed,
    MilestoneSucceeded,
    _agent_evidence,
    evidence_artifact,
)
from .lifecycle import MilestoneExecutionStatus

# Public because the doctrine staleness scan also has to say which WorkUnit
# owns a stale dispatch, and a second copy of this pattern would drift.
WORK_UNIT_DISPATCH_SOURCE = re.compile(
    r"^work_unit:(?P<work_unit_id>[^:]+):milestone_execution:(?P<milestone_key>[^:]+)$"
)


class DispatchAdoptionRefused(ValueError):
    """The recovered commit has not crossed every boundary required for adoption."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DispatchAdoption:
    work_unit_id: str
    milestone_key: str
    attempt: int
    approval_id: str
    commit_sha: str
    applied: bool


@dataclass(frozen=True)
class IntegratedMilestoneAdoption:
    """An operator attestation that an ancestor commit already satisfies a milestone."""

    work_unit_id: str
    milestone_key: str
    attempt: int
    commit_sha: str
    accepted_by: str
    applied: bool


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> Sequence[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _typed_artifacts(run_result: Mapping[str, Any], artifact_type: str) -> list[Mapping[str, Any]]:
    return [
        _mapping(artifact)
        for task in _sequence(run_result.get("tasks"))
        for artifact in _sequence(_mapping(task).get("artifacts"))
        if _mapping(artifact).get("artifact_type") == artifact_type
    ]


def adopt_recovered_dispatch(
    intent_id: str,
    *,
    settings: Settings | None = None,
) -> DispatchAdoption:
    """Complete a blocked milestone from an approved and integrated recovery."""

    settings = settings or get_settings()
    intent = _dispatch_intent(intent_id)
    match = WORK_UNIT_DISPATCH_SOURCE.fullmatch(str(intent.get("source") or ""))
    if match is None:
        raise DispatchAdoptionRefused(
            "dispatch_not_owned_by_work_unit",
            "dispatch source does not name a WorkUnit milestone",
        )
    approval = _approved_recovery(intent_id)
    payload = _mapping(approval.get("payload"))
    dispatch_result = _mapping(payload.get("dispatch_result"))
    if (
        dispatch_result.get("recovered_from_intent_id") != intent_id
        or dispatch_result.get("result_origin") != DispatchResultOrigin.AUTOMATED_RECOVERY.value
        or dispatch_result.get("result_state") != DispatchResultState.COMPLETED.value
        or dispatch_result.get("promotion_state") != DispatchPromotionState.MERGE_PENDING.value
    ):
        raise DispatchAdoptionRefused(
            "recovered_dispatch_contract_invalid",
            "approved recovery does not name this intent at the merge-pending boundary",
        )
    run_result = _mapping(dispatch_result.get("run_result"))
    checkpoints = _typed_artifacts(run_result, "worktree_commit_checkpoint")
    reviews = _typed_artifacts(run_result, "review_result")
    if not checkpoints or not reviews:
        raise DispatchAdoptionRefused(
            "recovered_evidence_incomplete",
            "approved recovery lacks a checkpoint or typed review",
        )
    checkpoint = _mapping(checkpoints[-1].get("content"))
    review_contents = [_mapping(review.get("content")) for review in reviews]
    issue = diagnose_staff_review_provenance(review_contents[-1], review_contents[:-1], checkpoint)
    if issue is not None:
        raise DispatchAdoptionRefused(
            "recovered_review_provenance_invalid",
            "approved recovery does not prove an exact host-stamped staff approval: "
            f"{issue.code.value}: {issue.message}",
        )
    commit_sha = str(payload.get("commit_sha") or "").strip()
    base_sha = str(payload.get("base_sha") or "").strip()
    target_project_id = str(payload.get("target_project_id") or "").strip()
    if (
        commit_sha != checkpoint.get("commit_sha")
        or base_sha != checkpoint.get("base_head_sha")
        or target_project_id != intent.get("target_project_id")
    ):
        raise DispatchAdoptionRefused(
            "recovered_dispatch_subject_mismatch",
            "approval target, base, or commit differs from the recovered dispatch checkpoint",
        )
    project = load_project_center(settings).project_by_id(target_project_id)
    if not _integrated_branch_contains(project, commit_sha):
        raise DispatchAdoptionRefused(
            "approved_commit_not_integrated",
            f"{target_project_id}/{project.integrated_branch} does not contain {commit_sha}",
        )

    work_unit_id = match.group("work_unit_id")
    milestone_key = match.group("milestone_key")
    unit = repo.get_work_unit(work_unit_id)
    milestone = next(
        (
            item
            for item in repo.list_milestone_executions(work_unit_id)
            if item.stable_key == milestone_key
        ),
        None,
    )
    if milestone is None:
        raise DispatchAdoptionRefused(
            "work_unit_milestone_missing",
            f"work unit {work_unit_id} has no milestone {milestone_key}",
        )
    recoverable_statuses = {
        MilestoneExecutionStatus.BLOCKED,
        MilestoneExecutionStatus.READY,
        MilestoneExecutionStatus.RUNNING,
        MilestoneExecutionStatus.SUCCEEDED,
    }
    if milestone.status not in recoverable_statuses:
        raise DispatchAdoptionRefused(
            "work_unit_milestone_not_blocked",
            f"milestone {milestone_key} is {milestone.status.value}, not BLOCKED",
        )
    if milestone.status in {
        MilestoneExecutionStatus.READY,
        MilestoneExecutionStatus.RUNNING,
    } and not _adoption_started(work_unit_id, milestone_key, intent_id, milestone.attempt):
        raise DispatchAdoptionRefused(
            "work_unit_milestone_owned_by_another_execution",
            f"milestone {milestone_key} is already {milestone.status.value} for another execution",
        )

    plan_revision = repo.get_compiled_plan_revision(unit.compiled_plan_revision_id)
    compiled_milestone = plan_revision.plan.milestone(milestone_key)
    attempt = (
        milestone.attempt + 1
        if milestone.status is MilestoneExecutionStatus.BLOCKED
        else (milestone.attempt)
    )
    child_workflow_id = f"dispatch-adoption:{approval['approval_id']}"
    context = MilestoneContext(
        work_unit_id=work_unit_id,
        root_workflow_id=unit.root_workflow_id,
        child_workflow_id=child_workflow_id,
        milestone=compiled_milestone,
        attempt=attempt,
        design_doc_revision_id=unit.design_doc_revision_id,
        compiled_plan_hash=unit.compiled_plan_hash,
        document_context=plan_revision.plan.document_context,
        target_project_id=plan_revision.plan.target_project_id,
    )
    artifacts = []
    missing = []
    for required in compiled_milestone.required_artifacts:
        kind = ArtifactKind(required)
        content = _agent_evidence(kind, run_result)
        if content is None:
            missing.append(kind.value)
            continue
        artifacts.append(
            evidence_artifact(
                context,
                RequirableArtifact(kind),
                content,
                step_name=f"dispatch-adoption:{intent_id}",
                metadata={
                    "source_dispatch_intent_id": intent_id,
                    "approval_id": approval["approval_id"],
                    "integrated_commit_sha": commit_sha,
                    "recovery": True,
                },
            )
        )
    if missing:
        raise DispatchAdoptionRefused(
            "recovered_dispatch_missing_required_artifacts",
            "recovered dispatch does not prove: " + ", ".join(sorted(missing)),
        )

    if milestone.status is MilestoneExecutionStatus.BLOCKED:
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=milestone.phase,
                milestone_key=milestone_key,
                status=MilestoneExecutionStatus.READY,
                attempt=attempt,
                payload={"recovered_from_dispatch_intent_id": intent_id},
            ),
        )
    if milestone.status in {
        MilestoneExecutionStatus.BLOCKED,
        MilestoneExecutionStatus.READY,
    }:
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=milestone.phase,
                milestone_key=milestone_key,
                status=MilestoneExecutionStatus.RUNNING,
                attempt=attempt,
                child_workflow_id=child_workflow_id,
                dispatch_intent_id=intent_id,
                payload={"recovered_from_dispatch_intent_id": intent_id},
            ),
            child_workflow_id=child_workflow_id,
        )
    outcome = repo.record_fact(
        work_unit_id,
        MilestoneTransition(
            phase=milestone.phase,
            milestone_key=milestone_key,
            status=MilestoneExecutionStatus.SUCCEEDED,
            attempt=attempt,
            child_workflow_id=child_workflow_id,
            dispatch_intent_id=intent_id,
            result_summary=(
                f"adopted approved dispatch {intent_id} after exact commit integration"
            ),
            artifacts=tuple(artifacts),
            payload={
                "recovered_from_dispatch_intent_id": intent_id,
                "approval_id": approval["approval_id"],
                "integrated_commit_sha": commit_sha,
            },
        ),
        child_workflow_id=child_workflow_id,
    )
    return DispatchAdoption(
        work_unit_id=work_unit_id,
        milestone_key=milestone_key,
        attempt=attempt,
        approval_id=str(approval["approval_id"]),
        commit_sha=commit_sha,
        applied=outcome.applied,
    )


def adopt_integrated_milestone(
    work_unit_id: str,
    milestone_key: str,
    commit: str,
    *,
    accepted_by: str,
    acceptance_evidence: str,
    settings: Settings | None = None,
) -> IntegratedMilestoneAdoption:
    """Attest that an integrated ancestor commit already satisfies blocked work.

    This is deliberately narrower than a general status setter. It is available
    only after a provider usage limit blocked an implementation milestone, only
    for a non-empty source-patch requirement, and only when the named commit is
    already an ancestor of the target project's integrated branch. The operator
    supplies the acceptance argument, while git supplies the immutable subject.
    """

    actor = accepted_by.strip()
    evidence = acceptance_evidence.strip()
    if not actor or not evidence:
        raise DispatchAdoptionRefused(
            "integrated_adoption_attestation_missing",
            "accepted_by and acceptance_evidence are both required",
        )
    unit = repo.get_work_unit(work_unit_id)
    milestone = next(
        (
            item
            for item in repo.list_milestone_executions(work_unit_id)
            if item.stable_key == milestone_key
        ),
        None,
    )
    if milestone is None:
        raise DispatchAdoptionRefused(
            "work_unit_milestone_missing",
            f"work unit {work_unit_id} has no milestone {milestone_key}",
        )
    previous = _integrated_adoption_event(work_unit_id, milestone_key)
    if milestone.status is MilestoneExecutionStatus.SUCCEEDED and previous is not None:
        adopted_sha = str(previous.payload.get("integrated_commit_sha") or "")
        resolved_input = _resolve_commit(
            load_project_center(settings or get_settings()).project_by_id(
                repo.get_compiled_plan_revision(
                    unit.compiled_plan_revision_id
                ).plan.target_project_id
            ),
            commit,
        )
        if adopted_sha != resolved_input:
            raise DispatchAdoptionRefused(
                "integrated_adoption_conflict",
                f"milestone {milestone_key} was already adopted from {adopted_sha}",
            )
        return IntegratedMilestoneAdoption(
            work_unit_id=work_unit_id,
            milestone_key=milestone_key,
            attempt=milestone.attempt,
            commit_sha=adopted_sha,
            accepted_by=str(previous.payload["accepted_by"]),
            applied=False,
        )
    if milestone.status is not MilestoneExecutionStatus.BLOCKED:
        raise DispatchAdoptionRefused(
            "work_unit_milestone_not_blocked",
            f"milestone {milestone_key} is {milestone.status.value}, not BLOCKED",
        )
    if milestone.failure_code != "USAGE_LIMIT":
        raise DispatchAdoptionRefused(
            "integrated_adoption_not_provider_blocked",
            "only a milestone blocked by USAGE_LIMIT may adopt pre-existing integrated work",
        )

    plan_revision = repo.get_compiled_plan_revision(unit.compiled_plan_revision_id)
    compiled_milestone = plan_revision.plan.milestone(milestone_key)
    if tuple(compiled_milestone.required_artifacts) != (ArtifactKind.SOURCE_PATCH.value,):
        raise DispatchAdoptionRefused(
            "integrated_adoption_artifacts_unsupported",
            "integrated commit adoption can prove exactly one required source_patch artifact",
        )
    project = load_project_center(settings or get_settings()).project_by_id(
        plan_revision.plan.target_project_id
    )
    commit_sha = _resolve_commit(project, commit)
    if not _integrated_branch_contains(project, commit_sha):
        raise DispatchAdoptionRefused(
            "approved_commit_not_integrated",
            f"{project.id}/{project.integrated_branch} does not contain {commit_sha}",
        )
    changed_files = _commit_changed_files(project, commit_sha)
    if not changed_files:
        raise DispatchAdoptionRefused(
            "integrated_commit_has_no_patch",
            f"commit {commit_sha} changes no files",
        )

    attempt = milestone.attempt + 1
    child_workflow_id = f"integrated-adoption:{commit_sha}"
    context = MilestoneContext(
        work_unit_id=work_unit_id,
        root_workflow_id=unit.root_workflow_id,
        child_workflow_id=child_workflow_id,
        milestone=compiled_milestone,
        attempt=attempt,
        design_doc_revision_id=unit.design_doc_revision_id,
        compiled_plan_hash=unit.compiled_plan_hash,
        document_context=plan_revision.plan.document_context,
        target_project_id=plan_revision.plan.target_project_id,
    )
    artifact = evidence_artifact(
        context,
        RequirableArtifact(ArtifactKind.SOURCE_PATCH),
        content=(
            f"integrated commit: {commit_sha}\n"
            f"accepted by: {actor}\n"
            f"acceptance evidence: {evidence}\n"
            "changed files:\n" + "\n".join(changed_files)
        ),
        step_name=f"integrated-adoption:{commit_sha}",
        metadata={
            "adoption_kind": "integrated_ancestor.v1",
            "accepted_by": actor,
            "acceptance_evidence": evidence,
            "integrated_commit_sha": commit_sha,
            "source_dispatch_intent_id": milestone.dispatch_intent_id,
            "changed_files": list(changed_files),
        },
    )
    shared_payload = {
        "adoption_kind": "integrated_ancestor.v1",
        "accepted_by": actor,
        "acceptance_evidence": evidence,
        "integrated_commit_sha": commit_sha,
        "provider_blocked_dispatch_intent_id": milestone.dispatch_intent_id,
    }
    repo.record_fact(
        work_unit_id,
        MilestoneTransition(
            phase=milestone.phase,
            milestone_key=milestone_key,
            status=MilestoneExecutionStatus.READY,
            attempt=attempt,
            payload=shared_payload,
        ),
    )
    repo.record_fact(
        work_unit_id,
        MilestoneTransition(
            phase=milestone.phase,
            milestone_key=milestone_key,
            status=MilestoneExecutionStatus.RUNNING,
            attempt=attempt,
            child_workflow_id=child_workflow_id,
            dispatch_intent_id=milestone.dispatch_intent_id,
            payload=shared_payload,
        ),
        child_workflow_id=child_workflow_id,
    )
    outcome = repo.record_fact(
        work_unit_id,
        MilestoneTransition(
            phase=milestone.phase,
            milestone_key=milestone_key,
            status=MilestoneExecutionStatus.SUCCEEDED,
            attempt=attempt,
            child_workflow_id=child_workflow_id,
            dispatch_intent_id=milestone.dispatch_intent_id,
            result_summary=f"operator adopted integrated ancestor commit {commit_sha}",
            artifacts=(artifact,),
            payload=shared_payload,
        ),
        child_workflow_id=child_workflow_id,
    )
    return IntegratedMilestoneAdoption(
        work_unit_id=work_unit_id,
        milestone_key=milestone_key,
        attempt=attempt,
        commit_sha=commit_sha,
        accepted_by=actor,
        applied=outcome.applied,
    )


def _integrated_adoption_event(work_unit_id: str, milestone_key: str) -> Any | None:
    return next(
        (
            event
            for event in reversed(repo.list_work_unit_events(work_unit_id, limit=10_000))
            if event.payload.get("milestone_key") == milestone_key
            and event.payload.get("adoption_kind") == "integrated_ancestor.v1"
        ),
        None,
    )


@dataclass(frozen=True)
class SettledDispatchAdoption:
    """A milestone credited with the dispatch that settled after its wait ran out."""

    work_unit_id: str
    milestone_key: str
    attempt: int
    intent_id: str
    applied: bool


def adopt_settled_dispatch(work_unit_id: str, milestone_key: str) -> SettledDispatchAdoption:
    """Credit a wait-elapsed milestone with its own dispatch, once that settled DONE.

    `dispatch_wait_elapsed` names a clock that ran out, not work that failed: the
    milestone stops waiting while the dispatch keeps running. When that dispatch
    later settles DONE, the ledger holds a complete, checkable result that no
    lifecycle state can reach - a resume mints a fresh attempt and a rival
    intent, so a milestone whose work reliably outlives its compiled bound
    re-spends the work forever and never credits it.

    This is the narrow repair. It accepts only a milestone blocked by
    `dispatch_wait_elapsed`, reads only that milestone's own intent, and only
    when the ledger says DONE. The evidence goes through the same translation
    the milestone workflow itself would have applied - a result without the
    runner payload, or without every required artifact, is refused with that
    translation's own reason rather than adopted on faith. No operator
    attestation is taken because none is substituted: unlike the integrated
    ancestor path, every fact here is machine-checkable from the immutable row.

    A still-active intent is refused rather than waited on, so this cannot
    become a second wait with a different name. A failed, cancelled, or
    superseded intent is refused because the normal retry path owns those.
    """

    unit = repo.get_work_unit(work_unit_id)
    milestone = next(
        (
            item
            for item in repo.list_milestone_executions(work_unit_id)
            if item.stable_key == milestone_key
        ),
        None,
    )
    if milestone is None:
        raise DispatchAdoptionRefused(
            "work_unit_milestone_missing",
            f"work unit {work_unit_id} has no milestone {milestone_key}",
        )
    previous = _settled_adoption_event(work_unit_id, milestone_key)
    if milestone.status is MilestoneExecutionStatus.SUCCEEDED and previous is not None:
        return SettledDispatchAdoption(
            work_unit_id=work_unit_id,
            milestone_key=milestone_key,
            attempt=milestone.attempt,
            intent_id=str(previous.payload["adopted_intent_id"]),
            applied=False,
        )
    if milestone.status is not MilestoneExecutionStatus.BLOCKED:
        raise DispatchAdoptionRefused(
            "work_unit_milestone_not_blocked",
            f"milestone {milestone_key} is {milestone.status.value}, not BLOCKED",
        )
    if milestone.failure_code != "dispatch_wait_elapsed":
        raise DispatchAdoptionRefused(
            "settled_adoption_not_wait_elapsed",
            "only a milestone blocked by dispatch_wait_elapsed may adopt its settled dispatch",
        )
    intent_id = milestone.dispatch_intent_id
    if not intent_id:
        raise DispatchAdoptionRefused(
            "settled_adoption_intent_unknown",
            f"milestone {milestone_key} records no dispatch intent to adopt",
        )
    row = _dispatch_intent(intent_id)
    status = DispatchIntentStatus(str(row["status"]))
    match classify_dispatch_progress(status):
        case DispatchProgress.ACTIVE:
            raise DispatchAdoptionRefused(
                "settled_adoption_dispatch_still_active",
                f"dispatch intent {intent_id} is {status.value}; "
                "wait for it to settle before adopting",
            )
        case DispatchProgress.PARKED:
            raise DispatchAdoptionRefused(
                "settled_adoption_dispatch_parked",
                f"dispatch intent {intent_id} is parked on a checkpoint; "
                "decide the checkpoint instead of adopting",
            )
        case DispatchProgress.SETTLED if status is not DispatchIntentStatus.DONE:
            raise DispatchAdoptionRefused(
                "settled_adoption_dispatch_not_done",
                f"dispatch intent {intent_id} settled {status.value}; "
                "the normal retry path owns settled failures",
            )

    plan_revision = repo.get_compiled_plan_revision(unit.compiled_plan_revision_id)
    compiled_milestone = plan_revision.plan.milestone(milestone_key)
    attempt = milestone.attempt + 1
    child_workflow_id = f"settled-adoption:{intent_id}"
    context = MilestoneContext(
        work_unit_id=work_unit_id,
        root_workflow_id=unit.root_workflow_id,
        child_workflow_id=child_workflow_id,
        milestone=compiled_milestone,
        attempt=attempt,
        design_doc_revision_id=unit.design_doc_revision_id,
        compiled_plan_hash=unit.compiled_plan_hash,
        document_context=plan_revision.plan.document_context,
        target_project_id=plan_revision.plan.target_project_id,
    )
    outcome = DispatchBackedExecutorRuntime()._outcome_from_settled_row(
        context, intent_id, dict(row)
    )
    match outcome:
        case MilestoneFailed():
            raise DispatchAdoptionRefused(
                outcome.failure_code,
                f"the settled dispatch does not carry adoptable evidence: "
                f"{outcome.failure_summary}",
            )
        case MilestoneSucceeded():
            pass

    shared_payload = {
        "adoption_kind": "settled_dispatch.v1",
        "adopted_intent_id": intent_id,
        "wait_elapsed_attempt": milestone.attempt,
        "wait_elapsed_summary": milestone.failure_summary,
    }
    repo.record_fact(
        work_unit_id,
        MilestoneTransition(
            phase=milestone.phase,
            milestone_key=milestone_key,
            status=MilestoneExecutionStatus.READY,
            attempt=attempt,
            payload=shared_payload,
        ),
    )
    repo.record_fact(
        work_unit_id,
        MilestoneTransition(
            phase=milestone.phase,
            milestone_key=milestone_key,
            status=MilestoneExecutionStatus.RUNNING,
            attempt=attempt,
            child_workflow_id=child_workflow_id,
            dispatch_intent_id=intent_id,
            payload=shared_payload,
        ),
        child_workflow_id=child_workflow_id,
    )
    recorded = repo.record_fact(
        work_unit_id,
        MilestoneTransition(
            phase=milestone.phase,
            milestone_key=milestone_key,
            status=MilestoneExecutionStatus.SUCCEEDED,
            attempt=attempt,
            child_workflow_id=child_workflow_id,
            dispatch_intent_id=intent_id,
            result_summary=(
                f"adopted dispatch intent {intent_id}, which settled DONE after "
                f"attempt {milestone.attempt} stopped waiting"
            ),
            artifacts=outcome.artifacts,
            payload=shared_payload,
        ),
        child_workflow_id=child_workflow_id,
    )
    return SettledDispatchAdoption(
        work_unit_id=work_unit_id,
        milestone_key=milestone_key,
        attempt=attempt,
        intent_id=intent_id,
        applied=recorded.applied,
    )


def _settled_adoption_event(work_unit_id: str, milestone_key: str) -> Any | None:
    return next(
        (
            event
            for event in reversed(repo.list_work_unit_events(work_unit_id, limit=10_000))
            if event.payload.get("milestone_key") == milestone_key
            and event.payload.get("adoption_kind") == "settled_dispatch.v1"
        ),
        None,
    )


def _resolve_commit(project: LinkedProject, commit: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(project.expanded_path), "rev-parse", f"{commit}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    resolved = completed.stdout.strip()
    if completed.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", resolved):
        raise DispatchAdoptionRefused(
            "integrated_commit_invalid",
            f"{commit!r} does not resolve to a commit in {project.id}",
        )
    return resolved


def _commit_changed_files(project: LinkedProject, commit_sha: str) -> tuple[str, ...]:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(project.expanded_path),
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-r",
            commit_sha,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise DispatchAdoptionRefused(
            "integrated_commit_unreadable",
            f"could not read changed files for {commit_sha}",
        )
    return tuple(sorted(line for line in completed.stdout.splitlines() if line))


def _adoption_started(
    work_unit_id: str,
    milestone_key: str,
    intent_id: str,
    attempt: int,
) -> bool:
    return any(
        event.payload.get("milestone_key") == milestone_key
        and event.payload.get("attempt") == attempt
        and event.payload.get("recovered_from_dispatch_intent_id") == intent_id
        for event in repo.list_work_unit_events(work_unit_id, limit=10_000)
    )


def _dispatch_intent(intent_id: str) -> Mapping[str, Any]:
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM dispatch_intents WHERE intent_id = ?", (intent_id,)
        ).fetchone()
    if row is None:
        raise DispatchAdoptionRefused("dispatch_intent_missing", f"unknown intent {intent_id}")
    return rowdict(row)


def _approved_recovery(intent_id: str) -> Mapping[str, Any]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT * FROM approval_requests WHERE request_type = 'CODE_MERGE' AND status = ?",
            (ApprovalStatus.APPROVED.value,),
        ).fetchall()
    matches = []
    for row in rows:
        item = rowdict(row)
        item["payload"] = decode_json_object(item.pop("payload_json", None))
        recovery = _mapping(_mapping(item["payload"]).get("review_recovery"))
        if recovery.get("source_intent_id") == intent_id:
            matches.append(item)
    if len(matches) != 1:
        raise DispatchAdoptionRefused(
            "approved_recovery_missing" if not matches else "approved_recovery_ambiguous",
            f"expected one approved parser recovery for {intent_id}, found {len(matches)}",
        )
    return matches[0]


def _integrated_branch_contains(project: LinkedProject, commit_sha: str) -> bool:
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        return False
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(project.expanded_path),
            "merge-base",
            "--is-ancestor",
            commit_sha,
            f"refs/heads/{project.integrated_branch}",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    return completed.returncode == 0


__all__ = [
    "WORK_UNIT_DISPATCH_SOURCE",
    "DispatchAdoption",
    "DispatchAdoptionRefused",
    "IntegratedMilestoneAdoption",
    "adopt_integrated_milestone",
    "adopt_recovered_dispatch",
]
