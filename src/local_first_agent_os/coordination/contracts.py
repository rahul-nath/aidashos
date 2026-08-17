# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Typed command and result contracts for the coordination ledger boundary."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .outcomes import TerminalOutcome


class CoordinationCommandName(StrEnum):
    ADOPT_INTEGRATED_WORK_UNIT_MILESTONE = "adopt_integrated_work_unit_milestone"
    ADOPT_RECOVERED_WORK_UNIT_DISPATCH = "adopt_recovered_work_unit_dispatch"
    ADOPT_SETTLED_WORK_UNIT_DISPATCH = "adopt_settled_work_unit_dispatch"
    AMEND_SAGA_MILESTONE = "amend_saga_milestone"
    APPEND_EXECUTION_EVENT = "append_execution_event"
    ATTACH_EXECUTION_ARTIFACT = "attach_execution_artifact"
    APPEND_NOTE = "append_note"
    APPROVE_GAWD_DOC = "approve_gawd_doc"
    ATTACH_GAWD_DOC_TO_SAGA = "attach_gawd_doc_to_saga"
    CANCEL_DISPATCH_INTENT = "cancel_dispatch_intent"
    CHECK_AMBIGUITY = "check_ambiguity"
    CHECK_STAGNATION = "check_stagnation"
    CLAIM_NEXT_DISPATCH_INTENT = "claim_next_dispatch_intent"
    CLAIM_NEXT_LEDGER_EVENT = "claim_next_ledger_event"
    CLAIM_TASK = "claim_task"
    COMPLETE_DISPATCH_INTENT = "complete_dispatch_intent"
    COMPLETE_EXECUTION_LEASE = "complete_execution_lease"
    COMPLETE_LEDGER_EVENT = "complete_ledger_event"
    COMPLETE_POW_WOW = "complete_pow_wow"
    COMPLETE_SAGA_MILESTONE = "complete_saga_milestone"
    COMPLETE_TASK = "complete_task"
    CREATE_GAWD_DOC = "create_gawd_doc"
    CREATE_EXECUTION_CHECKPOINT = "create_execution_checkpoint"
    CREATE_POW_WOW = "create_pow_wow"
    CREATE_SAGA = "create_saga"
    CREATE_SAGA_MILESTONE = "create_saga_milestone"
    CANCEL_WORK_UNIT = "cancel_work_unit"
    COMPILE_DESIGN_DOC = "compile_design_doc"
    DRAIN_WORK_UNIT_ENQUEUES = "drain_work_unit_enqueues"
    RUN_ENQUEUE_DRAINER = "run_enqueue_drainer"
    RUN_CRASH_RECONCILER = "run_crash_reconciler"
    GET_WORK_UNIT = "get_work_unit"
    LIST_WORK_UNITS = "list_work_units"
    LIST_DESIGN_DOCS = "list_design_docs"
    LIST_WORK_UNIT_ARTIFACTS = "list_work_unit_artifacts"
    LIST_WORK_UNIT_EVENTS = "list_work_unit_events"
    RESUME_WORK_UNIT = "resume_work_unit"
    START_WORK_UNIT = "start_work_unit"
    SUBMIT_WORK_UNIT_DECISION = "submit_work_unit_decision"
    DESCRIBE_RESIDENT_LOOPS = "describe_resident_loops"
    DELEGATE_TASK = "delegate_task"
    DECIDE_EXECUTION_CHECKPOINT = "decide_execution_checkpoint"
    FAIL_SAGA_MILESTONE = "fail_saga_milestone"
    FAIL_TASK = "fail_task"
    GC = "gc"
    GET_ARTIFACT = "get_artifact"
    GET_EXECUTION_CHECKPOINT = "get_execution_checkpoint"
    GET_GAWD_DOC = "get_gawd_doc"
    GET_POW_WOW = "get_pow_wow"
    GET_SAGA = "get_saga"
    GET_SAGA_MILESTONE = "get_saga_milestone"
    HANDOFF = "handoff"
    HEARTBEAT = "heartbeat"
    HEARTBEAT_EXECUTION_LEASE = "heartbeat_execution_lease"
    LATEST_REPO_AUDIT = "latest_repo_audit"
    LIST_APPROVAL_REQUESTS = "list_approval_requests"
    LIST_DISPATCH_INTENTS = "list_dispatch_intents"
    LIST_EXECUTION_LEASES = "list_execution_leases"
    LIST_EXECUTION_CHECKPOINTS = "list_execution_checkpoints"
    LIST_EXECUTION_EVENTS = "list_execution_events"
    LIST_EXECUTION_ARTIFACTS = "list_execution_artifacts"
    LIST_INTEGRATION_REQUESTS = "list_integration_requests"
    LIST_LEDGER_EVENTS = "list_ledger_events"
    LIST_POW_WOWS = "list_pow_wows"
    LIST_SAGA_MILESTONES = "list_saga_milestones"
    LIST_SAGAS = "list_sagas"
    LIST_SESSIONS = "list_sessions"
    LIST_TASKS = "list_tasks"
    NEXT_READY_SAGA_MILESTONE = "next_ready_saga_milestone"
    OPEN_EXECUTION_LEASE = "open_execution_lease"
    READ_EXECUTION_LEDGER = "read_execution_ledger"
    RUN_LEDGER_DISPATCHER = "run_ledger_dispatcher"
    RUN_REFINERY = "run_refinery"
    READ_NOTES = "read_notes"
    RECONCILE_SAGA_MILESTONES = "reconcile_saga_milestones"
    RECOVER_UNPARSED_STAFF_REVIEW = "recover_unparsed_staff_review"
    RECORD_MILESTONE_EVIDENCE = "record_milestone_evidence"
    REGISTER_AGENT = "register_agent"
    REQUEST_EXECUTION_CANCEL = "request_execution_cancel"
    REQUEST_RECOVERY_STAFF_REVIEW = "request_recovery_staff_review"
    RESOLVE_APPROVAL_REQUEST = "resolve_approval_request"
    REVOKE_APPROVAL_REQUEST = "revoke_approval_request"
    RETRY_SAGA_MILESTONE = "retry_saga_milestone"
    SERVE = "serve"
    START_SAGA_MILESTONE = "start_saga_milestone"
    SUBMIT_APPROVAL_REQUEST = "submit_approval_request"
    SUBMIT_ARTIFACT = "submit_artifact"
    SUBMIT_DISPATCH_INTENT = "submit_dispatch_intent"
    SUPERSEDE_DISPATCH_INTENT = "supersede_dispatch_intent"


class CoordinationFlag(StrEnum):
    ABANDONED_AFTER_SECONDS = "--abandoned-after-seconds"
    ACCEPTANCE_EVIDENCE = "--acceptance-evidence"
    ACCEPTANCE_CRITERIA = "--acceptance-criteria"
    ACCEPTED_BY = "--accepted-by"
    AFTER_SEQUENCE = "--after-sequence"
    ADAPTER = "--adapter"
    AGENT_NAME = "--agent-name"
    AGENT_TIER = "--agent-tier"
    AUDIENCE = "--audience"
    ALLOW_TIER = "--allow-tier"
    ALLOWED_TOOLS = "--allowed-tools"
    AMENDED_BY = "--amended-by"
    APPROVAL_REQUIRED = "--approval-required"
    APPROVED_PLAN_HASH = "--approved-plan-hash"
    BLOCKED_BY = "--blocked-by"
    BUDGET_SECONDS = "--budget-seconds"
    BUDGET_TOKENS = "--budget-tokens"
    CANCELED_BY = "--canceled-by"
    CLAIMED_BY = "--claimed-by"
    COMMAND_JSON = "--command-json"
    COMPENSATION_JSON = "--compensation-json"
    CONSTRAINTS = "--constraints"
    CONTENT_FILE = "--content-file"
    DECIDED_BY = "--decided-by"
    DECISION_JSON = "--decision-json"
    DEPENDS_ON = "--depends-on"
    DESCRIPTION = "--description"
    DESIGN_DOC_ID = "--design-doc-id"
    CLASSIFY_PHASES = "--classify-phases"
    DISPATCH_INTENT_ID = "--dispatch-intent-id"
    DISPATCHER_NAME = "--dispatcher-name"
    ENTRY_CRITERIA = "--entry-criteria"
    ERROR = "--error"
    EVENT_TYPE = "--event-type"
    EVENT_SUMMARY = "--event-summary"
    EVIDENCE_CONTENT = "--evidence-content"
    EVIDENCE_TYPE = "--evidence-type"
    EXIT_CRITERIA = "--exit-criteria"
    FANOUT = "--fanout"
    GAWD_DOC_ID = "--gawd-doc-id"
    CONTENT_DIGEST = "--content-digest"
    INLINE = "--inline"
    INTERVAL_SECONDS = "--interval-seconds"
    INTENT_ID = "--intent-id"
    KIND = "--kind"
    LIMIT = "--limit"
    MAX_AUTOMATIC_RECOVERIES = "--max-automatic-recoveries"
    MAX_POLLS = "--max-polls"
    MAX_TOKENS = "--max-tokens"
    MILESTONE_ID = "--milestone-id"
    MODEL_ROLE = "--model-role"
    NO_NEXT_COMMANDS = "--no-next-commands"
    NO_SUBMIT_RESULT = "--no-submit-result"
    PARENT_INTENT_ID = "--parent-intent-id"
    PAYLOAD = "--payload"
    PAYLOAD_FILE = "--payload-file"
    PAYLOAD_SHA256 = "--payload-sha256"
    POW_WOW_ID = "--pow-wow-id"
    PROMPT = "--prompt"
    REASON = "--reason"
    REDUCE = "--reduce"
    REDUCER_TIER = "--reducer-tier"
    REQUESTED_BY = "--requested-by"
    REQUIRED_ARTIFACT = "--required-artifact"
    REQUIRED_OUTPUTS = "--required-outputs"
    RESOLVED_BY = "--resolved-by"
    REVOKED_BY = "--revoked-by"
    RESULT = "--result"
    RESULT_FILE = "--result-file"
    RESULT_JSON = "--result-json"
    RETENTION_SECONDS = "--retention-seconds"
    ROLE = "--role"
    ROOT = "--root"
    SAGA_ID = "--saga-id"
    SCHEMA_VERSION = "--schema-version"
    SEQUENCE = "--sequence"
    OCCURRED_AT = "--occurred-at"
    OUTCOME = "--outcome"
    SESSION = "--session"
    SOURCE = "--source"
    SOURCE_REPO_PATH = "--source-repo-path"
    STATE = "--state"
    STATUS = "--status"
    SUCCESS_CRITERIA = "--success-criteria"
    SUMMARY = "--summary"
    SUBMIT_REVIEW = "--submit-review"
    SUPERSEDED_BY = "--superseded-by"
    TARGET_PROJECT_ID = "--target-project-id"
    TASK_GRAPH_JSON = "--task-graph-json"
    TASK_ID = "--task-id"
    TIER = "--tier"
    TITLE = "--title"
    TIMEOUT_SECONDS = "--timeout-seconds"
    TRANSCRIPT_ARTIFACT_ID = "--transcript-artifact-id"
    PATCH_ARTIFACT_ID = "--patch-artifact-id"
    GIT_STATUS_ARTIFACT_ID = "--git-status-artifact-id"
    TEST_SUMMARY_ARTIFACT_ID = "--test-summary-artifact-id"
    JUNIOR_REVIEW_ARTIFACT_ID = "--junior-review-artifact-id"
    BASE_HEAD_SHA = "--base-head-sha"
    BRANCH = "--branch"
    COMMIT_SHA = "--commit-sha"
    TASK_CONTRACT = "--task-contract"
    TRANSPORT = "--transport"
    UNRESOLVED = "--unresolved"
    WORKER_ID = "--worker-id"
    WORKFLOW_NAME = "--workflow-name"
    WORKTREE_PATH = "--worktree-path"


class DispatchTerminalStatus(StrEnum):
    DONE = "DONE"
    FAILED = "FAILED"


class ExecutionLeaseTerminalStatus(StrEnum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELED = "CANCELED"
    COMPENSATED = "COMPENSATED"


class ApprovalDecision(StrEnum):
    APPROVE = "approve"
    DENY = "deny"


class DispatchTier(StrEnum):
    JUNIOR = "junior"
    SENIOR = "senior"
    STAFF = "staff"


class DispatchKind(StrEnum):
    ADVISORY = "advisory"
    CODE = "code"


class DispatchReduce(StrEnum):
    NONE = "none"
    VOTE = "vote"
    JUDGE = "judge"


class CoordinationCommand(Protocol):
    @property
    def name(self) -> CoordinationCommandName: ...

    def to_argv(self) -> list[str]: ...


@dataclass(frozen=True)
class RawCoordinationCommand:
    """Validated adapter for intentionally serialized argv callers."""

    name: CoordinationCommandName
    arguments: tuple[str, ...] = ()

    @classmethod
    def from_argv(cls, argv: Sequence[str]) -> RawCoordinationCommand:
        if not argv:
            raise ValueError("coordination command argv cannot be empty")
        return cls(CoordinationCommandName(argv[0]), tuple(argv[1:]))

    def to_argv(self) -> list[str]:
        return [self.name.value, *self.arguments]


def _append_option(argv: list[str], flag: CoordinationFlag, value: object | None) -> None:
    if value is not None:
        argv.extend([flag.value, str(value)])


def _append_repeated(
    argv: list[str],
    flag: CoordinationFlag,
    values: Sequence[object],
) -> None:
    for value in values:
        argv.extend([flag.value, str(value)])


@dataclass(frozen=True)
class CreateSaga:
    goal: str
    budget_tokens: int = 1_000_000
    budget_seconds: int = 86_400
    gawd_doc_id: str | None = None
    # sha256 of the source draft. Set it and a repeated ingest replays
    # onto the existing saga instead of creating a second one.
    content_digest: str | None = None
    name: CoordinationCommandName = CoordinationCommandName.CREATE_SAGA

    def to_argv(self) -> list[str]:
        argv = [
            self.name.value,
            self.goal,
            CoordinationFlag.BUDGET_TOKENS.value,
            str(self.budget_tokens),
            CoordinationFlag.BUDGET_SECONDS.value,
            str(self.budget_seconds),
        ]
        _append_option(argv, CoordinationFlag.GAWD_DOC_ID, self.gawd_doc_id)
        _append_option(argv, CoordinationFlag.CONTENT_DIGEST, self.content_digest)
        return argv


@dataclass(frozen=True)
class CreateGawdDoc:
    goal: str
    saga_id: str | None = None
    constraints: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    task_graph: Mapping[str, object] | None = None
    name: CoordinationCommandName = CoordinationCommandName.CREATE_GAWD_DOC

    def to_argv(self) -> list[str]:
        argv = [self.name.value, self.goal]
        _append_option(argv, CoordinationFlag.SAGA_ID, self.saga_id)
        if self.constraints:
            argv.append(CoordinationFlag.CONSTRAINTS.value)
            argv.extend(self.constraints)
        if self.success_criteria:
            argv.append(CoordinationFlag.SUCCESS_CRITERIA.value)
            argv.extend(self.success_criteria)
        if self.unresolved:
            argv.append(CoordinationFlag.UNRESOLVED.value)
            argv.extend(self.unresolved)
        if self.acceptance_criteria:
            argv.append(CoordinationFlag.ACCEPTANCE_CRITERIA.value)
            argv.extend(self.acceptance_criteria)
        if self.task_graph is not None:
            argv.extend(
                [
                    CoordinationFlag.TASK_GRAPH_JSON.value,
                    json.dumps(self.task_graph, sort_keys=True),
                ]
            )
        return argv


@dataclass(frozen=True)
class AttachGawdDocToSaga:
    saga_id: str
    gawd_doc_id: str
    name: CoordinationCommandName = CoordinationCommandName.ATTACH_GAWD_DOC_TO_SAGA

    def to_argv(self) -> list[str]:
        return [self.name.value, self.saga_id, self.gawd_doc_id]


@dataclass(frozen=True)
class GetGawdDoc:
    gawd_doc_id: str
    name: CoordinationCommandName = CoordinationCommandName.GET_GAWD_DOC

    def to_argv(self) -> list[str]:
        return [self.name.value, self.gawd_doc_id]


@dataclass(frozen=True)
class ApproveGawdDoc:
    gawd_doc_id: str
    name: CoordinationCommandName = CoordinationCommandName.APPROVE_GAWD_DOC

    def to_argv(self) -> list[str]:
        return [self.name.value, self.gawd_doc_id]


@dataclass(frozen=True)
class CreatePowWow:
    saga_id: str
    stage: str
    goal: str
    exit_criteria: str = ""
    budget_tokens: int = 100_000
    allowed_tools: tuple[str, ...] = ()
    required_outputs: tuple[str, ...] = ()
    name: CoordinationCommandName = CoordinationCommandName.CREATE_POW_WOW

    def to_argv(self) -> list[str]:
        argv = [self.name.value, self.saga_id, self.stage, self.goal]
        _append_option(argv, CoordinationFlag.EXIT_CRITERIA, self.exit_criteria)
        _append_option(argv, CoordinationFlag.BUDGET_TOKENS, self.budget_tokens)
        if self.allowed_tools:
            argv.append(CoordinationFlag.ALLOWED_TOOLS.value)
            argv.extend(self.allowed_tools)
        if self.required_outputs:
            argv.append(CoordinationFlag.REQUIRED_OUTPUTS.value)
            argv.extend(self.required_outputs)
        return argv


@dataclass(frozen=True)
class ClaimTask:
    pow_wow_id: str
    task_name: str
    description: str
    blocked_by: tuple[str, ...] = ()
    name: CoordinationCommandName = CoordinationCommandName.CLAIM_TASK

    def to_argv(self) -> list[str]:
        argv = [self.name.value, self.pow_wow_id, self.task_name, self.description]
        _append_repeated(argv, CoordinationFlag.BLOCKED_BY, self.blocked_by)
        return argv


@dataclass(frozen=True)
class CompletePowWow:
    pow_wow_id: str
    output_summary: str
    status: str = "COMPLETED"
    name: CoordinationCommandName = CoordinationCommandName.COMPLETE_POW_WOW

    def to_argv(self) -> list[str]:
        return [
            self.name.value,
            self.pow_wow_id,
            self.output_summary,
            CoordinationFlag.STATUS.value,
            self.status,
        ]


@dataclass(frozen=True)
class ListSagaMilestones:
    saga_id: str
    status: str | None = None
    name: CoordinationCommandName = CoordinationCommandName.LIST_SAGA_MILESTONES

    def to_argv(self) -> list[str]:
        argv = [self.name.value, self.saga_id]
        _append_option(argv, CoordinationFlag.STATUS, self.status)
        return argv


@dataclass(frozen=True)
class ListSagas:
    status: str | None = None
    name: CoordinationCommandName = CoordinationCommandName.LIST_SAGAS

    def to_argv(self) -> list[str]:
        argv = [self.name.value]
        _append_option(argv, CoordinationFlag.STATUS, self.status)
        return argv


@dataclass(frozen=True)
class NextReadySagaMilestone:
    saga_id: str
    name: CoordinationCommandName = CoordinationCommandName.NEXT_READY_SAGA_MILESTONE

    def to_argv(self) -> list[str]:
        return [self.name.value, self.saga_id]


@dataclass(frozen=True)
class CreateSagaMilestone:
    saga_id: str
    name_text: str
    sequence: int
    milestone_id: str | None = None
    gawd_doc_id: str | None = None
    description: str = ""
    depends_on: tuple[str, ...] = ()
    entry_criteria: tuple[str, ...] = ()
    exit_criteria: tuple[str, ...] = ()
    required_artifacts: tuple[str, ...] = ()
    approval_required: bool = False
    name: CoordinationCommandName = CoordinationCommandName.CREATE_SAGA_MILESTONE

    def to_argv(self) -> list[str]:
        argv = [
            self.name.value,
            self.saga_id,
            self.name_text,
            CoordinationFlag.SEQUENCE.value,
            str(self.sequence),
        ]
        _append_option(argv, CoordinationFlag.MILESTONE_ID, self.milestone_id)
        _append_option(argv, CoordinationFlag.GAWD_DOC_ID, self.gawd_doc_id)
        _append_option(argv, CoordinationFlag.DESCRIPTION, self.description)
        _append_repeated(argv, CoordinationFlag.DEPENDS_ON, self.depends_on)
        _append_repeated(argv, CoordinationFlag.ENTRY_CRITERIA, self.entry_criteria)
        _append_repeated(argv, CoordinationFlag.EXIT_CRITERIA, self.exit_criteria)
        _append_repeated(
            argv,
            CoordinationFlag.REQUIRED_ARTIFACT,
            self.required_artifacts,
        )
        if self.approval_required:
            argv.append(CoordinationFlag.APPROVAL_REQUIRED.value)
        return argv


@dataclass(frozen=True)
class AmendSagaMilestone:
    milestone_id: str
    reason: str
    amended_by: str = "operator"
    description: str | None = None
    entry_criteria: tuple[str, ...] | None = None
    exit_criteria: tuple[str, ...] | None = None
    required_artifacts: tuple[str, ...] | None = None
    name: CoordinationCommandName = CoordinationCommandName.AMEND_SAGA_MILESTONE

    def to_argv(self) -> list[str]:
        argv = [self.name.value, self.milestone_id]
        _append_option(argv, CoordinationFlag.DESCRIPTION, self.description)
        if self.entry_criteria is not None:
            _append_repeated(argv, CoordinationFlag.ENTRY_CRITERIA, self.entry_criteria)
        if self.exit_criteria is not None:
            _append_repeated(argv, CoordinationFlag.EXIT_CRITERIA, self.exit_criteria)
        if self.required_artifacts is not None:
            _append_repeated(
                argv,
                CoordinationFlag.REQUIRED_ARTIFACT,
                self.required_artifacts,
            )
        argv.extend(
            [
                CoordinationFlag.REASON.value,
                self.reason,
                CoordinationFlag.AMENDED_BY.value,
                self.amended_by,
            ]
        )
        return argv


@dataclass(frozen=True)
class RetrySagaMilestone:
    milestone_id: str
    reason: str
    name: CoordinationCommandName = CoordinationCommandName.RETRY_SAGA_MILESTONE

    def to_argv(self) -> list[str]:
        return [self.name.value, self.milestone_id, self.reason]


@dataclass(frozen=True)
class ListApprovalRequests:
    saga_id: str | None = None
    status: str | None = None
    name: CoordinationCommandName = CoordinationCommandName.LIST_APPROVAL_REQUESTS

    def to_argv(self) -> list[str]:
        argv = [self.name.value]
        _append_option(argv, CoordinationFlag.SAGA_ID, self.saga_id)
        _append_option(argv, CoordinationFlag.STATUS, self.status)
        return argv


@dataclass(frozen=True)
class ListIntegrationRequests:
    """What the refinery's queue holds, for a project or for all of them.

    Read-only and here from the queue's first milestone, because a durable table
    only test code can read is the shape this repository has already paid for
    several times. An operator who resolves a merge is told the request id; this
    is how they see what became of it.
    """

    target_project_id: str | None = None
    state: str | None = None
    name: CoordinationCommandName = CoordinationCommandName.LIST_INTEGRATION_REQUESTS

    def to_argv(self) -> list[str]:
        argv = [self.name.value]
        _append_option(argv, CoordinationFlag.TARGET_PROJECT_ID, self.target_project_id)
        _append_option(argv, CoordinationFlag.STATE, self.state)
        return argv


@dataclass(frozen=True)
class SubmitDispatchIntent:
    tier: DispatchTier
    prompt: str
    kind: DispatchKind = DispatchKind.ADVISORY
    target_project_id: str | None = None
    source: str | None = None
    fanout: int = 1
    allow_tiers: tuple[DispatchTier, ...] = ()
    reduce: DispatchReduce = DispatchReduce.NONE
    reducer_tier: DispatchTier | None = None
    name: CoordinationCommandName = CoordinationCommandName.SUBMIT_DISPATCH_INTENT

    def to_argv(self) -> list[str]:
        argv = [
            self.name.value,
            self.tier.value,
            self.prompt,
            CoordinationFlag.KIND.value,
            self.kind.value,
            CoordinationFlag.FANOUT.value,
            str(self.fanout),
            CoordinationFlag.REDUCE.value,
            self.reduce.value,
        ]
        _append_option(argv, CoordinationFlag.TARGET_PROJECT_ID, self.target_project_id)
        _append_option(argv, CoordinationFlag.SOURCE, self.source)
        _append_repeated(
            argv,
            CoordinationFlag.ALLOW_TIER,
            tuple(tier.value for tier in self.allow_tiers),
        )
        _append_option(
            argv,
            CoordinationFlag.REDUCER_TIER,
            self.reducer_tier.value if self.reducer_tier else None,
        )
        return argv


@dataclass(frozen=True)
class ClaimNextDispatchIntent:
    claimed_by: str
    tier: DispatchTier | None = None
    name: CoordinationCommandName = CoordinationCommandName.CLAIM_NEXT_DISPATCH_INTENT

    def to_argv(self) -> list[str]:
        argv = [
            self.name.value,
            CoordinationFlag.CLAIMED_BY.value,
            self.claimed_by,
        ]
        _append_option(
            argv,
            CoordinationFlag.TIER,
            self.tier.value if self.tier else None,
        )
        return argv


@dataclass(frozen=True)
class SubmitArtifact:
    pow_wow_id: str
    artifact_type: str
    content: str
    schema_version: str = "v1"
    task_id: str | None = None
    name: CoordinationCommandName = CoordinationCommandName.SUBMIT_ARTIFACT

    def to_argv(self) -> list[str]:
        argv = [self.name.value, self.pow_wow_id, self.artifact_type, self.content]
        argv.extend([CoordinationFlag.SCHEMA_VERSION.value, self.schema_version])
        if self.task_id is not None:
            argv.extend([CoordinationFlag.TASK_ID.value, self.task_id])
        return argv


@dataclass(frozen=True)
class LatestRepoAudit:
    """The most recent repository audit one project's same-tier successor may inherit.

    A collection of at most one, rather than an entity, because the empty
    answer is the normal one: the first dispatch for any project starts cold by
    construction, and the transport treats entity not-found as a failure.
    """

    target_project_id: str
    tier: str
    name: CoordinationCommandName = CoordinationCommandName.LATEST_REPO_AUDIT

    def to_argv(self) -> list[str]:
        return [self.name.value, self.target_project_id, self.tier]


@dataclass(frozen=True)
class CompleteTask:
    task_id: str
    name: CoordinationCommandName = CoordinationCommandName.COMPLETE_TASK

    def to_argv(self) -> list[str]:
        return [self.name.value, self.task_id]


@dataclass(frozen=True)
class FailTask:
    task_id: str
    reason: str
    name: CoordinationCommandName = CoordinationCommandName.FAIL_TASK

    def to_argv(self) -> list[str]:
        return [self.name.value, self.task_id, self.reason]


@dataclass(frozen=True)
class CompleteDispatchIntent:
    intent_id: str
    status: DispatchTerminalStatus
    result: str | None = None
    error: str | None = None
    name: CoordinationCommandName = CoordinationCommandName.COMPLETE_DISPATCH_INTENT

    def to_argv(self) -> list[str]:
        argv = [self.name.value, self.intent_id, self.status.value]
        if self.result is not None:
            argv.extend([CoordinationFlag.RESULT.value, self.result])
        if self.error is not None:
            argv.extend([CoordinationFlag.ERROR.value, self.error])
        return argv


@dataclass(frozen=True)
class ListDispatchIntents:
    status: str | None = None
    parent_intent_id: str | None = None
    name: CoordinationCommandName = CoordinationCommandName.LIST_DISPATCH_INTENTS

    def to_argv(self) -> list[str]:
        argv = [self.name.value]
        if self.status is not None:
            argv.extend([CoordinationFlag.STATUS.value, self.status])
        if self.parent_intent_id is not None:
            argv.extend([CoordinationFlag.PARENT_INTENT_ID.value, self.parent_intent_id])
        return argv


@dataclass(frozen=True)
class SubmitApprovalRequest:
    saga_id: str
    request_type: str
    requested_by: str | None = None
    payload: Mapping[str, object] | None = None
    name: CoordinationCommandName = CoordinationCommandName.SUBMIT_APPROVAL_REQUEST

    def to_argv(self) -> list[str]:
        argv = [self.name.value, self.saga_id, self.request_type]
        if self.requested_by is not None:
            argv.extend([CoordinationFlag.REQUESTED_BY.value, self.requested_by])
        if self.payload is not None:
            argv.extend([CoordinationFlag.PAYLOAD.value, json.dumps(self.payload, sort_keys=True)])
        return argv


@dataclass(frozen=True)
class ResolveApprovalRequest:
    approval_id: str
    decision: ApprovalDecision
    resolved_by: str
    name: CoordinationCommandName = CoordinationCommandName.RESOLVE_APPROVAL_REQUEST

    def to_argv(self) -> list[str]:
        return [
            self.name.value,
            self.approval_id,
            self.decision.value,
            CoordinationFlag.RESOLVED_BY.value,
            self.resolved_by,
        ]


@dataclass(frozen=True)
class RevokeApprovalRequest:
    approval_id: str
    revoked_by: str
    reason: str
    name: CoordinationCommandName = CoordinationCommandName.REVOKE_APPROVAL_REQUEST

    def to_argv(self) -> list[str]:
        return [
            self.name.value,
            self.approval_id,
            CoordinationFlag.REVOKED_BY.value,
            self.revoked_by,
            CoordinationFlag.REASON.value,
            self.reason,
        ]


@dataclass(frozen=True)
class OpenExecutionLease:
    idempotency_key: str
    worker_id: str
    timeout_seconds: int
    agent_tier: str | None = None
    agent_name: str | None = None
    intent_id: str | None = None
    task_id: str | None = None
    worktree_path: str | None = None
    command: tuple[str, ...] = ()
    compensation: Mapping[str, object] | None = None
    name: CoordinationCommandName = CoordinationCommandName.OPEN_EXECUTION_LEASE

    def to_argv(self) -> list[str]:
        argv = [
            self.name.value,
            self.idempotency_key,
            CoordinationFlag.WORKER_ID.value,
            self.worker_id,
            CoordinationFlag.TIMEOUT_SECONDS.value,
            str(self.timeout_seconds),
        ]
        for flag, value in (
            (CoordinationFlag.AGENT_TIER, self.agent_tier),
            (CoordinationFlag.AGENT_NAME, self.agent_name),
            (CoordinationFlag.INTENT_ID, self.intent_id),
            (CoordinationFlag.TASK_ID, self.task_id),
            (CoordinationFlag.WORKTREE_PATH, self.worktree_path),
        ):
            if value is not None:
                argv.extend([flag.value, value])
        if self.command:
            argv.extend([CoordinationFlag.COMMAND_JSON.value, json.dumps(self.command)])
        if self.compensation is not None:
            argv.extend(
                [
                    CoordinationFlag.COMPENSATION_JSON.value,
                    json.dumps(self.compensation, sort_keys=True),
                ]
            )
        return argv


@dataclass(frozen=True)
class CompleteExecutionLease:
    lease_id: str
    status: ExecutionLeaseTerminalStatus
    result: Mapping[str, object] | None = None
    error: str | None = None
    name: CoordinationCommandName = CoordinationCommandName.COMPLETE_EXECUTION_LEASE

    def to_argv(self) -> list[str]:
        argv = [self.name.value, self.lease_id, self.status.value]
        if self.result is not None:
            argv.extend(
                [
                    CoordinationFlag.RESULT_JSON.value,
                    json.dumps(self.result, sort_keys=True),
                ]
            )
        if self.error is not None:
            argv.extend([CoordinationFlag.ERROR.value, self.error])
        return argv


@dataclass(frozen=True)
class HeartbeatExecutionLease:
    lease_id: str
    worker_id: str
    name: CoordinationCommandName = CoordinationCommandName.HEARTBEAT_EXECUTION_LEASE

    def to_argv(self) -> list[str]:
        return [
            self.name.value,
            self.lease_id,
            CoordinationFlag.WORKER_ID.value,
            self.worker_id,
        ]


@dataclass(frozen=True)
class RequestExecutionCancel:
    lease_id: str
    reason: str | None = None
    requested_by: str = "supervisor"
    name: CoordinationCommandName = CoordinationCommandName.REQUEST_EXECUTION_CANCEL

    def to_argv(self) -> list[str]:
        argv = [
            self.name.value,
            self.lease_id,
            CoordinationFlag.REQUESTED_BY.value,
            self.requested_by,
        ]
        _append_option(argv, CoordinationFlag.REASON, self.reason)
        return argv


@dataclass(frozen=True)
class AppendExecutionEvent:
    lease_id: str
    sequence: int
    occurred_at: float
    source: str
    kind: str
    payload: Mapping[str, object]
    payload_sha256: str
    name: CoordinationCommandName = CoordinationCommandName.APPEND_EXECUTION_EVENT

    def to_argv(self) -> list[str]:
        return [
            self.name.value,
            self.lease_id,
            CoordinationFlag.SEQUENCE.value,
            str(self.sequence),
            CoordinationFlag.OCCURRED_AT.value,
            str(self.occurred_at),
            CoordinationFlag.SOURCE.value,
            self.source,
            CoordinationFlag.KIND.value,
            self.kind,
            CoordinationFlag.PAYLOAD.value,
            json.dumps(self.payload, sort_keys=True),
            CoordinationFlag.PAYLOAD_SHA256.value,
            self.payload_sha256,
        ]


@dataclass(frozen=True)
class ListExecutionEvents:
    lease_id: str
    after_sequence: int = 0
    limit: int = 200
    name: CoordinationCommandName = CoordinationCommandName.LIST_EXECUTION_EVENTS

    def to_argv(self) -> list[str]:
        return [
            self.name.value,
            self.lease_id,
            CoordinationFlag.AFTER_SEQUENCE.value,
            str(self.after_sequence),
            CoordinationFlag.LIMIT.value,
            str(self.limit),
        ]


@dataclass(frozen=True)
class AttachExecutionArtifact:
    lease_id: str
    artifact_id: str
    role: str
    schema_version: str
    name: CoordinationCommandName = CoordinationCommandName.ATTACH_EXECUTION_ARTIFACT

    def to_argv(self) -> list[str]:
        return [
            self.name.value,
            self.lease_id,
            self.artifact_id,
            CoordinationFlag.ROLE.value,
            self.role,
            CoordinationFlag.SCHEMA_VERSION.value,
            self.schema_version,
        ]


@dataclass(frozen=True)
class ListExecutionArtifacts:
    lease_id: str
    name: CoordinationCommandName = CoordinationCommandName.LIST_EXECUTION_ARTIFACTS

    def to_argv(self) -> list[str]:
        return [self.name.value, self.lease_id]


@dataclass(frozen=True)
class CreateExecutionCheckpoint:
    lease_id: str
    reason: str
    status: str
    saga_id: str | None = None
    pow_wow_id: str | None = None
    worktree_path: str | None = None
    source_repo_path: str | None = None
    base_head_sha: str | None = None
    transcript_artifact_id: str | None = None
    patch_artifact_id: str | None = None
    git_status_artifact_id: str | None = None
    test_summary_artifact_id: str | None = None
    task_contract: str = ""
    event_summary: str = ""
    submit_review: bool = False
    error: str | None = None
    name: CoordinationCommandName = CoordinationCommandName.CREATE_EXECUTION_CHECKPOINT

    def to_argv(self) -> list[str]:
        argv = [
            self.name.value,
            self.lease_id,
            CoordinationFlag.REASON.value,
            self.reason,
            CoordinationFlag.STATUS.value,
            self.status,
        ]
        for flag, value in (
            (CoordinationFlag.SAGA_ID, self.saga_id),
            (CoordinationFlag.POW_WOW_ID, self.pow_wow_id),
            (CoordinationFlag.WORKTREE_PATH, self.worktree_path),
            (CoordinationFlag.SOURCE_REPO_PATH, self.source_repo_path),
            (CoordinationFlag.BASE_HEAD_SHA, self.base_head_sha),
            (CoordinationFlag.TRANSCRIPT_ARTIFACT_ID, self.transcript_artifact_id),
            (CoordinationFlag.PATCH_ARTIFACT_ID, self.patch_artifact_id),
            (CoordinationFlag.GIT_STATUS_ARTIFACT_ID, self.git_status_artifact_id),
            (CoordinationFlag.TEST_SUMMARY_ARTIFACT_ID, self.test_summary_artifact_id),
            (CoordinationFlag.TASK_CONTRACT, self.task_contract or None),
            (CoordinationFlag.EVENT_SUMMARY, self.event_summary or None),
            (CoordinationFlag.ERROR, self.error),
        ):
            _append_option(argv, flag, value)
        if self.submit_review:
            argv.append(CoordinationFlag.SUBMIT_REVIEW.value)
        return argv


@dataclass(frozen=True)
class GetExecutionCheckpoint:
    checkpoint_id: str
    name: CoordinationCommandName = CoordinationCommandName.GET_EXECUTION_CHECKPOINT

    def to_argv(self) -> list[str]:
        return [self.name.value, self.checkpoint_id]


@dataclass(frozen=True)
class RequestRecoveryStaffReview:
    checkpoint_id: str
    target_project_id: str
    branch: str
    base_sha: str
    commit_sha: str
    milestone_id: str | None = None
    name: CoordinationCommandName = CoordinationCommandName.REQUEST_RECOVERY_STAFF_REVIEW

    def to_argv(self) -> list[str]:
        argv = [
            self.name.value,
            self.checkpoint_id,
            CoordinationFlag.TARGET_PROJECT_ID.value,
            self.target_project_id,
            CoordinationFlag.BRANCH.value,
            self.branch,
            CoordinationFlag.BASE_HEAD_SHA.value,
            self.base_sha,
            CoordinationFlag.COMMIT_SHA.value,
            self.commit_sha,
        ]
        _append_option(argv, CoordinationFlag.MILESTONE_ID, self.milestone_id)
        return argv


@dataclass(frozen=True)
class ListExecutionCheckpoints:
    status: str | None = None
    name: CoordinationCommandName = CoordinationCommandName.LIST_EXECUTION_CHECKPOINTS

    def to_argv(self) -> list[str]:
        argv = [self.name.value]
        _append_option(argv, CoordinationFlag.STATUS, self.status)
        return argv


@dataclass(frozen=True)
class ListExecutionLeases:
    status: str | None = None
    name: CoordinationCommandName = CoordinationCommandName.LIST_EXECUTION_LEASES

    def to_argv(self) -> list[str]:
        argv = [self.name.value]
        _append_option(argv, CoordinationFlag.STATUS, self.status)
        return argv


@dataclass(frozen=True)
class DecideExecutionCheckpoint:
    checkpoint_id: str
    decision: Mapping[str, object]
    junior_review_artifact_id: str | None = None
    name: CoordinationCommandName = CoordinationCommandName.DECIDE_EXECUTION_CHECKPOINT

    def to_argv(self) -> list[str]:
        argv = [
            self.name.value,
            self.checkpoint_id,
            CoordinationFlag.DECISION_JSON.value,
            json.dumps(self.decision, sort_keys=True),
        ]
        _append_option(
            argv,
            CoordinationFlag.JUNIOR_REVIEW_ARTIFACT_ID,
            self.junior_review_artifact_id,
        )
        return argv


type TypedCoordinationCommand = (
    CreateSaga
    | CreateGawdDoc
    | AttachGawdDocToSaga
    | GetGawdDoc
    | ApproveGawdDoc
    | CreatePowWow
    | ClaimTask
    | CompletePowWow
    | ListSagas
    | ListSagaMilestones
    | NextReadySagaMilestone
    | CreateSagaMilestone
    | AmendSagaMilestone
    | RetrySagaMilestone
    | ListApprovalRequests
    | LatestRepoAudit
    | ListIntegrationRequests
    | SubmitDispatchIntent
    | ClaimNextDispatchIntent
    | SubmitArtifact
    | CompleteTask
    | FailTask
    | CompleteDispatchIntent
    | ListDispatchIntents
    | SubmitApprovalRequest
    | ResolveApprovalRequest
    | RevokeApprovalRequest
    | OpenExecutionLease
    | CompleteExecutionLease
    | HeartbeatExecutionLease
    | RequestExecutionCancel
    | AppendExecutionEvent
    | ListExecutionEvents
    | AttachExecutionArtifact
    | ListExecutionArtifacts
    | CreateExecutionCheckpoint
    | GetExecutionCheckpoint
    | RequestRecoveryStaffReview
    | ListExecutionLeases
    | ListExecutionCheckpoints
    | DecideExecutionCheckpoint
    | RawCoordinationCommand
)


class CoordinationResultKind(StrEnum):
    ENTITY = "entity"
    COLLECTION = "collection"
    ACKNOWLEDGEMENT = "acknowledgement"


@dataclass(frozen=True)
class LedgerRecord:
    values: Mapping[str, object]

    def require_str(self, field: str) -> str:
        value = self.values.get(field)
        if not isinstance(value, str) or not value:
            raise ValueError(f"coordination record requires non-empty string {field!r}")
        return value


@dataclass(frozen=True)
class EntityResult:
    command: CoordinationCommandName
    field: str
    entity: LedgerRecord
    metadata: LedgerRecord
    kind: CoordinationResultKind = CoordinationResultKind.ENTITY


@dataclass(frozen=True)
class CollectionResult:
    command: CoordinationCommandName
    field: str
    items: tuple[LedgerRecord, ...]
    kind: CoordinationResultKind = CoordinationResultKind.COLLECTION


@dataclass(frozen=True)
class AcknowledgementResult:
    command: CoordinationCommandName
    payload: LedgerRecord
    kind: CoordinationResultKind = CoordinationResultKind.ACKNOWLEDGEMENT


type CoordinationResult = EntityResult | CollectionResult | AcknowledgementResult

_ENTITY_FIELDS: Mapping[CoordinationCommandName, str] = {
    CoordinationCommandName.AMEND_SAGA_MILESTONE: "milestone",
    CoordinationCommandName.GET_SAGA: "saga",
    CoordinationCommandName.GET_POW_WOW: "pow_wow",
    CoordinationCommandName.CREATE_SAGA_MILESTONE: "milestone",
    CoordinationCommandName.GET_SAGA_MILESTONE: "milestone",
    CoordinationCommandName.GET_GAWD_DOC: "gawd_doc",
    CoordinationCommandName.GET_ARTIFACT: "artifact",
    CoordinationCommandName.OPEN_EXECUTION_LEASE: "lease",
    CoordinationCommandName.COMPLETE_EXECUTION_LEASE: "lease",
    CoordinationCommandName.HEARTBEAT_EXECUTION_LEASE: "lease",
    CoordinationCommandName.REQUEST_EXECUTION_CANCEL: "lease",
    CoordinationCommandName.APPEND_EXECUTION_EVENT: "event",
    CoordinationCommandName.ATTACH_EXECUTION_ARTIFACT: "execution_artifact",
    CoordinationCommandName.CREATE_EXECUTION_CHECKPOINT: "checkpoint",
    CoordinationCommandName.GET_EXECUTION_CHECKPOINT: "checkpoint",
    CoordinationCommandName.REQUEST_RECOVERY_STAFF_REVIEW: "intent",
    CoordinationCommandName.DECIDE_EXECUTION_CHECKPOINT: "checkpoint",
}

_COLLECTION_FIELDS: Mapping[CoordinationCommandName, str] = {
    CoordinationCommandName.LIST_DISPATCH_INTENTS: "intents",
    CoordinationCommandName.LIST_SAGA_MILESTONES: "milestones",
    CoordinationCommandName.LIST_APPROVAL_REQUESTS: "requests",
    CoordinationCommandName.LATEST_REPO_AUDIT: "artifacts",
    CoordinationCommandName.LIST_INTEGRATION_REQUESTS: "requests",
    CoordinationCommandName.LIST_TASKS: "tasks",
    CoordinationCommandName.LIST_POW_WOWS: "pow_wows",
    CoordinationCommandName.LIST_SAGAS: "sagas",
    CoordinationCommandName.LIST_EXECUTION_LEASES: "leases",
    CoordinationCommandName.LIST_LEDGER_EVENTS: "events",
    CoordinationCommandName.LIST_EXECUTION_EVENTS: "events",
    CoordinationCommandName.LIST_EXECUTION_ARTIFACTS: "execution_artifacts",
    CoordinationCommandName.LIST_EXECUTION_CHECKPOINTS: "checkpoints",
}


def parse_coordination_result(
    command: CoordinationCommand,
    payload: Mapping[str, object],
) -> CoordinationResult:
    """Parse a subprocess payload into a finite result sum type."""

    entity_field = _ENTITY_FIELDS.get(command.name)
    if entity_field is not None:
        entity = payload.get(entity_field)
        if not isinstance(entity, Mapping):
            raise ValueError(f"{command.name.value} response requires {entity_field!r}")
        return EntityResult(
            command.name,
            entity_field,
            LedgerRecord(entity),
            LedgerRecord(payload),
        )
    collection_field = _COLLECTION_FIELDS.get(command.name)
    if collection_field is not None:
        items = payload.get(collection_field)
        if not isinstance(items, list) or not all(isinstance(item, Mapping) for item in items):
            raise ValueError(f"{command.name.value} response requires {collection_field!r} list")
        return CollectionResult(
            command.name,
            collection_field,
            tuple(LedgerRecord(item) for item in items),
        )
    return AcknowledgementResult(command.name, LedgerRecord(payload))


def spill_payload_location(command: CoordinationCommand) -> tuple[int, CoordinationFlag] | None:
    """Identify the large payload position from typed command data."""

    argv = command.to_argv()
    if command.name is CoordinationCommandName.SUBMIT_ARTIFACT and len(argv) >= 4:
        return 3, CoordinationFlag.CONTENT_FILE
    for source_flag, file_flag in (
        (CoordinationFlag.RESULT, CoordinationFlag.RESULT_FILE),
        (CoordinationFlag.PAYLOAD, CoordinationFlag.PAYLOAD_FILE),
    ):
        if source_flag.value in argv:
            return argv.index(source_flag.value) + 1, file_flag
    return None


def path_argument(path: Path) -> str:
    return str(path.expanduser())


__all__ = [
    "AcknowledgementResult",
    "AmendSagaMilestone",
    "AppendExecutionEvent",
    "AttachExecutionArtifact",
    "ApprovalDecision",
    "ApproveGawdDoc",
    "AttachGawdDocToSaga",
    "ClaimNextDispatchIntent",
    "ClaimTask",
    "CollectionResult",
    "CompleteDispatchIntent",
    "CompleteExecutionLease",
    "CompletePowWow",
    "CompleteTask",
    "CoordinationCommand",
    "CoordinationCommandName",
    "CoordinationFlag",
    "CoordinationResult",
    "CoordinationResultKind",
    "CreateGawdDoc",
    "CreateExecutionCheckpoint",
    "CreatePowWow",
    "CreateSaga",
    "CreateSagaMilestone",
    "DispatchKind",
    "DispatchReduce",
    "DispatchTerminalStatus",
    "DispatchTier",
    "EntityResult",
    "FailTask",
    "ExecutionLeaseTerminalStatus",
    "DecideExecutionCheckpoint",
    "GetGawdDoc",
    "GetExecutionCheckpoint",
    "HeartbeatExecutionLease",
    "LedgerRecord",
    "ListApprovalRequests",
    "ListDispatchIntents",
    "ListExecutionCheckpoints",
    "ListExecutionLeases",
    "ListExecutionEvents",
    "ListExecutionArtifacts",
    "ListSagaMilestones",
    "ListSagas",
    "NextReadySagaMilestone",
    "OpenExecutionLease",
    "LatestRepoAudit",
    "ListIntegrationRequests",
    "RawCoordinationCommand",
    "ResolveApprovalRequest",
    "RevokeApprovalRequest",
    "RequestExecutionCancel",
    "RequestRecoveryStaffReview",
    "RetrySagaMilestone",
    "SubmitApprovalRequest",
    "SubmitArtifact",
    "SubmitDispatchIntent",
    "TerminalOutcome",
    "TypedCoordinationCommand",
    "parse_coordination_result",
    "spill_payload_location",
]
