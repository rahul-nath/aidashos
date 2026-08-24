# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import os
import re
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from .constants import APPROVAL_REQUEST_TYPES, DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS
from .work_units.events import OperatorDecision

SCHEMA_VERSION_INGRESS = "ingress_event.v1"
SCHEMA_VERSION_WORKFLOW_RESULT = "workflow_result.v1"
SCHEMA_VERSION_PI_TASK = "pi_task.v1"
SCHEMA_VERSION_MODEL_CALL = "model_call.v1"
SCHEMA_VERSION_MEDICAL_REPORT = "med_report.v1"
SCHEMA_VERSION_TRAINING_STUB = "training_manifest.v0_stub"
WORKFLOW_VERSION = "v1"
CHUNKER_VERSION = "text_chunker_v1"


def enum_values_by_name(enum_class: type[StrEnum]) -> dict[str, str]:
    return {member.name: member.value for member in enum_class}


class WorkspaceId(StrEnum):
    GENERAL = "general"
    WHITEBOARD_OCR = "whiteboard_ocr"
    PAPER_NOTES = "paper_notes"
    APPLE_NOTES = "apple_notes"
    WORKFLOWY = "workflowy"
    CHROME = "chrome"
    AUDIO = "audio"
    MEDICAL = "medical"
    TRAINING = "training"


class SourceType(StrEnum):
    FILE = "file"
    APPLE_NOTES = "apple_notes"
    WORKFLOWY = "workflowy"
    MANUAL = "manual"
    SCHEDULED = "scheduled"


class TerminalActionKind(StrEnum):
    DIRECTIVE = "directive"
    QUERY = "query"


@dataclass(frozen=True)
class TerminalAction:
    kind: TerminalActionKind
    text: str
    model_role: ModelRole | None = None
    model_selector: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", TerminalActionKind(self.kind))
        if self.model_role is not None:
            object.__setattr__(self, "model_role", ModelRole(self.model_role))


class BareTerminalDirective(StrEnum):
    START = "start"
    STOP = "stop"
    GET = "get"
    COMPACT = "compact"
    TIMER = "timer"
    STORE = "store"
    EMBED = "embed"
    OCR = "ocr"
    SCREENSHOT = "screenshot"
    SEND_TO_WF = "send-to-wf"
    DONE = "done"
    CHROME = "chrome"
    LEDGER = "ledger"
    READ = "read"
    SAGA = "saga"
    POW_WOW = "pow-wow"
    AMBIGUITY = "ambiguity"
    STAGNATION = "stagnation"
    PROJECT_STATUS = "project-status"
    GRAPH = "graph"


BARE_TERMINAL_DIRECTIVES = frozenset(directive.value for directive in BareTerminalDirective)


class WorkflowType(StrEnum):
    GENERAL_QUESTIONS = "general_questions"
    WHITEBOARD_OCR = "whiteboard_ocr"
    PAPER_NOTES_OCR = "paper_notes_ocr"
    APPLE_NOTES_SYNC = "apple_notes_sync"
    WORKFLOWY_SYNC = "workflowy_sync"
    WORKFLOWY_WRITE = "workflowy_write"
    AUDIO_TRANSCRIPTION = "audio_transcription"
    EMBEDDER = "embedder"
    MEDICAL_IMAGE_ANALYZER = "medical_image_analyzer"
    TRAINING_EXPORT_STUB = "training_export_stub"
    MODEL_DIRECTIVE = "model_directive"
    AGENT_QUERY = "agent_query"
    OCR_CAPTURE = "ocr_capture"
    DIRECTORY_EMBEDDING = "directory_embedding"
    CONTEXT_COMPACTION = "context_compaction"
    SEND_TO_WORKFLOWY = "send_to_workflowy"
    DONE_RECALL = "done_recall"
    CHROME_CONTROL = "chrome_control"
    WHITEBOARD_INTENT = "whiteboard_intent"
    CREATE_TOMORROW = "create_tomorrow"
    GRAPH_EXTRACTION = "graph_extraction"
    GRAPH_ANALYTICS = "graph_analytics"
    # The workflow a resident local delegate opens for itself so its model calls
    # have a parent. `model_invocations.workflow_id` is NOT NULL REFERENCES
    # workflow_runs, and `get_workflow_run_state` coerces the column back through
    # this enum, so a free-form value would write a row nothing can read.
    RESIDENT_LOCAL_DELEGATE = "resident_local_delegate"


WhiteboardOcrExtension = Literal[".jpg", ".jpeg", ".png", ".webp", ".heic"]
PaperNotesOcrExtension = Literal[".jpg", ".jpeg", ".png", ".webp", ".heic", ".pdf"]
AudioTranscriptionExtension = Literal[".m4a", ".mp3", ".wav", ".aac", ".flac"]
MedicalImageAnalyzerExtension = Literal[".jpg", ".jpeg", ".png", ".dcm"]


class WorkflowStatus(StrEnum):
    CREATED = "CREATED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_PERMANENT = "FAILED_PERMANENT"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    UNSUPPORTED_STUB = "UNSUPPORTED_STUB"
    CANCELLED = "CANCELLED"


class Stage(StrEnum):
    DETECTED = "DETECTED"
    STABILIZING = "STABILIZING"
    REGISTERED = "REGISTERED"
    VALIDATED = "VALIDATED"
    ROUTED = "ROUTED"
    MODEL_LOADING = "MODEL_LOADING"
    PROCESSING = "PROCESSING"
    ARTIFACT_PERSISTED = "ARTIFACT_PERSISTED"
    EMBEDDING_PENDING = "EMBEDDING_PENDING"
    EMBEDDING_COMPLETED = "EMBEDDING_COMPLETED"
    RERANKED = "RERANKED"
    EGRESS_PENDING = "EGRESS_PENDING"
    COMPLETED = "COMPLETED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    UNSUPPORTED_STUB = "UNSUPPORTED_STUB"


class ArtifactRole(StrEnum):
    SOURCE_FILE = "source_file"
    SOURCE_IMAGE = "source_image"
    OCR_INPUT_IMAGE = "ocr_input_image"
    AGENT_QUERY_RECORD = "agent_query_record"
    PROMPT = "prompt"
    OCR_TEXT = "ocr_text"
    OCR_BATCH_MANIFEST = "ocr_batch_manifest"
    NORMALIZED_TEXT = "normalized_text"
    NOTES_SNAPSHOT = "notes_snapshot"
    WORKFLOWY_NODE_SNAPSHOT = "workflowy_node_snapshot"
    TRANSCRIPT = "transcript"
    MED_REPORT = "med_report"
    CANDIDATE_SET = "candidate_set"
    PI_DECISION = "pi_decision"
    DIRECTIVE_RESULT = "directive_result"
    STORE_MANIFEST = "store_manifest"
    CONTEXT_COMPACTION = "context_compaction"
    SESSION_CONTEXT = "session_context"
    MODEL_OUTPUT = "model_output"
    ANSWER = "answer"
    TRAINING_MANIFEST = "training_manifest"
    UNSUPPORTED_STUB = "unsupported_stub"
    SEND_TO_WF_PAYLOAD = "send_to_wf_payload"
    DONE_RECALL_RESULT = "done_recall_result"
    CHROME_CONTROL_RESULT = "chrome_control_result"
    BROWSER_ACCEPTANCE_REQUEST = "browser_acceptance_request"
    BROWSER_ACCEPTANCE_EVIDENCE = "browser_acceptance_evidence"
    BROWSER_SCREENSHOT = "browser_screenshot"
    WHITEBOARD_INTENT_GRAPH = "whiteboard_intent_graph"
    WHITEBOARD_CORPUS_EVIDENCE = "whiteboard_corpus_evidence"
    WHITEBOARD_DIFF = "whiteboard_diff"
    DAILY_VIEW_PATCH = "daily_view_patch"
    ENTITY_GRAPH = "entity_graph"
    GRAPH_METRICS = "graph_metrics"


class AgentHarness(StrEnum):
    """A frontier CLI reachable as a direct query target.

    The value is the adapter's own `name`, so a harness routes to its adapter
    without a second lookup table to keep in sync.
    """

    CLAUDE_CODE = "claude_code"
    CODEX_CLI = "codex_cli"


class ModelRole(StrEnum):
    GENERAL = "general"
    GENERAL_FALLBACK = "general_fallback"
    # The heavyweight local seat. Distinct from GENERAL, which is the fast
    # default every terminal query and junior task hits and which gemma4 holds on
    # a measured 14x latency advantage. A role whose model costs ~20GB resident
    # and answers in tens of seconds is not the same kind of thing, and giving it
    # its own name is what keeps the fast path from quietly inheriting that cost.
    DELIBERATOR = "deliberator"
    OCR = "ocr"
    HARD_OCR = "hard_ocr"
    ASR = "asr"
    EMBEDDER = "embedder"
    MEDICAL = "medical"
    COMPACTOR = "compactor"


class EgressStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    DEDUPED = "deduped"
    DENIED = "denied"


class IngressEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["ingress_event.v1"] = SCHEMA_VERSION_INGRESS
    event_id: str
    source_type: SourceType
    event_type: str
    workspace_id: str
    source_uri: str
    content_sha256: str | None = None
    detected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = Field(default_factory=dict)


class ArtifactRef(BaseModel):
    artifact_id: str
    role: ArtifactRole | str
    uri: str
    sha256: str
    mime_type: str
    size_bytes: int
    schema_version: str

    @computed_field
    @property
    def path(self) -> str:
        if self.uri.startswith("file://"):
            return self.uri[7:]
        return self.uri


class WorkflowResult(BaseModel):
    schema_version: Literal["workflow_result.v1"] = SCHEMA_VERSION_WORKFLOW_RESULT
    workflow_id: str
    workflow_type: WorkflowType
    status: WorkflowStatus
    current_stage: Stage
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    egress_ids: list[str] = Field(default_factory=list)
    embedding_degraded: bool = False
    manual_review_reason: str | None = None
    help: dict[str, Any] | None = None


class WorkflowRunState(BaseModel):
    """The durable state used to decide whether a workflow may run again."""

    workflow_id: str
    workflow_type: WorkflowType
    status: WorkflowStatus
    current_stage: Stage
    retry_count: int = 0
    last_error: str | None = None


class ModelCallRequest(BaseModel):
    schema_version: Literal["model_call.v1"] = SCHEMA_VERSION_MODEL_CALL
    workflow_id: str
    model_role: ModelRole
    input_artifact_id: str
    payload: dict[str, Any]
    params: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS


class ModelCallResult(BaseModel):
    invocation_id: str
    model_role: ModelRole
    model_id: str
    output_artifact: ArtifactRef
    latency_ms: int
    status: str = "completed"
    error: str | None = None


class PiTask(BaseModel):
    schema_version: Literal["pi_task.v1"] = SCHEMA_VERSION_PI_TASK
    workflow_id: str
    workspace_id: str
    task_type: str
    prompt: str
    allowed_tools: list[str]
    forbidden_tools: list[str] = Field(default_factory=list)
    input_artifacts: list[str] = Field(default_factory=list)
    output_schema: str
    max_turns: int = 4


class WorkflowyDestinationDecision(BaseModel):
    schema_version: Literal["workflowy_destination_decision.v1"] = (
        "workflowy_destination_decision.v1"
    )
    action: Literal["propose_insert", "no_action", "manual_review"]
    target_node_id: str | None = None
    target_reason: str
    confidence: float = Field(ge=0, le=1)
    requires_manual_review: bool = False

    @field_validator("target_node_id")
    @classmethod
    def target_required_for_insert(cls, value: str | None, info: Any) -> str | None:
        if info.data.get("action") == "propose_insert" and not value:
            raise ValueError("target_node_id is required for propose_insert")
        return value


class MedicalReport(BaseModel):
    schema_version: Literal["med_report.v1"] = SCHEMA_VERSION_MEDICAL_REPORT
    summary: str
    visible_findings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    non_diagnostic_disclaimer: str
    review_required: Literal[True] = True
    diagnostic_claims_forbidden: Literal[True] = True
    confidence: float = Field(ge=0, le=1)


class StableTextChunk(BaseModel):
    chunk_id: str
    artifact_id: str
    workspace_id: str
    chunk_index: int
    text: str
    text_sha256: str
    embedding_model_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkspacePolicy(BaseModel):
    workspace_id: str
    root_path: Path
    allowed_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=lambda: ["bash", "raw_http"])
    approved_workflowy_parent_ids: list[str] = Field(default_factory=list)
    write_enabled: bool = False
    embed_medical_outputs: bool = False
    decision_confidence_threshold: float = 0.85


REASONING_SHORTHAND: Final = re.compile(r"^(?:(off|full)|bounded\(\s*(\d+)\s*\))$")


class ReasoningPolicy(BaseModel):
    """How much a model may think, said in terms no chat template owns.

    Every model spells this differently. llama.cpp has `--reasoning on|off` and
    `--reasoning-budget N`; Qwen-family templates read `enable_thinking`;
    Muse-Glimmer's template reads `reasoning_strength` and defaults it to
    `high`. A caller that wants "think a little here" should not have to know
    which of those the destination model happens to speak, so the registry
    declares intent and `ModelSpec.reasoning_dialect` owns the spelling.

    Written in config as `off`, `bounded(256)`, or `full`.
    """

    model_config = ConfigDict(frozen=True)

    mode: Literal["off", "bounded", "full"]
    budget_tokens: int | None = Field(default=None, ge=1)

    @model_validator(mode="before")
    @classmethod
    def accept_shorthand(cls, value: Any) -> Any:
        """Config writes `bounded(256)`; nothing downstream should parse strings."""
        if not isinstance(value, str):
            return value
        match = REASONING_SHORTHAND.match(value.strip())
        if match is None:
            raise ValueError(f"reasoning must be 'off', 'full', or 'bounded(N)'; got {value!r}")
        keyword, budget = match.groups()
        if keyword:
            return {"mode": keyword}
        return {"mode": "bounded", "budget_tokens": int(budget)}

    @model_validator(mode="after")
    def budget_belongs_to_bounded(self) -> ReasoningPolicy:
        """A budget on `off` or `full` is a contradiction, not a default to drop."""
        if self.mode == "bounded" and self.budget_tokens is None:
            raise ValueError("reasoning bounded(N) requires a token budget")
        if self.mode != "bounded" and self.budget_tokens is not None:
            raise ValueError(f"reasoning {self.mode} must not carry a token budget")
        return self


class SpeculativeDecoding(BaseModel):
    """Per-model llama.cpp speculative decoding; absent means none."""

    type: Literal[
        "draft-simple",
        "draft-eagle3",
        "draft-mtp",
        "draft-dflash",
        "draft-dspark",
        "ngram-simple",
        "ngram-map-k",
        "ngram-map-k4v",
        "ngram-mod",
        "ngram-cache",
    ]
    draft_n_max: int | None = Field(default=None, ge=1)
    draft_n_min: int | None = Field(default=None, ge=0)
    # Where the draft weights come from, which is not the same question as which
    # speculation algorithm runs. A `draft-mtp` head can be built into the main
    # GGUF, in which case there is nothing to point at and this stays None; it
    # can equally ship as its own file, in which case llama.cpp needs `-md` and
    # silently speculates nothing without it. qwen3.6-27b-mtp was the first kind
    # and qwen3.8-27b-mtp is the second, so the distinction had to become data
    # the moment the second one arrived.
    draft_gguf_path: str | None = None

    @field_validator("draft_gguf_path")
    @classmethod
    def expand_draft_artifact_path(cls, value: str | None) -> str | None:
        """Same argv-facing invariant as ModelSpec's artifact paths.

        llama.cpp expands nothing, so the absolute path has to be established at
        a boundary rather than demanded of the config file, which keeps one
        operator's home directory out of the checked-in registry.
        """
        if value is None:
            return None
        expanded = os.path.expandvars(os.path.expanduser(value))
        if not Path(expanded).is_absolute():
            raise ValueError(f"draft model path must resolve to an absolute path: {value}")
        return expanded


class ModelSpec(BaseModel):
    alias: str
    role: ModelRole
    model_id: str
    server_model_name: str
    runtime: str = "llama.cpp"
    backend: str = "metal"
    gguf_path: str | None = None
    ggml_path: str | None = None
    mmproj_path: str | None = None
    coreml_path: str | None = None
    warm_ttl_seconds: int = 300
    priority: int = 100
    pinned: bool = False
    context_window: int | None = None
    parallel: int | None = Field(default=None, ge=1)
    speculative: SpeculativeDecoding | None = None
    server_url: str | None = None
    port: int | None = None
    language: str | None = None
    translate: bool = False
    threads: int | None = None
    image_first: bool = False
    # llama.cpp routes text emitted after a `<think>` tag into the response's
    # `reasoning_content` field. Models that open a thought tag but return their
    # real answer inside it (chandra-ocr-2) leave `content` empty under the
    # server default, so record here which parsing mode the model needs.
    reasoning_format: Literal["none", "deepseek"] | None = None
    # How much this model may think. Until 2026-08-14 this key existed only in
    # `configs/model_registry.toml`, where `gen_llama_presets.py` read it straight
    # off the raw TOML; `ModelSpec` never declared it, so pydantic dropped it and
    # `DEFAULT_MODELS` could not express it at all. The registry falls back to
    # `DEFAULT_MODELS` when the TOML has no `[models]`, and every model would have
    # come up thinking - the opposite of the setting the file was written to hold.
    reasoning: ReasoningPolicy | None = None
    # Which chat-template variable carries `reasoning` to this model. Declared
    # rather than inferred, because guessing is how a request quietly does
    # nothing: Muse-Glimmer's template contains no `enable_thinking` at all, so
    # sending that key is accepted and ignored, and the model thinks anyway.
    reasoning_dialect: Literal["reasoning_strength"] | None = None

    def reasoning_request_overrides(self) -> dict[str, Any]:
        """The request-body fragment that makes this model honor `reasoning`.

        Empty when the model declares no dialect, which leaves the server-side
        default from the preset in charge and is the correct behavior for a
        model whose template exposes no per-request lever.

        The `reasoning_strength` dialect is a graded instruction rather than a
        token cap, so a `bounded(N)` budget maps to the nearest band instead of
        being enforced. Measured on Muse-Glimmer 30B on 2026-08-14, one bounded
        classification, reasoning tokens generated: none 66, low 68, medium 84,
        high 158 (the template's own default). Calling that an enforced budget
        would be a stronger claim than the lever supports.
        """
        if self.reasoning is None or self.reasoning_dialect is None:
            return {}
        if self.reasoning_dialect == "reasoning_strength":
            match self.reasoning.mode:
                case "off":
                    strength = "none"
                case "full":
                    strength = "high"
                case "bounded":
                    budget = self.reasoning.budget_tokens or 0
                    strength = "low" if budget <= 256 else "medium"
            return {"chat_template_kwargs": {"reasoning_strength": strength}}
        assert_never(self.reasoning_dialect)

    # Long-edge pixel budget for images sent to this model. Beyond a model's own
    # internal cap the extra pixels are discarded after costing decode time, so
    # the ceiling belongs to the destination model rather than the caller.
    ocr_max_dimension: int | None = Field(default=None, ge=256)
    default_params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("gguf_path", "ggml_path", "mmproj_path", "coreml_path")
    @classmethod
    def expand_model_artifact_path(cls, value: str | None) -> str | None:
        """Expand here so every reader downstream holds an absolute path.

        llama.cpp and whisper.cpp receive these as argv and expand nothing
        themselves, so an absolute path is the invariant they need. Doing the
        expansion at this boundary keeps that invariant without forcing one
        operator's home directory into a checked-in config file.
        """
        if value is None:
            return None
        expanded = os.path.expandvars(os.path.expanduser(value))
        if not Path(expanded).is_absolute():
            raise ValueError(f"model artifact path must resolve to an absolute path: {value}")
        return expanded


class FileBound(BaseModel):
    extensions: AbstractSet[str]
    max_bytes: int
    max_pages: int | None = None
    terminal_on_violation: WorkflowStatus = WorkflowStatus.FAILED_PERMANENT


DirectiveAction = Literal[
    "start",
    "stop",
    "get",
    "fetch",
    "compact",
    "timer",
    "store",
    "ocr_capture",
    "agent_query",
    "screenshot",
    "send_to_wf",
    "done",
    "chrome",
    "saga",
    "pow_wow",
    "ambiguity_check",
    "stagnation_check",
    "dispatcher",
    "dispatch_once",
    "ledger",
    "new_project",
    "approved_gawd",
    "approve_most_recent",
    "review_merge",
    "approve_merge",
    "try_milestone",
    "observability",
    "status",
    "project_status",
    "graph",
]


class GraphSubcommand(StrEnum):
    BUILD = "build"
    ANALYZE = "analyze"
    GET = "get"
    NODE = "node"
    STATS = "stats"
    REVIEW = "review"
    REBUILD = "rebuild"


GRAPH_SUBCOMMANDS = tuple(subcommand.value for subcommand in GraphSubcommand)

WalkthruAction = Literal[
    "start",
    "answer",
    "accept",
    "revise",
    "edit",
    "skip",
    "status",
    "finish",
]


@dataclass(frozen=True)
class DirectiveSpec:
    raw: str
    action: DirectiveAction
    model_role: ModelRole | None = None
    query: str | None = None
    path: Path | None = None
    remote: bool = False
    alias: str | None = None
    query_tail: str | None = None
    month_day: str | None = None
    chrome_action: str | None = None
    chrome_args: tuple[str, ...] = ()
    agent_harness: AgentHarness | None = None
    # Dispatcher / reactor fields
    dispatcher_name: str | None = None
    dispatcher_tier: Literal["junior", "senior", "staff"] | None = None
    dispatcher_interval_seconds: float | None = None
    dispatcher_max_polls: int | None = None
    target_project_id: str | None = None
    create_target_id: str | None = None
    walkthru_action: WalkthruAction | None = None
    walkthru_id: str | None = None
    walkthru_section_id: str | None = None
    walkthru_text: str | None = None
    retrieval_source: Literal["workflowy"] | None = None
    # Saga / pow-wow fields
    saga_id: str | None = None
    pow_wow_stage: str | None = None
    budget_tokens: int | None = None
    saga_executor_backend: Literal["dry_run", "fake_process", "cli"] | None = None
    saga_worktree_root: Path | None = None
    # Knowledge graph fields
    graph_subcommand: GraphSubcommand | None = None


@dataclass(frozen=True)
class DirectiveHelp:
    summary: str
    suggestions: list[str]
    canonical_examples: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "suggestions": self.suggestions,
            "canonical_examples": self.canonical_examples,
        }


@dataclass
class SearchHit:
    chunk_id: str
    artifact_id: str
    workspace_id: str
    text: str
    score: float
    metadata: dict[str, Any]


@dataclass(frozen=True)
class DumpSummary:
    chunks_written: int
    artifacts_written: int
    output_path: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunks_written": self.chunks_written,
            "artifacts_written": self.artifacts_written,
            "output_path": str(self.output_path),
        }


@dataclass(frozen=True)
class RestoreSummary:
    chunks_restored: int
    artifacts_restored: int
    source_path: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunks_restored": self.chunks_restored,
            "artifacts_restored": self.artifacts_restored,
            "source_path": str(self.source_path),
        }


class CompactionPayload(BaseModel):
    schema_version: Literal["context_compaction.v1", "context_compaction.v2"]
    status: Literal["compacted", "not_needed"]
    compacted_context: str = ""


@dataclass
class PiRequestContext:
    workspace_id: str = WorkspaceId.GENERAL.value
    shell_session_id: str | None = None
    model_selector: str | None = None
    model_id: str | None = None
    source_workspace_id: str | None = None
    retrieval_sources: list[str] | None = None
    context: str | None = None
    max_window_tokens: int | None = None
    streaming: bool = True


class QuestionRequest(BaseModel):
    prompt: str = Field(max_length=256_000)
    workspace_id: str = WorkspaceId.GENERAL.value
    use_dbos: bool = False


class FileIngressRequest(BaseModel):
    path: str
    workspace_id: str
    workflow_type: WorkflowType
    event_type: str = "created"
    stable: bool = False
    use_dbos: bool = False


class WorkUnitDecisionRequest(BaseModel):
    """One operator decision, naming exactly the request it resolves.

    ``request_id`` is required. A decision that does not name a persisted request
    cannot unblock anything, which is what keeps an approving-sounding message from
    acting as an approval. ``idempotency_key`` makes a re-delivered submission
    harmless.
    """

    request_id: str = Field(min_length=1)
    decision: OperatorDecision
    idempotency_key: str = Field(min_length=1)
    decided_by: str = "operator"
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkflowyWriteRequest(BaseModel):
    parent_node_id: str
    content: str
    workspace_id: str = WorkspaceId.WORKFLOWY.value
    use_dbos: bool = False


class PiDirectiveRequest(BaseModel):
    text: str = Field(max_length=64_000)
    workspace_id: str = WorkspaceId.GENERAL.value
    session_id: str | None = None
    context: str | None = Field(default=None, max_length=512_000)
    max_window_tokens: int | None = None


def parse_file_uri(uri: str) -> Path:
    if uri.startswith("file://"):
        return Path(uri[7:])
    return Path(uri)


# ---------------------------------------------------------------------------
# Whiteboard intent / daily plan contracts
# ---------------------------------------------------------------------------

SCHEMA_VERSION_WHITEBOARD_INTENT = "whiteboard_intent.v1"
SCHEMA_VERSION_WHITEBOARD_CORPUS_EVIDENCE = "whiteboard_corpus_evidence.v1"
SCHEMA_VERSION_WHITEBOARD_DIFF = "whiteboard_diff.v1"
SCHEMA_VERSION_DAILY_VIEW_PATCH = "daily_view_patch.v1"


class WhiteboardExtractionMode(StrEnum):
    """How a whiteboard snapshot became an intent graph.

    STRUCTURED means the perception model returned parseable group structure.
    FLAT_FALLBACK means only flat text was recoverable; the loss is recorded
    as data instead of being hidden, mirroring the ViewBlock convention.
    """

    STRUCTURED = "structured"
    FLAT_FALLBACK = "flat_fallback"


class IntentNovelty(StrEnum):
    """How an item relates to the corpus, computed inside a workflow invocation.

    This is a temporary interpretation derived on demand from stored matches.
    It is never persisted as a label on the corpus; only match evidence is
    durable.
    """

    NEW = "new"
    DUPLICATE = "duplicate"
    UPDATE = "update"
    AMBIGUOUS = "ambiguous"


class WhiteboardIntentItem(BaseModel):
    """One written item on the board; layout and ink are data, not noise."""

    text: str
    ink_color: str | None = None
    crossed_out: bool = False
    region_hint: str | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)


class WhiteboardIntentGroup(BaseModel):
    """A spatial/color cluster of items that reads as one initiative."""

    label: str | None = None
    ink_color: str | None = None
    inferred_project: str | None = None
    confidence: float = Field(default=0.5, ge=0, le=1)
    items: list[WhiteboardIntentItem] = Field(default_factory=list)


class WhiteboardIntentGraph(BaseModel):
    schema_version: Literal["whiteboard_intent.v1"] = SCHEMA_VERSION_WHITEBOARD_INTENT
    source_artifact_id: str
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    extraction_mode: WhiteboardExtractionMode
    groups: list[WhiteboardIntentGroup] = Field(default_factory=list)

    def flattened_items(self) -> list[tuple[int, int, WhiteboardIntentItem]]:
        return [
            (group_index, item_index, item)
            for group_index, group in enumerate(self.groups)
            for item_index, item in enumerate(group.items)
        ]


class WorkflowyMatch(BaseModel):
    """One corpus candidate for an extracted item, with score provenance."""

    chunk_id: str
    score: float
    semantic_score: float
    lexical_score: float
    text_excerpt: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class CorpusMatchedItem(BaseModel):
    """One board item plus its corpus match evidence, with no interpretation.

    Novelty, routine-ness, and similar labels are computed inside the workflow
    that needs them; only the matches themselves are durable evidence.
    """

    group_index: int
    item_index: int
    text: str
    crossed_out: bool = False
    matches: list[WorkflowyMatch] = Field(default_factory=list)


class WhiteboardCorpusEvidence(BaseModel):
    schema_version: Literal["whiteboard_corpus_evidence.v1"] = (
        SCHEMA_VERSION_WHITEBOARD_CORPUS_EVIDENCE
    )
    graph_artifact_id: str
    extraction_mode: WhiteboardExtractionMode
    items: list[CorpusMatchedItem] = Field(default_factory=list)


class DisappearanceEvidence(StrEnum):
    """What the evidence says about an item that left the board.

    Inferred only when the evidence is strong; otherwise it stays UNRESOLVED
    rather than forcing the operator to label the case.
    """

    TRANSFERRED = "transferred"
    COMPLETED = "completed"
    UNRESOLVED = "unresolved"


class DisappearedItem(BaseModel):
    text: str
    evidence: DisappearanceEvidence
    best_match_chunk_id: str | None = None
    best_match_top_level: str | None = None
    rationale: str


class WhiteboardDiff(BaseModel):
    """Evidence-backed comparison of consecutive snapshots of one board."""

    schema_version: Literal["whiteboard_diff.v1"] = SCHEMA_VERSION_WHITEBOARD_DIFF
    previous_graph_artifact_id: str
    current_graph_artifact_id: str
    appeared: list[str] = Field(default_factory=list)
    persisted: list[str] = Field(default_factory=list)
    disappeared: list[DisappearedItem] = Field(default_factory=list)


class WorkflowyOutlineNode(BaseModel):
    """One bullet in a proposed Workflowy outline."""

    text: str
    children: list[WorkflowyOutlineNode] = Field(default_factory=list)


class InterpretationMode(StrEnum):
    """Whether a model interpreted the instruction or a skeleton stood in."""

    MODEL_STRUCTURED = "model_structured"
    FALLBACK_SKELETON = "fallback_skeleton"


class DailyViewPatch(BaseModel):
    """A materialized daily view proposed for one dated top-level node.

    The patch is a temporary execution view generated from the sources for one
    specific instruction; it is not a canonical task taxonomy. A human approves
    before anything reaches Workflowy.
    """

    schema_version: Literal["daily_view_patch.v1"] = SCHEMA_VERSION_DAILY_VIEW_PATCH
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    instruction: str
    target_top_level: str
    interpretation_mode: InterpretationMode
    sections: list[WorkflowyOutlineNode] = Field(default_factory=list)
    evidence_artifact_id: str | None = None
    diff_artifact_id: str | None = None
    requires_approval: Literal[True] = True
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Saga / Pow-wow / GAWD doc contracts
# ---------------------------------------------------------------------------

SCHEMA_VERSION_GAWD_DOC = "gawd_doc.v1"
SCHEMA_VERSION_SAGA = "saga.v1"
SCHEMA_VERSION_POW_WOW = "pow_wow.v1"
SCHEMA_VERSION_EVALUATION = "evaluation_result.v1"
SCHEMA_VERSION_DRIFT_REPORT = "drift_report.v1"


class SagaStatus(StrEnum):
    PLANNING = "PLANNING"
    ACTIVE = "ACTIVE"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    STAGNATED = "STAGNATED"


class PowWowStatus(StrEnum):
    FORMING = "FORMING"
    ACTIVE = "ACTIVE"
    EVALUATING = "EVALUATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SagaStage(StrEnum):
    IDEA_INTAKE = "IDEA_INTAKE"
    GAWD_DOC = "GAWD_DOC"
    REQUIREMENT_DECOMPOSITION = "REQUIREMENT_DECOMPOSITION"
    IMPLEMENTATION = "IMPLEMENTATION"
    REVIEW_EVALUATION = "REVIEW_EVALUATION"
    USER_APPROVAL = "USER_APPROVAL"


class Mind(StrEnum):
    """Atomic cognitive primitives. Each mind asks one core question.

    Minds are summoned by Characters into sub-pow-wows; a summon returns a
    structured artifact, not tool access.
    """

    SOCRATES = "socrates"  # "What are you assuming?" (subsumes contrarian inversion)
    ONTOLOGIST = "ontologist"  # "What IS this, really?"
    SEED_ARCHITECT = "seed_architect"  # "Is this complete and unambiguous?"
    EVALUATOR = "evaluator"  # "Did we build the right thing?"
    HACKER = "hacker"  # "What constraints are actually real?"
    SIMPLIFIER = "simplifier"  # "What's the simplest thing that could work?"
    RESEARCHER = "researcher"  # "What evidence do we actually have?"
    ARCHITECT = "architect"  # "If we started over, would we build it this way?"


class CharacterName(StrEnum):
    """Personas instantiated as agents. Compose summonable minds + tier + tools."""

    STAFF_ENGINEER = "staff_engineer"
    SENIOR_ENGINEER = "senior_engineer"
    JUNIOR_ENGINEER = "junior_engineer"
    PRODUCT_OWNER = "product_owner"
    IDEATOR = "ideator"
    REALIST = "realist"


class FunctionalRole(StrEnum):
    """Deterministic helpers — tools wearing a hat, not personas with judgment."""

    SECURITY_AGENT = "security_agent"
    QA_AGENT = "qa_agent"
    CODE_REVIEWER = "code_reviewer"
    TEST_RUNNER = "test_runner"
    GAWD_DOC_CREATOR = "gawd_doc_creator"
    NOTE_NORMALIZER = "note_normalizer"
    DOC_GUIDELINE_CHECKER = "doc_guideline_checker"
    SUMMARIZER = "summarizer"


AnyRole = Mind | CharacterName | FunctionalRole


class AgentTier(StrEnum):
    """Weak = local/small; Strong = frontier; Special = approval-board roles."""

    WEAK = "weak"
    STRONG = "strong"
    SPECIAL = "special"


class EvaluationType(StrEnum):
    MECHANICAL = "MECHANICAL"  # tests, lint, build, typecheck
    SEMANTIC = "SEMANTIC"  # compare output to GAWD requirements
    CONSENSUS = "CONSENSUS"  # multi-agent verdict


class ApprovalRequestType(StrEnum):
    PURCHASE = "PURCHASE"
    EXTERNAL_COMMS = "EXTERNAL_COMMS"
    CODE_MERGE = "CODE_MERGE"
    MODEL_ESCALATION = "MODEL_ESCALATION"
    REVIEW_ESCALATION = "REVIEW_ESCALATION"
    GENERAL = "GENERAL"


if tuple(member.value for member in ApprovalRequestType) != APPROVAL_REQUEST_TYPES:
    raise AssertionError(
        "contracts.ApprovalRequestType drifted from constants.APPROVAL_REQUEST_TYPES"
    )


class ProjectActionKind(StrEnum):
    """Mutually exclusive operator actions projected by the project cockpit."""

    WORKING = "WORKING"
    WAITING_FOR_MODEL = "WAITING_FOR_MODEL"
    RECOVERABLE_FAILURE = "RECOVERABLE_FAILURE"
    HUMAN_DECISION_REQUIRED = "HUMAN_DECISION_REQUIRED"
    MERGE_APPROVAL_REQUIRED = "MERGE_APPROVAL_REQUIRED"
    MERGE_INTEGRATION_REQUIRED = "MERGE_INTEGRATION_REQUIRED"
    DEPLOY_APPROVAL_REQUIRED = "DEPLOY_APPROVAL_REQUIRED"
    BLOCKED = "BLOCKED"
    COMPLETE = "COMPLETE"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    # A task claimed by a session that stopped heartbeating. Distinct from
    # FAILED, which means the task ran and did not succeed.
    ABANDONED = "ABANDONED"


class LeaseStatus(StrEnum):
    """Every state an agent execution lease can occupy.

    ExecutionLeaseTerminalStatus names only the five an executor settles into;
    the column also holds the two live states.
    """

    ACTIVE = "ACTIVE"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELED = "CANCELED"
    COMPENSATED = "COMPENSATED"


class LedgerEventStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    # An outbox row has no heartbeat, so a consumer that dies mid-claim leaves
    # nothing behind that could ever resolve it. ABANDONED is that fact.
    ABANDONED = "ABANDONED"


class ProgressAssessmentStatus(StrEnum):
    """Whether a stalled lease has been assessed, and how that turned out."""

    NOT_REQUESTED = "NOT_REQUESTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class CheckpointStatus(StrEnum):
    PENDING_JUNIOR = "PENDING_JUNIOR"
    DECIDED = "DECIDED"
    PAUSED = "PAUSED"
    FAILED = "FAILED"


class GawdDocStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    SUPERSEDED = "SUPERSEDED"


class DispatchIntentStatus(StrEnum):
    """Every state a dispatch intent can occupy.

    DispatchTerminalStatus names only the two an executor reports back. This is
    the full column vocabulary, which is what a reader or a comparison needs.
    """

    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    IN_PROGRESS = "IN_PROGRESS"
    CHECKPOINT_REVIEW = "CHECKPOINT_REVIEW"
    PAUSED = "PAUSED"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    SUPERSEDED = "SUPERSEDED"


class DispatchProgress(StrEnum):
    """What a waiter should do about an intent's current status.

    Three answers, not two. A waiter used to ask only "is this terminal", so
    ``PAUSED`` and ``CHECKPOINT_REVIEW`` answered the same as ``CLAIMED`` - keep
    waiting - and a milestone whose intent had paused burned its whole 1800s
    before reporting `dispatch_wait_elapsed`, which was not what happened.

    ``PARKED`` is the missing third: the intent has stopped moving on its own and
    will not move again without a decision. Waiting on it is waiting on a person.
    """

    SETTLED = "SETTLED"  # it is over; read the row and translate it
    PARKED = "PARKED"  # it stopped, pending a decision that is not the waiter's
    ACTIVE = "ACTIVE"  # somebody is or will be working on it


def classify_dispatch_progress(status: DispatchIntentStatus) -> DispatchProgress:
    """Partition every dispatch status into the three a waiter can act on.

    Exhaustive, so a new ``DispatchIntentStatus`` is a type error here rather
    than silently joining ``ACTIVE`` and inheriting a full-timeout wait. This is
    the single source of truth for the split; the sets below are derived from it
    rather than restated, which is how two modules came to spell "settled"
    identically while disagreeing about ``SUPERSEDED``.
    """

    match status:
        case (
            DispatchIntentStatus.DONE
            | DispatchIntentStatus.FAILED
            | DispatchIntentStatus.CANCELED
            | DispatchIntentStatus.SUPERSEDED
        ):
            return DispatchProgress.SETTLED
        case DispatchIntentStatus.PAUSED | DispatchIntentStatus.CHECKPOINT_REVIEW:
            return DispatchProgress.PARKED
        case (
            DispatchIntentStatus.PENDING
            | DispatchIntentStatus.CLAIMED
            | DispatchIntentStatus.IN_PROGRESS
        ):
            return DispatchProgress.ACTIVE
    assert_never(status)


def dispatch_statuses_with(progress: DispatchProgress) -> frozenset[DispatchIntentStatus]:
    """Every status that classifies as ``progress``, derived not restated."""

    return frozenset(
        status for status in DispatchIntentStatus if classify_dispatch_progress(status) is progress
    )


# An intent in one of these has stopped moving, so a waiter can stop asking.
# Deliberately NOT the set a quorum parent settles on: SUPERSEDED means the work
# was replaced, which ends the wait for this intent and does not end the parent's
# wait for its replacement. Those are two questions, so they get two names.
TERMINAL_DISPATCH_INTENT_STATUSES = dispatch_statuses_with(DispatchProgress.SETTLED)

# An intent here has also stopped moving, and a waiter must also stop asking -
# but for the opposite reason. Terminal means the answer exists; parked means it
# is waiting on somebody. Reporting a parked intent as a timeout says the agent
# never answered, when the ledger knows it stopped on purpose.
PARKED_DISPATCH_INTENT_STATUSES = dispatch_statuses_with(DispatchProgress.PARKED)


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DENIED = "DENIED"
    REVOKED = "REVOKED"


class MilestoneStatus(StrEnum):
    """The states a saga milestone can occupy.

    Saga status and stage are a projection of these, so this enum is the closed
    set that projection must account for. It was previously a private set of
    string literals, which let BLOCKED exist as a writable status that no
    projection rule mentioned.
    """

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


# --- GAWD doc ---


class GawdDoc(BaseModel):
    """Goal-Aligned Work Definition document.

    Immutable once approved. Changes create a new version, never silent edits.
    """

    schema_version: Literal["gawd_doc.v1"] = SCHEMA_VERSION_GAWD_DOC
    gawd_doc_id: str
    saga_id: str | None = None
    version: int = 1
    goal: str
    constraints: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    task_graph: dict[str, Any] = Field(default_factory=dict)
    status: Literal["DRAFT", "APPROVED", "SUPERSEDED"] = "DRAFT"
    superseded_by: str | None = None


class AmbiguityScore(BaseModel):
    """Clarity scores for a GAWD doc (ambiguity gate)."""

    gawd_doc_id: str
    goal_clarity: float = Field(ge=0, le=1)
    constraints_clarity: float = Field(ge=0, le=1)
    success_criteria_clarity: float = Field(ge=0, le=1)
    unresolved_critical: int = Field(ge=0)
    ready_to_execute: bool
    passes: dict[str, bool] = Field(default_factory=dict)
    scores: dict[str, float | int] = Field(default_factory=dict)


# --- Saga ---


class SagaBudget(BaseModel):
    budget_tokens: int = 1_000_000
    budget_seconds: int = 86400
    tokens_used: int = 0

    @computed_field
    @property
    def tokens_remaining(self) -> int:
        return max(0, self.budget_tokens - self.tokens_used)

    @computed_field
    @property
    def budget_fraction_used(self) -> float:
        if self.budget_tokens == 0:
            return 1.0
        return round(self.tokens_used / self.budget_tokens, 4)


class Saga(BaseModel):
    schema_version: Literal["saga.v1"] = SCHEMA_VERSION_SAGA
    saga_id: str
    goal: str
    gawd_doc_id: str | None = None
    current_stage: SagaStage = SagaStage.IDEA_INTAKE
    status: SagaStatus = SagaStatus.PLANNING
    budget: SagaBudget = Field(default_factory=SagaBudget)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None


# --- Pow-wow ---


class PowWowRosterEntry(BaseModel):
    agent_name: str
    role: AnyRole | str
    allowed_tools: list[str] = Field(default_factory=list)
    tier: AgentTier = AgentTier.WEAK


class PowWow(BaseModel):
    """A bounded, staged multi-agent cohort within a saga."""

    schema_version: Literal["pow_wow.v1"] = SCHEMA_VERSION_POW_WOW
    pow_wow_id: str
    saga_id: str
    stage: SagaStage | str
    goal: str
    input_artifacts: list[str] = Field(default_factory=list)
    roster: list[PowWowRosterEntry] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    budget_tokens: int = 100_000
    exit_criteria: str = ""
    required_outputs: list[str] = Field(default_factory=list)
    carryover_agents: list[str] = Field(default_factory=list)
    status: PowWowStatus = PowWowStatus.FORMING
    output_summary: str | None = None
    cycle_count: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# --- Evaluation ---


class EvaluationResult(BaseModel):
    schema_version: Literal["evaluation_result.v1"] = SCHEMA_VERSION_EVALUATION
    eval_id: str
    artifact_id: str
    pow_wow_id: str
    evaluator_agent: str | None = None
    eval_type: EvaluationType
    score: float = Field(ge=0, le=1)
    passed: bool
    notes: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EvaluationSummary(BaseModel):
    """Aggregated evaluation verdict for a pow-wow."""

    pow_wow_id: str
    by_type: dict[str, Any] = Field(default_factory=dict)
    overall_pass: bool
    verdict: Literal["PASS", "FAIL"]


# --- Drift ---


class DriftReport(BaseModel):
    """Compares pow-wow outputs against the GAWD doc requirements."""

    schema_version: Literal["drift_report.v1"] = SCHEMA_VERSION_DRIFT_REPORT
    pow_wow_id: str
    gawd_doc_id: str
    drift_detected: bool
    drift_score: float = Field(ge=0, le=1)
    drift_reasons: list[str] = Field(default_factory=list)
    new_requirements_invented: list[str] = Field(default_factory=list)
    ignored_constraints: list[str] = Field(default_factory=list)
    meaning_changed: bool = False
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# --- Stagnation ---


class StagnationReport(BaseModel):
    saga_id: str
    stagnated: bool
    delta_ratio: float = 0.0
    threshold: float = 0.10
    reason: str = ""
    recommendation: str | None = None
    pow_wows_checked: list[str] = Field(default_factory=list)


# --- Approval ---


class ApprovalRequest(BaseModel):
    approval_id: str
    saga_id: str
    request_type: ApprovalRequestType
    payload: dict[str, Any] = Field(default_factory=dict)
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_by: str | None = None
    resolved_by: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None


# --- Agent capabilities ---


class AgentCapability(BaseModel):
    """What an agent can do. Role is advisory; permissions are granted separately."""

    agent_id: str
    agent_name: str
    role: AnyRole | str
    tier: AgentTier
    allowed_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    max_context_tokens: int = 32_768
    can_spawn_subagents: bool = False
    requires_approval_for: list[ApprovalRequestType] = Field(default_factory=list)


class ToolPermissionGrant(BaseModel):
    """Explicit runtime tool-permission grant (not role-derived)."""

    grant_id: str
    agent_name: str
    tool_name: str
    task_id: str | None = None
    pow_wow_id: str | None = None
    reason: str
    status: Literal["PENDING", "GRANTED", "DENIED"] = "PENDING"
    granted_by: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None


# ---------------------------------------------------------------------------
# Characters: composed personas (tier + budget + summonable minds + tools)
# ---------------------------------------------------------------------------

SCHEMA_VERSION_MIND_SUMMON_REQUEST = "mind_summon_request.v1"
SCHEMA_VERSION_MIND_SUMMON_RESULT = "mind_summon_result.v1"


class Character(BaseModel):
    """A persona that can be instantiated as an agent.

    Tool namespace is OWN_ONLY: a Character has its own ``allowed_tools``.
    When it summons a Mind, the Mind runs as a sub-pow-wow with its own
    ToolPermissionGrant; the Character receives a structured artifact in
    return and never inherits the Mind's tool access.
    """

    name: CharacterName
    tier: AgentTier
    summonable_minds: list[Mind] = Field(default_factory=list)
    budget_tokens_per_stage: int = 100_000
    allowed_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    tool_locality: Literal["local_only", "any"] = "any"
    can_escalate_to: list[CharacterName] = Field(default_factory=list)
    prompt_prefix: str = ""

    def can_summon(self, mind: Mind) -> bool:
        return mind in self.summonable_minds


CHARACTERS: dict[CharacterName, Character] = {
    CharacterName.STAFF_ENGINEER: Character(
        name=CharacterName.STAFF_ENGINEER,
        tier=AgentTier.STRONG,
        summonable_minds=[
            Mind.SOCRATES,
            Mind.ONTOLOGIST,
            Mind.SEED_ARCHITECT,
            Mind.ARCHITECT,
            Mind.SIMPLIFIER,
        ],
        budget_tokens_per_stage=300_000,
        tool_locality="any",
        prompt_prefix=(
            "You are a staff-level engineer. You define failure semantics and durable "
            "boundaries. You think structurally — find the cause, not the symptom. "
            "Summon Ontologist to name what something really is, Seed Architect to "
            "crystallize specs, Architect to question structural decisions, "
            "Simplifier to remove what doesn't serve the goal, Socrates to challenge "
            "assumptions. You do not inherit a summoned Mind's tools — you receive its "
            "report and decide."
        ),
    ),
    CharacterName.SENIOR_ENGINEER: Character(
        name=CharacterName.SENIOR_ENGINEER,
        tier=AgentTier.STRONG,
        summonable_minds=[Mind.SOCRATES, Mind.SIMPLIFIER, Mind.HACKER],
        budget_tokens_per_stage=150_000,
        tool_locality="any",
        can_escalate_to=[CharacterName.STAFF_ENGINEER],
        prompt_prefix=(
            "You are a senior engineer. You implement and ship. Distinguish real "
            "constraints from assumed ones. Summon Hacker for unconventional paths, "
            "Simplifier when scope creeps, Socrates when assumptions feel load-bearing. "
            "Escalate to Staff Engineer if confidence drops below threshold."
        ),
    ),
    CharacterName.JUNIOR_ENGINEER: Character(
        name=CharacterName.JUNIOR_ENGINEER,
        tier=AgentTier.WEAK,
        summonable_minds=[Mind.HACKER],
        budget_tokens_per_stage=40_000,
        tool_locality="local_only",
        can_escalate_to=[CharacterName.SENIOR_ENGINEER],
        prompt_prefix=(
            "You are a junior engineer specialized in Workflowy and local info "
            "management. You run on local models only and never reach external "
            "services. Pair with Hacker for ideation; escalate to Senior Engineer "
            "when blocked."
        ),
    ),
    CharacterName.PRODUCT_OWNER: Character(
        name=CharacterName.PRODUCT_OWNER,
        tier=AgentTier.STRONG,
        summonable_minds=[Mind.SOCRATES, Mind.RESEARCHER],
        budget_tokens_per_stage=120_000,
        tool_locality="any",
        prompt_prefix=(
            "You own the user-facing intent. You hold the goal steady against scope "
            "drift. Summon Researcher when claims need evidence, Socrates when "
            "stakeholder asks rest on hidden assumptions."
        ),
    ),
    CharacterName.IDEATOR: Character(
        name=CharacterName.IDEATOR,
        tier=AgentTier.STRONG,
        summonable_minds=[Mind.HACKER],
        budget_tokens_per_stage=80_000,
        tool_locality="any",
        prompt_prefix=(
            "You generate possibilities. You expand the option space before anyone "
            "narrows it. Summon Hacker to find unconventional paths."
        ),
    ),
    CharacterName.REALIST: Character(
        name=CharacterName.REALIST,
        tier=AgentTier.STRONG,
        summonable_minds=[Mind.SOCRATES, Mind.SIMPLIFIER],
        budget_tokens_per_stage=80_000,
        tool_locality="any",
        prompt_prefix=(
            "You ground proposals in what is actually true and feasible. Summon "
            "Socrates to surface assumptions, Simplifier to strip away nice-to-haves."
        ),
    ),
}


class MindSummonRequest(BaseModel):
    """A Character requests a Mind sub-pow-wow.

    The summon spawns a new agent with the Mind's prompt prefix and its own
    ToolPermissionGrant scope. The summoner does not gain the Mind's tools.
    """

    schema_version: Literal["mind_summon_request.v1"] = SCHEMA_VERSION_MIND_SUMMON_REQUEST
    summon_id: str
    parent_pow_wow_id: str
    summoning_character: CharacterName
    summoning_agent_id: str
    mind: Mind
    framing_question: str
    input_artifact_ids: list[str] = Field(default_factory=list)
    budget_tokens: int = 5_000


class MindSummonResult(BaseModel):
    schema_version: Literal["mind_summon_result.v1"] = SCHEMA_VERSION_MIND_SUMMON_RESULT
    summon_id: str
    mind: Mind
    output_artifact: ArtifactRef
    tokens_used: int
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def get_character(name: CharacterName) -> Character:
    return CHARACTERS[name]


def can_character_summon(name: CharacterName, mind: Mind) -> bool:
    """Authorization check enforced at summon time."""
    character = CHARACTERS.get(name)
    return bool(character and character.can_summon(mind))


# --- Pre-defined pow-wow stages (canonical saga structure) ---

# ---------------------------------------------------------------------------
# Minds registry
# ---------------------------------------------------------------------------


class MindSpec(BaseModel):
    """Specification for a single Mind."""

    role: Mind
    core_question: str
    prompt_prefix: str
    tier: AgentTier = AgentTier.STRONG
    never_builds: bool = False


MINDS: list[MindSpec] = [
    MindSpec(
        role=Mind.SOCRATES,
        core_question="What are you assuming?",
        prompt_prefix="You ask questions only. You never suggest solutions or write code. "
        "Surface every hidden assumption — what is being treated as fact "
        "that is actually assumption? Then invert the premise: what would "
        "the strongest counter-argument look like? What if the opposite "
        "were true? Ask: What are you assuming?",
        never_builds=True,
    ),
    MindSpec(
        role=Mind.ONTOLOGIST,
        core_question="What IS this, really?",
        prompt_prefix="You find the essence, not the symptoms. Strip away labels and ask: "
        "What IS this, really? What category does this truly belong to?",
    ),
    MindSpec(
        role=Mind.SEED_ARCHITECT,
        core_question="Is this complete and unambiguous?",
        prompt_prefix="You crystallize specs from dialogue. Check completeness and "
        "ambiguity. Ask: Is this complete and unambiguous? "
        "What would a machine need to execute this without asking questions?",
    ),
    MindSpec(
        role=Mind.EVALUATOR,
        core_question="Did we build the right thing?",
        prompt_prefix="You run 3-stage verification: mechanical (tests pass?), "
        "semantic (does it match requirements?), consensus (do multiple "
        "reviewers agree?). Ask: Did we build the right thing?",
    ),
    MindSpec(
        role=Mind.HACKER,
        core_question="What constraints are actually real?",
        prompt_prefix="You find unconventional paths. Question every constraint. "
        "Ask: What constraints are actually real vs. assumed? "
        "What is the minimal change that achieves the goal?",
    ),
    MindSpec(
        role=Mind.SIMPLIFIER,
        core_question="What's the simplest thing that could work?",
        prompt_prefix="You remove complexity. Eliminate every element that doesn't "
        "serve the core goal. Ask: What's the simplest thing that could work?",
    ),
    MindSpec(
        role=Mind.RESEARCHER,
        core_question="What evidence do we actually have?",
        prompt_prefix="You stop coding and start investigating. Demand evidence. "
        "Ask: What evidence do we actually have? "
        "What are we treating as fact that is actually assumption?",
    ),
    MindSpec(
        role=Mind.ARCHITECT,
        core_question="If we started over, would we build it this way?",
        prompt_prefix="You identify structural causes. Step back from the current design. "
        "Ask: If we started over, would we build it this way? "
        "What structural decision created this problem?",
    ),
]

MINDS_BY_KEY: dict[str, MindSpec] = {m.role.value: m for m in MINDS}


# ---------------------------------------------------------------------------
# Skills — always-on behavioral guardrails composed into pow-wow prompts.
#
# A Skill is not a Mind (one-shot sub-pow-wow returning a structured artifact)
# and not a Character (rostered persona with judgment). It is a prompt fragment
# layered onto whoever happens to be rostered when the stage/role filters match.
# ---------------------------------------------------------------------------


class SkillName(StrEnum):
    KARPATHY_CODING_DISCIPLINE = "karpathy_coding_discipline"


class SkillSpec(BaseModel):
    name: SkillName
    prompt_name: str  # key into PiPromptRegistry (configs/pi_prompts.toml)
    applies_to_stages: list[SagaStage] = Field(default_factory=list)
    applies_to_characters: list[CharacterName] = Field(default_factory=list)
    applies_to_functional_roles: list[FunctionalRole] = Field(default_factory=list)


SKILLS: list[SkillSpec] = [
    SkillSpec(
        name=SkillName.KARPATHY_CODING_DISCIPLINE,
        prompt_name="skill.karpathy_coding_discipline",
        applies_to_stages=[
            SagaStage.REQUIREMENT_DECOMPOSITION,
            SagaStage.IMPLEMENTATION,
            SagaStage.REVIEW_EVALUATION,
        ],
        applies_to_characters=[
            CharacterName.STAFF_ENGINEER,
            CharacterName.SENIOR_ENGINEER,
            CharacterName.JUNIOR_ENGINEER,
        ],
        applies_to_functional_roles=[
            FunctionalRole.CODE_REVIEWER,
            FunctionalRole.QA_AGENT,
            FunctionalRole.TEST_RUNNER,
        ],
    ),
]


# ---------------------------------------------------------------------------
# Canonical saga stages
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Knowledge graph layer (`entity_graph.v1`)
#
# The graph is a derived index over the immutable artifact store, never a
# source of truth. Everything below is storage-agnostic on purpose: the
# extraction, resolution, and analytics stages are written against these
# contracts, so the graph backend stays a late-bound, reversible choice.
# ---------------------------------------------------------------------------

SCHEMA_VERSION_ENTITY_GRAPH = "entity_graph.v1"
SCHEMA_VERSION_GRAPH_METRICS = "graph_metrics.v1"


class GraphWriteOutcome(StrEnum):
    """Whether a graph upsert created a row or folded into an existing one."""

    CREATED = "created"
    MERGED = "merged"


class NodeResolutionPath(StrEnum):
    """How an extracted entity found its node.

    An enum rather than a bool because the three cases are what the §10 runbook
    reads: an `EMBEDDING` merge is a resolution collision worth alerting on, an
    `EXACT` merge is routine, and `NEW` is neither.
    """

    NEW = "new"
    EXACT = "exact"
    EMBEDDING = "embedding"


class ExtractedEntity(BaseModel):
    name: str
    node_type: str
    description: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0, le=1)
    snippet: str = ""


class ExtractedRelation(BaseModel):
    src_name: str
    dst_name: str
    edge_type: str
    confidence: float = Field(ge=0, le=1)
    snippet: str = ""


class EntityGraph(BaseModel):
    """The `entity_graph.v1` artifact payload: one artifact's extraction.

    This is the durable record the whole graph is re-derivable from, which is
    why it names the ontology version and extractor that produced it.
    """

    schema_version: Literal["entity_graph.v1"] = SCHEMA_VERSION_ENTITY_GRAPH
    source_artifact_id: str
    ontology_version: str
    extractor_model_id: str
    entities: list[ExtractedEntity] = Field(default_factory=list)
    relations: list[ExtractedRelation] = Field(default_factory=list)
    truncated: bool = False


class GraphNode(BaseModel):
    node_id: str
    node_type: str
    canonical_name: str
    normalized_name: str
    aliases: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    mention_count: int = 0
    needs_review: bool = False
    first_seen_artifact_id: str = ""
    pagerank: float | None = None
    degree: int | None = None
    community_id: int | None = None


class GraphEdge(BaseModel):
    edge_id: str
    src_node_id: str
    dst_node_id: str
    edge_type: str
    confidence: float = Field(ge=0, le=1)
    weight: int = 1
    source_artifact_ids: list[str] = Field(default_factory=list)
    needs_review: bool = False


class GraphMention(BaseModel):
    mention_id: str
    node_id: str
    artifact_id: str
    chunk_id: str | None = None
    snippet: str = ""


class GraphNeighborhood(BaseModel):
    """Retrieval-time contract fed into the `general` model's context."""

    seed_node_ids: list[str] = Field(default_factory=list)
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    mention_snippets: list[dict[str, str]] = Field(default_factory=list)


class GraphMetrics(BaseModel):
    """The `graph_metrics.v1` artifact payload written by the analytics pass."""

    schema_version: Literal["graph_metrics.v1"] = SCHEMA_VERSION_GRAPH_METRICS
    node_count: int
    edge_count: int
    community_count: int
    top_nodes: list[dict[str, Any]] = Field(default_factory=list)


class Ontology(BaseModel):
    """The closed entity/relation type sets from `configs/ontology.toml`.

    Closed is the point: a type outside these sets is dropped at the extraction
    boundary rather than admitted, which is what keeps the graph queryable.
    """

    model_config = ConfigDict(frozen=True)

    ontology_version: str = "v1"
    entity_types: dict[str, str] = Field(default_factory=dict)
    relation_types: dict[str, str] = Field(default_factory=dict)

    def allows_entity(self, node_type: str) -> bool:
        return node_type in self.entity_types

    def allows_relation(self, edge_type: str) -> bool:
        return edge_type in self.relation_types


class GraphExtractionBounds(BaseModel):
    model_config = ConfigDict(frozen=True)

    extractor_role: ModelRole = ModelRole.GENERAL
    resolution_threshold: float = Field(default=0.86, ge=0, le=1)
    review_threshold: float = Field(default=0.55, ge=0, le=1)
    max_extraction_chars: int = Field(default=24000, gt=0)
    max_entities_per_artifact: int = Field(default=60, gt=0)
    max_batch_artifacts: int = Field(default=500, gt=0)


class GraphRetrievalBounds(BaseModel):
    model_config = ConfigDict(frozen=True)

    max_hops: int = Field(default=2, ge=1)
    max_neighbors: int = Field(default=40, ge=0)


class GraphConfig(BaseModel):
    """Everything `configs/ontology.toml` declares, in one parsed value."""

    model_config = ConfigDict(frozen=True)

    ontology: Ontology = Field(default_factory=Ontology)
    extraction: GraphExtractionBounds = Field(default_factory=GraphExtractionBounds)
    retrieval: GraphRetrievalBounds = Field(default_factory=GraphRetrievalBounds)

    @classmethod
    def from_toml(cls, config: dict[str, Any]) -> GraphConfig:
        return cls(
            ontology=Ontology(
                ontology_version=str(config.get("ontology_version", "v1")),
                entity_types=dict(config.get("entity_types") or {}),
                relation_types=dict(config.get("relation_types") or {}),
            ),
            extraction=GraphExtractionBounds.model_validate(config.get("extraction") or {}),
            retrieval=GraphRetrievalBounds.model_validate(config.get("retrieval") or {}),
        )
