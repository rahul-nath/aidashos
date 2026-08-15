# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fail-closed project action projection for the operator cockpit."""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .contracts import (
    ApprovalRequestType,
    ApprovalStatus,
    CheckpointStatus,
    DispatchIntentStatus,
    LeaseStatus,
    MilestoneStatus,
    ProjectActionKind,
)
from .coordination.contracts import (
    ListApprovalRequests,
    ListDispatchIntents,
    ListExecutionCheckpoints,
    ListExecutionLeases,
    ListSagaMilestones,
    ListSagas,
)
from .pow_wow.ledger import run_coordination_command
from .project_center import load_project_center, project_status_row
from .runtime_source import runtime_revision
from .settings import Settings, get_settings

PROJECT_ACTION_SCHEMA_VERSION = "project_action_snapshot.v1"


class ExecutionKind(StrEnum):
    """Which durable row the current execution facts came from.

    The snapshot used to pass whichever row it found straight through, so a client
    had to probe for ``lease_id`` to learn which of two shapes it was holding. The
    server already knows, and this is it saying so.
    """

    LEASE = "lease"
    INTENT = "intent"


class LedgerFacts(BaseModel):
    """Base for every projection of a ledger row.

    ``extra="forbid"`` plus an explicit ``from_ledger_row`` on each subclass is
    what keeps the ledger's columns from becoming this API's fields by accident. A
    new column reaches the cockpit when someone adds it here, and not before.

    ``json_schema_serialization_defaults_required`` separates two things a default
    used to conflate: whether a caller must supply a field, and whether a response
    always contains it. A defaulted field is optional to construct and always
    present once serialized, so the published schema says required and clients stop
    handling an absence the server never produces.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_serialization_defaults_required=True,
    )


class LeaseFacts(LedgerFacts):
    """One agent execution lease, as the cockpit renders it."""

    execution_kind: Literal[ExecutionKind.LEASE] = ExecutionKind.LEASE
    lease_id: str | None = None
    intent_id: str | None = None
    status: str | None = None
    outcome: str | None = None
    activity_status: str | None = None
    agent_status: str | None = None
    agent_failure: str | None = None
    agent_failure_category: str | None = None
    supervisor_status: str | None = None
    supervisor_failure: str | None = None
    persistence_status: str | None = None
    persistence_failure: str | None = None
    progress_assessment_status: str | None = None
    progress_assessment_error: str | None = None
    next_action: str | None = None
    agent_tier: str | None = None
    agent_name: str | None = None
    worktree_path: str | None = None
    error: str | None = None

    @classmethod
    def from_ledger_row(cls, row: Mapping[str, Any]) -> LeaseFacts:
        return cls.model_validate({name: _optional_text(row.get(name)) for name in _LEASE_FIELDS})


class IntentFacts(LedgerFacts):
    """One dispatch intent, for the window before any lease exists.

    Deliberately smaller than ``LeaseFacts``. An intent has no agent, no
    supervisor, and no persistence lane, and the previous flattened shape made the
    cockpit render four empty rows claiming otherwise.
    """

    execution_kind: Literal[ExecutionKind.INTENT] = ExecutionKind.INTENT
    intent_id: str | None = None
    status: str | None = None
    outcome: str | None = None
    tier: str | None = None
    kind: str | None = None
    error: str | None = None

    @classmethod
    def from_ledger_row(cls, row: Mapping[str, Any]) -> IntentFacts:
        return cls.model_validate({name: _optional_text(row.get(name)) for name in _INTENT_FIELDS})


ExecutionFacts = Annotated[LeaseFacts | IntentFacts, Field(discriminator="execution_kind")]
"""One of two execution shapes, named rather than merged.

A product type with every field optional cannot say which fields belong together;
a discriminated sum can. This is the type the cockpit switches on.
"""


class CheckpointFacts(LedgerFacts):
    checkpoint_id: str | None = None
    status: str | None = None
    reason: str | None = None
    error: str | None = None
    worktree_path: str | None = None
    base_head_sha: str | None = None

    @classmethod
    def from_ledger_row(cls, row: Mapping[str, Any]) -> CheckpointFacts:
        return cls.model_validate(
            {name: _optional_text(row.get(name)) for name in _CHECKPOINT_FIELDS}
        )


class ApprovalFacts(LedgerFacts):
    approval_id: str | None = None
    request_type: str | None = None
    status: str | None = None

    @classmethod
    def from_ledger_row(cls, row: Mapping[str, Any]) -> ApprovalFacts:
        return cls.model_validate(
            {name: _optional_text(row.get(name)) for name in _APPROVAL_FIELDS}
        )


class SagaFacts(LedgerFacts):
    saga_id: str | None = None
    status: str | None = None
    current_stage: str | None = None

    @classmethod
    def from_ledger_row(cls, row: Mapping[str, Any]) -> SagaFacts:
        return cls.model_validate({name: _optional_text(row.get(name)) for name in _SAGA_FIELDS})


class MilestoneFacts(LedgerFacts):
    milestone_id: str | None = None
    name: str | None = None
    status: str | None = None
    sequence: int | None = None

    @classmethod
    def from_ledger_row(cls, row: Mapping[str, Any]) -> MilestoneFacts:
        sequence = row.get("sequence")
        return cls(
            milestone_id=_optional_text(row.get("milestone_id")),
            name=_optional_text(row.get("name")),
            status=_optional_text(row.get("status")),
            sequence=int(sequence) if isinstance(sequence, int | str) and str(sequence) else None,
        )


class ProjectFacts(LedgerFacts):
    """The registered project, including the two facts the action logic gates on."""

    id: str
    path: str | None = None
    status: str | None = None
    branch: str | None = None
    head_sha: str | None = None
    exists: bool = False
    git_repo: bool = False

    @classmethod
    def from_project_row(cls, row: Mapping[str, Any]) -> ProjectFacts:
        return cls(
            id=str(row.get("id") or ""),
            path=_optional_text(row.get("path")),
            status=_optional_text(row.get("status")),
            branch=_optional_text(row.get("branch")),
            head_sha=_optional_text(row.get("head_sha")),
            exists=bool(row.get("exists")),
            git_repo=bool(row.get("git_repo")),
        )


class RuntimeFacts(LedgerFacts):
    status: str
    application_version: str
    coordination_backend: str
    revision: str | None = None


_LEASE_FIELDS: tuple[str, ...] = (
    "lease_id",
    "intent_id",
    "status",
    "outcome",
    "activity_status",
    "agent_status",
    "agent_failure",
    "agent_failure_category",
    "supervisor_status",
    "supervisor_failure",
    "persistence_status",
    "persistence_failure",
    "progress_assessment_status",
    "progress_assessment_error",
    "next_action",
    "agent_tier",
    "agent_name",
    "worktree_path",
    "error",
)
_INTENT_FIELDS: tuple[str, ...] = ("intent_id", "status", "outcome", "tier", "kind", "error")
_CHECKPOINT_FIELDS: tuple[str, ...] = (
    "checkpoint_id",
    "status",
    "reason",
    "error",
    "worktree_path",
    "base_head_sha",
)
_APPROVAL_FIELDS: tuple[str, ...] = ("approval_id", "request_type", "status")
_SAGA_FIELDS: tuple[str, ...] = ("saga_id", "status", "current_stage")


def _optional_text(value: Any) -> str | None:
    """Normalize a ledger cell to text, treating an empty cell as absent.

    Ledger rows arrive from two backends and carry numbers, booleans, and empty
    strings in columns the cockpit renders as text. Deciding that here means no
    panel has to.
    """

    if value is None:
        return None
    text = str(value).strip()
    return text or None


class ProjectActionSnapshot(BaseModel):
    """One deterministic, versioned answer to: what should the operator do next?"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_serialization_defaults_required=True,
    )

    schema_version: str = PROJECT_ACTION_SCHEMA_VERSION
    generated_at: datetime
    freshness_seconds: int = Field(ge=0)
    action: ProjectActionKind
    summary: str
    next_command: str | None = None
    runtime: RuntimeFacts
    project: ProjectFacts
    saga: SagaFacts | None = None
    milestone: MilestoneFacts | None = None
    execution: ExecutionFacts | None = None
    checkpoint: CheckpointFacts | None = None
    # Model output rather than a ledger row: these carry whatever a verification or
    # review step reported, so there is no column list to name. They stay open on
    # purpose, and the cockpit treats them as opaque.
    verification: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    approval: ApprovalFacts | None = None
    warnings: list[str] = Field(default_factory=list)
    source_ids: dict[str, list[str]] = Field(default_factory=dict)


class ProjectActionSource(Protocol):
    def read_project_action_facts(self, project_id: str) -> Mapping[str, Any]: ...


class LedgerProjectActionSource:
    """Read the authoritative owners without introducing a second state store."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def _execute_coordination_command(self, command: Any) -> dict[str, Any]:
        return run_coordination_command(command, timeout=15, settings=self.settings)

    def read_project_action_facts(self, project_id: str) -> Mapping[str, Any]:
        center = load_project_center(self.settings)
        project = center.project_by_id(project_id)
        return {
            "project": project_status_row(project, include_git=True),
            "sagas": self._execute_coordination_command(ListSagas()).get("sagas", []),
            "intents": self._execute_coordination_command(ListDispatchIntents()).get("intents", []),
            "leases": self._execute_coordination_command(ListExecutionLeases()).get("leases", []),
            "checkpoints": self._execute_coordination_command(ListExecutionCheckpoints()).get(
                "checkpoints", []
            ),
            "approvals": self._execute_coordination_command(ListApprovalRequests()).get(
                "requests", []
            ),
            "milestones_for": lambda saga_id: self._execute_coordination_command(
                ListSagaMilestones(saga_id)
            ).get("milestones", []),
        }


def _parse_record_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _parse_json_object(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _merge_already_landed(
    approval: Mapping[str, Any],
    approval_status: ApprovalStatus | None,
    project: Mapping[str, Any],
) -> bool:
    """Whether this CODE_MERGE's approved commit is already the project HEAD.

    Its own predicate because it decides whether the merge branch runs at all,
    rather than what the branch concludes. An approved commit that has landed
    needs no merge action, and the branch has nothing else to say about it: it
    used to be entered, match no inner case, and fall out carrying the chain's
    initial "an operator decision is required" with no command attached at all.

    What is genuinely outstanding in that state is the ledger transition, not a
    git one, and `workflow/engine.py` already detects a contained approved commit
    and names the exact `complete_saga_milestone` call. So this state belongs to
    the approved-GAWD fall-through below, and saying so here is what lets it get
    there.
    """

    if approval_status is not ApprovalStatus.APPROVED:
        return False
    commit_sha = str(_parse_json_object(approval.get("payload")).get("commit_sha") or "")
    return bool(commit_sha) and commit_sha == str(project.get("head_sha") or "")


def _select_latest_record(rows: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    return max(
        rows,
        key=lambda row: str(
            row.get("updated_at") or row.get("completed_at") or row.get("created_at") or ""
        ),
        default=None,
    )


def _collect_project_saga_ids(
    project: Mapping[str, Any],
    intents: Sequence[dict[str, Any]],
    checkpoints: Sequence[dict[str, Any]],
    approvals: Sequence[dict[str, Any]],
) -> set[str]:
    ids: set[str] = set()
    project_id = str(project.get("id") or "")
    project_path = str(project.get("path") or "")
    for intent in intents:
        if intent.get("target_project_id") != project_id:
            continue
        prompt = _parse_json_object(intent.get("prompt"))
        for value in (prompt.get("saga_id"), intent.get("saga_id")):
            if value:
                ids.add(str(value))
        source = str(intent.get("source") or "")
        marker = ":milestone:"
        if marker in source:
            milestone_id = source.split(marker, 1)[1]
            if ":m" in milestone_id:
                ids.add(milestone_id.rsplit(":m", 1)[0])
    for checkpoint in checkpoints:
        if str(checkpoint.get("source_repo_path") or "") == project_path and checkpoint.get(
            "saga_id"
        ):
            ids.add(str(checkpoint["saga_id"]))
    for approval in approvals:
        payload = _parse_json_object(approval.get("payload"))
        if payload.get("target_project_id") == project_id and approval.get("saga_id"):
            ids.add(str(approval["saga_id"]))
    return ids


def _select_saga(
    project: Mapping[str, Any],
    sagas: Sequence[dict[str, Any]],
    intents: Sequence[dict[str, Any]],
    checkpoints: Sequence[dict[str, Any]],
    approvals: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    candidates = _collect_project_saga_ids(project, intents, checkpoints, approvals)
    matched = [saga for saga in sagas if str(saga.get("saga_id")) in candidates]
    # Product sagas carry the GAWD attachment. Dispatch-execution sagas do not.
    product = [saga for saga in matched if saga.get("gawd_doc_id")]
    return _select_latest_record(product or matched)


def _milestone_status_or_none(row: Mapping[str, Any]) -> MilestoneStatus | None:
    """A milestone's status as its enum, or ``None`` when it is not one.

    Selection has no warning channel, so it takes the conservative reading: a
    status it cannot parse is not settled, and the milestone stays in play rather
    than being silently treated as finished.
    """

    raw = row.get("status")
    try:
        return MilestoneStatus(str(raw))
    except ValueError:
        return None


def _parse_row_status[StatusT: StrEnum](
    row: Mapping[str, Any] | None,
    status_enum: type[StatusT],
    *,
    label: str,
    unrecognized: list[str],
) -> StatusT | None:
    """Parse one ledger status into its enum, recording anything it cannot.

    A status this runtime does not know is not a status. It means the ledger and
    this projection disagree about the vocabulary, and the honest response is to
    say so and block. Comparing raw strings instead let an unknown value fall
    through the decision chain to whatever the last branch happened to be, so the
    cockpit would show a confident next command derived from a value nobody
    understood. A missing status is the same hazard by a different route.

    Returning ``None`` keeps the caller's branches readable: a status that could
    not be parsed matches no enum member, so every comparison below is false and
    the recorded warning is what decides the outcome.
    """

    if row is None:
        return None
    raw = row.get("status")
    if raw is None or not str(raw).strip():
        unrecognized.append(f"{label} carries no status.")
        return None
    try:
        return status_enum(str(raw))
    except ValueError:
        unrecognized.append(
            f"{label} carries unrecognized status {str(raw)!r}; "
            f"known values are {', '.join(member.value for member in status_enum)}."
        )
        return None


def _select_action_milestone(milestones: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
    settled = {MilestoneStatus.COMPLETED, MilestoneStatus.CANCELED}
    unresolved = [item for item in milestones if _milestone_status_or_none(item) not in settled]
    if unresolved:
        return min(
            unresolved,
            key=lambda item: (int(item.get("sequence") or 0), str(item.get("milestone_id"))),
        )
    return max(
        milestones,
        key=lambda item: (int(item.get("sequence") or 0), str(item.get("milestone_id"))),
        default=None,
    )


def _intent_matches_milestone(intent: Mapping[str, Any], milestone_id: str) -> bool:
    if milestone_id in str(intent.get("source") or ""):
        return True
    prompt = _parse_json_object(intent.get("prompt"))
    return str(prompt.get("milestone_id") or "") == milestone_id


def _read_runtime_revision() -> str | None:
    # This provenance answers "which code produced this record", which is the
    # same question `pi-daemon` publishes on `/health`. One owner, so the two
    # cannot disagree about the same execution.
    return runtime_revision()


def _execution_facts(
    lease: Mapping[str, Any] | None,
    intent: Mapping[str, Any] | None,
) -> LeaseFacts | IntentFacts | None:
    """Name which execution shape this is, instead of merging two into one bag.

    A lease supersedes its intent because it is the later and more specific fact
    about the same work. When no lease exists yet the intent is the whole truth,
    and saying so lets the cockpit render the smaller panel that is actually
    accurate rather than a wide one full of blanks.
    """

    if lease:
        return LeaseFacts.from_ledger_row(lease)
    if intent:
        return IntentFacts.from_ledger_row(intent)
    return None


def _build_project_action_snapshot(
    project_id: str,
    facts: Mapping[str, Any],
    *,
    settings: Settings,
    generated_at: datetime,
) -> ProjectActionSnapshot:
    project = dict(facts.get("project") or {})
    if project.get("id") != project_id:
        raise ValueError(f"Project action source returned the wrong project: {project!r}")
    sagas = _parse_record_list(facts.get("sagas"))
    intents = _parse_record_list(facts.get("intents"))
    leases = _parse_record_list(facts.get("leases"))
    checkpoints = _parse_record_list(facts.get("checkpoints"))
    approvals = _parse_record_list(facts.get("approvals"))
    saga = _select_saga(project, sagas, intents, checkpoints, approvals)
    milestones: list[dict[str, Any]] = []
    if saga is not None:
        loader = facts.get("milestones_for")
        milestones = (
            _parse_record_list(loader(str(saga["saga_id"])))
            if callable(loader)
            else _parse_record_list(facts.get("milestones"))
        )
    milestone = _select_action_milestone(milestones)
    milestone_id = str((milestone or {}).get("milestone_id") or "")

    project_intents = [
        intent for intent in intents if intent.get("target_project_id") == project_id
    ]
    current_intents = (
        [intent for intent in project_intents if _intent_matches_milestone(intent, milestone_id)]
        if milestone_id
        else project_intents
    )
    intent = _select_latest_record(current_intents)
    intent_id = str((intent or {}).get("intent_id") or "")
    lease = (
        _select_latest_record(
            [row for row in leases if str(row.get("intent_id") or "") == intent_id]
        )
        if intent_id
        else None
    )
    checkpoint = (
        _select_latest_record(
            [row for row in checkpoints if str(row.get("intent_id") or "") == intent_id]
        )
        if intent_id
        else None
    )
    saga_id = str((saga or {}).get("saga_id") or "")
    saga_approvals = [row for row in approvals if str(row.get("saga_id") or "") == saga_id]
    relevant_approvals = []
    for approval in saga_approvals:
        payload = _parse_json_object(approval.get("payload"))
        if payload.get("target_project_id") not in {None, project_id}:
            continue
        if milestone_id and payload.get("milestone_id") != milestone_id:
            continue
        relevant_approvals.append(approval)
    approval = _select_latest_record(relevant_approvals)

    warnings: list[str] = []
    action = ProjectActionKind.HUMAN_DECISION_REQUIRED
    summary = "The next durable project transition requires an operator decision."
    next_command: str | None = None

    # Statuses stop being strings here, once, before any branch reads one. Every
    # comparison below is then between enum members, so a renamed member follows
    # the code instead of leaving a literal quietly matching nothing.
    unrecognized: list[str] = []
    lease_status = _parse_row_status(
        lease, LeaseStatus, label="The execution lease", unrecognized=unrecognized
    )
    intent_status = _parse_row_status(
        intent, DispatchIntentStatus, label="The dispatch intent", unrecognized=unrecognized
    )
    checkpoint_status = _parse_row_status(
        checkpoint, CheckpointStatus, label="The execution checkpoint", unrecognized=unrecognized
    )
    approval_status = _parse_row_status(
        approval, ApprovalStatus, label="The approval request", unrecognized=unrecognized
    )
    milestone_status = _parse_row_status(
        milestone, MilestoneStatus, label="The current milestone", unrecognized=unrecognized
    )
    milestone_statuses = [
        _parse_row_status(
            item,
            MilestoneStatus,
            label=f"Milestone {item.get('milestone_id')}",
            unrecognized=unrecognized,
        )
        for item in milestones
    ]

    if not project.get("exists"):
        action = ProjectActionKind.BLOCKED
        summary = "The registered project path does not exist."
    elif not project.get("git_repo"):
        action = ProjectActionKind.BLOCKED
        summary = "The registered project path is not a Git repository."
    elif project.get("git_error"):
        action = ProjectActionKind.BLOCKED
        summary = "Git state could not be read; the cockpit refuses a partial action."
        warnings.append(str(project["git_error"]))
    elif saga is None:
        action = ProjectActionKind.BLOCKED
        summary = "No durable product saga could be resolved for this project."
        warnings.append("Create or attach an approved GAWD saga before dispatching work.")
    elif lease is not None and lease_status in {LeaseStatus.ACTIVE, LeaseStatus.CANCEL_REQUESTED}:
        activity = str(lease.get("activity_status") or "").upper()
        if any(token in activity for token in ("MODEL", "WAIT", "SPAWN")):
            action = ProjectActionKind.WAITING_FOR_MODEL
            summary = "Execution is active and currently waiting at a model boundary."
        else:
            action = ProjectActionKind.WORKING
            summary = "Execution is active in the isolated worktree."
        next_command = f"pi /project-status {project_id}"
    elif checkpoint_status in {CheckpointStatus.PAUSED, CheckpointStatus.FAILED}:
        action = ProjectActionKind.RECOVERABLE_FAILURE
        summary = "Execution stopped with a durable checkpoint available for recovery."
        next_command = "pi /ledger list_execution_checkpoints"
    elif (
        approval
        and str(approval.get("request_type")) == ApprovalRequestType.CODE_MERGE.value
        and not _merge_already_landed(approval, approval_status, project)
    ):
        approval_id = str(approval.get("approval_id") or "")
        if approval_status is ApprovalStatus.PENDING:
            action = ProjectActionKind.MERGE_APPROVAL_REQUIRED
            summary = "Staff review passed; the exact commit still needs merge approval."
            next_command = f"pi /review-merge {approval_id}"
        elif approval_status is ApprovalStatus.APPROVED:
            # Not `pi /approve-merge`, which is what this named for a long time.
            # That directive goes through `pending_code_merge_approval`, which
            # lists approvals with status PENDING and raises when there are none,
            # so in this state - approved, not landed - the command the cockpit
            # printed could only ever fail. A next action that cannot run is what
            # `LifecycleInvariantViolation.INVALID_VISIBLE_NEXT_ACTION` is for.
            #
            # The action here is the fast-forward itself, composed the same way
            # `workflow/engine.py` composes it for the approved-GAWD path. When
            # the refinery's runner lands (milestone 4 of
            # docs/refinery_integration_queue_design.md) this stops being an
            # operator action at all and becomes a state the queue is about to
            # resolve on its own.
            payload = _parse_json_object(approval.get("payload"))
            commit_sha = str(payload.get("commit_sha") or "")
            project_path = str(project.get("path") or "")
            if commit_sha and project_path:
                action = ProjectActionKind.MERGE_INTEGRATION_REQUIRED
                summary = (
                    "Merge is approved, but the approved commit is not the project HEAD. "
                    "Fast-forward it, then record the milestone completion."
                )
                next_command = shlex.join(
                    ["git", "-C", project_path, "merge", "--ff-only", commit_sha]
                )
            else:
                # An approved merge naming no commit, or a project with no
                # readable path, leaves nothing to compose. Enqueue refuses to
                # queue such an approval now, so this is unreachable from
                # anything submitted since; a row that predates the refusal still
                # gets a refusal rather than a confident empty answer.
                action = ProjectActionKind.BLOCKED
                summary = "An approved merge cannot be turned into an action the operator can run."
                warnings.append(
                    f"Approval {approval_id} is APPROVED but names no mergeable commit "
                    f"for {project_id}."
                )
        elif approval_status is ApprovalStatus.REVOKED:
            action = ProjectActionKind.BLOCKED
            summary = "The approval was revoked; the gated action must not proceed."
            warnings.append(f"Approval {approval_id} is REVOKED.")
    elif intent is not None and intent_status is DispatchIntentStatus.FAILED:
        action = ProjectActionKind.BLOCKED
        summary = "The current milestone dispatch failed without a recoverable checkpoint."
        warnings.append(str(intent.get("error") or "Dispatch failed."))
    elif intent_status in {DispatchIntentStatus.PENDING, DispatchIntentStatus.CLAIMED}:
        action = ProjectActionKind.WORKING
        summary = "The current milestone is queued or claimed by the dispatcher."
        next_command = (
            "pi /dispatch"
            if intent_status is DispatchIntentStatus.PENDING
            else f"pi /project-status {project_id}"
        )
    elif milestone_status in {MilestoneStatus.FAILED, MilestoneStatus.BLOCKED}:
        action = ProjectActionKind.BLOCKED
        summary = "The next milestone is durably blocked and needs bounded recovery."
        next_command = "pi /try-milestone"
    elif milestone_statuses and all(
        status is MilestoneStatus.COMPLETED for status in milestone_statuses
    ):
        action = ProjectActionKind.COMPLETE
        summary = "Every durable saga milestone is complete."
    else:
        gawd_doc_id = str(saga.get("gawd_doc_id") or "")
        milestone_text = " ".join(
            str((milestone or {}).get(field) or "") for field in ("name", "description")
        ).lower()
        if any(token in milestone_text for token in ("deploy", "hosted preview", "vercel")):
            action = ProjectActionKind.DEPLOY_APPROVAL_REQUIRED
            summary = "The hosted-preview boundary is waiting for explicit deploy approval."
        else:
            action = ProjectActionKind.HUMAN_DECISION_REQUIRED
            summary = "The next dependency-ready milestone is waiting for operator approval."
        next_command = (
            f"pi /start /approved-gawd {gawd_doc_id} --target-project {project_id}"
            if gawd_doc_id
            else None
        )

    if unrecognized:
        # This overrides whatever the chain concluded, because the chain reasoned
        # about statuses it could not read. Blocking with the reason on screen is
        # what fail-closed means; the previous fall-through produced a confident
        # next command from an unknown value.
        action = ProjectActionKind.BLOCKED
        summary = "A durable status could not be read, so the cockpit refuses to guess."
        next_command = None
        warnings.extend(unrecognized)

    intent_result = _parse_json_object((intent or {}).get("result"))
    verification = intent_result.get("verification")
    if not isinstance(verification, Mapping):
        verification = None
    review = intent_result.get("review") or intent_result.get("staff_review")
    if not isinstance(review, Mapping):
        review = None

    return ProjectActionSnapshot(
        generated_at=generated_at,
        freshness_seconds=0,
        action=action,
        summary=summary,
        next_command=next_command,
        runtime=RuntimeFacts(
            status="ok",
            application_version=settings.application_version,
            coordination_backend=settings.coordination_backend,
            revision=_read_runtime_revision(),
        ),
        project=ProjectFacts.from_project_row(project),
        saga=SagaFacts.from_ledger_row(saga) if saga else None,
        milestone=MilestoneFacts.from_ledger_row(milestone) if milestone else None,
        execution=_execution_facts(lease, intent),
        checkpoint=CheckpointFacts.from_ledger_row(checkpoint) if checkpoint else None,
        verification=dict(verification) if verification else None,
        review=dict(review) if review else None,
        approval=ApprovalFacts.from_ledger_row(approval) if approval else None,
        warnings=warnings,
        source_ids={
            "saga_ids": [saga_id] if saga_id else [],
            "intent_ids": [
                str(row.get("intent_id")) for row in project_intents if row.get("intent_id")
            ],
            "lease_ids": [str(lease["lease_id"])] if lease and lease.get("lease_id") else [],
            "checkpoint_ids": [
                str(row.get("checkpoint_id"))
                for row in checkpoints
                if row.get("checkpoint_id")
                and str(row.get("source_repo_path") or "") == str(project.get("path") or "")
            ],
            "approval_ids": [
                str(row.get("approval_id")) for row in saga_approvals if row.get("approval_id")
            ],
        },
    )


def build_project_action_snapshot(
    project_id: str,
    *,
    settings: Settings | None = None,
    source: ProjectActionSource | None = None,
    generated_at: datetime | None = None,
) -> ProjectActionSnapshot:
    settings = settings or get_settings()
    source = source or LedgerProjectActionSource(settings)
    return _build_project_action_snapshot(
        project_id,
        source.read_project_action_facts(project_id),
        settings=settings,
        generated_at=generated_at or datetime.now(UTC),
    )


__all__ = [
    "PROJECT_ACTION_SCHEMA_VERSION",
    "LedgerProjectActionSource",
    "ProjectActionKind",
    "ProjectActionSnapshot",
    "ProjectActionSource",
    "build_project_action_snapshot",
]
