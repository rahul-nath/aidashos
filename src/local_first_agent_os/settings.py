# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import tomllib
from enum import StrEnum
from functools import lru_cache
from os import getenv
from pathlib import Path
from types import NoneType, UnionType
from typing import Annotated, Any, Literal, Union, get_args, get_origin
from urllib.parse import urlsplit

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, SettingsConfigDict

from .access_posture import AccessPosture
from .constants import (
    DEFAULT_ARTIFACT_WRITE_TIMEOUT_SECONDS,
    DEFAULT_COORDINATION_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS,
    DEFAULT_PI_STREAM_HEARTBEAT_SECONDS,
    DEFAULT_PI_STREAM_IDLE_TIMEOUT_SECONDS,
    DEFAULT_PROGRESS_ASSESSMENT_TIMEOUT_SECONDS,
    DEFAULT_SAGA_TASK_TIMEOUT_SECONDS,
    DEFAULT_STREAM_DRAIN_TIMEOUT_SECONDS,
    LOCAL_AGENT_STATE_DIR_NAME,
)
from .vocabulary import GovernedSagaDoorPosture

LedgerOutboxName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _optional_field_keys(model: type[BaseSettings]) -> frozenset[str]:
    """Every key that can carry an optional field's value, name and aliases both.

    Settings sources hand values over under whichever key matched, so a field
    declared with `validation_alias=AliasChoices(...)` arrives under one of
    those alias strings rather than under its Python name. A lookup keyed only
    by field name silently misses exactly the fields that name their own
    environment variables, which is most of the ones a .env file sets.
    """

    keys: set[str] = set()
    for name, field in model.model_fields.items():
        if not _field_accepts_none(field):
            continue
        keys.add(name)
        alias = field.validation_alias
        if isinstance(alias, str):
            keys.add(alias)
        elif isinstance(alias, AliasChoices):
            keys.update(choice for choice in alias.choices if isinstance(choice, str))
    return frozenset(keys)


def _field_accepts_none(field: FieldInfo) -> bool:
    """Whether a field's declared type admits None.

    Read off the annotation rather than off the default, because the two answer
    different questions. `Settings` has optional fields whose default is not
    None, and required fields do not have a default at all, so a default-based
    test would be wrong in both directions.
    """

    annotation = field.annotation
    if annotation is None or annotation is NoneType:
        return True
    if get_origin(annotation) in {Union, UnionType}:
        return any(argument in {None, NoneType} for argument in get_args(annotation))
    return False


class DisabledLedgerOutbox(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["disabled"] = "disabled"


class ConfiguredLedgerOutbox(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["configured"]
    consumer: LedgerOutboxName
    topic: LedgerOutboxName


LedgerOutboxConfig = Annotated[
    DisabledLedgerOutbox | ConfiguredLedgerOutbox,
    Field(discriminator="mode"),
]


class DatabaseIdentity(BaseModel):
    """Which database a process is pointed at, with no room for a credential.

    `/health` is unauthenticated by necessity. The Compose healthcheck and the
    Kubernetes readiness and liveness probes cannot present a credential, so
    everything that endpoint reports is readable by whatever can reach the port.
    It used to report `database_url` whole, which published the password in
    every configuration that had one.

    This is a type rather than a redaction pass on purpose. No field here can
    hold userinfo, so a later edit cannot reintroduce the password by forgetting
    to strip it. Putting it back would mean adding a field that does not belong,
    which is a visible change rather than a silent omission.

    What survives is what the endpoint exists to answer: am I pointed at the
    database I think I am.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: str
    host: str | None = None
    port: int | None = None
    database: str | None = None

    @classmethod
    def from_url(cls, url: str) -> DatabaseIdentity:
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError:
            # A non-numeric port never reaches a driver, so this is already a
            # broken configuration. `/health` still answering matters more than
            # it answering completely: the liveness probe kills the pod on a
            # 500, and a crashlooping pod is a worse report of "your port is
            # malformed" than a null.
            port = None
        # One leading slash is the separator; any beyond it belong to the value.
        # `postgresql://host/local_agent` names a database and
        # `sqlite:////data/app.sqlite3` names an absolute path, and stripping
        # greedily would quietly turn the second into a relative one.
        database = parsed.path.removeprefix("/") or None
        return cls(
            backend=parsed.scheme or "unknown",
            host=parsed.hostname,
            port=port,
            database=database,
        )


class CoordinationTransportKind(StrEnum):
    """Which side of a process boundary a coordination command runs on.

    An enum rather than a boolean because these are two working states an
    operator picks between, not a knob. `IN_PROCESS` is the application calling
    its own functions; `SUBPROCESS` re-executes the packaged CLI, paying an
    interpreter start per command and inheriting the parent's stdio.
    """

    IN_PROCESS = "in_process"
    SUBPROCESS = "subprocess"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LOCAL_AGENT_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = Field(
        default="local_first_agent_os",
        validation_alias=AliasChoices("LOCAL_AGENT_APP_NAME", "DBOS_APPLICATION_NAME"),
    )
    application_version: str = Field(
        default="0.1.0",
        validation_alias=AliasChoices("LOCAL_AGENT_APPLICATION_VERSION", "DBOS__APPVERSION"),
    )
    database_url: str = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/local_agent"
    dbos_system_database_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "LOCAL_AGENT_DBOS_SYSTEM_DATABASE_URL",
            "DBOS_SYSTEM_DATABASE_URL",
        ),
    )
    # The least-privileged way into the DBOS system database: a role granted the
    # two metadata columns the execution-ledger tallies read and nothing else. A
    # process given this URL instead of the admin one cannot reach a workflow's
    # inputs even if its code asks. See scripts/grant_execution_ledger_reader.sql.
    ledger_reader_database_url: str | None = None
    dbos_executor_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LOCAL_AGENT_DBOS_EXECUTOR_ID", "DBOS__VMID"),
    )
    dbos_conductor_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LOCAL_AGENT_DBOS_CONDUCTOR_KEY", "DBOS_CONDUCTOR_KEY"),
    )
    dbos_conductor_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LOCAL_AGENT_DBOS_CONDUCTOR_URL", "DBOS_CONDUCTOR_URL"),
    )
    dbos_conductor_executor_metadata: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "DBOS_CONDUCTOR_EXECUTOR_METADATA",
            "LOCAL_AGENT_DBOS_CONDUCTOR_EXECUTOR_METADATA",
        ),
    )
    # DBOS's built-in Python admin server binds 0.0.0.0 and offers no host
    # setting. Keep it off unless a deliberate network boundary isolates it.
    dbos_admin_server_enabled: bool = False
    use_dbos: bool = Field(
        default=False,
        description=(
            "Execute pi work through DBOS rather than in-process. Changes durability "
            "and recovery semantics an operator can observe. A boolean where "
            "saga_executor_backend shows the better shape: this wants to become "
            "pi_execution_backend with named states."
        ),
        json_schema_extra={"feature_flag": True},
    )
    env: str = "local"
    service_name: str = "local-agent"
    structured_logs: bool = True
    log_level: str = "INFO"
    lifecycle_log_dir: Path = Field(
        default_factory=lambda: Path.home() / LOCAL_AGENT_STATE_DIR_NAME / "logs"
    )
    lifecycle_maintenance_state_path: Path = Field(
        default_factory=lambda: (
            Path.home() / LOCAL_AGENT_STATE_DIR_NAME / "state" / "lifecycle-maintenance-latest.json"
        )
    )
    lifecycle_log_max_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    lifecycle_log_retained_tail_bytes: int = Field(default=5 * 1024 * 1024, ge=0)
    # How long scheduled maintenance keeps collectable audit evidence: execution
    # transcripts and artifacts not pinned by a checkpoint, terminal leases and
    # dispatch intents nothing still references, settled ledger events, notes,
    # and handoffs.  ``None`` keeps them forever.  Durable evidence in
    # ``task_artifacts`` and the saga tables is never in scope here; deleting a
    # project's evidence is an operator decision, not a scheduled one.
    lifecycle_retention_seconds: int | None = Field(default=90 * 24 * 60 * 60, gt=0)
    # Whether scheduled maintenance may delete session-artifact blobs that no
    # live transcript still references, or only count them.
    #
    # Off by default, and the asymmetry is deliberate rather than timid. The
    # content-addressed store is bounded by deduplication and unbounded in
    # time, so it does need collecting; but the things in it are images an
    # operator pasted, the reachability argument rests on every transcript root
    # being found, and a scheduled job that silently deletes them on a
    # first-run bug is a worse failure than a directory that grows. Reporting
    # first makes the argument inspectable in the maintenance record before
    # anyone acts on it.
    lifecycle_sweep_session_artifacts: bool = Field(
        default=False,
        json_schema_extra={"feature_flag": True},
    )
    # The durable outbox is operational delivery state, not an audit log.
    # Keep it disabled unless a named consumer and topic make every PENDING row
    # actionable. events.jsonl remains the local human-readable audit trail.
    ledger_outbox: LedgerOutboxConfig = Field(
        default_factory=DisabledLedgerOutbox,
        description=(
            "Whether ledger events are delivered to an external consumer. A "
            "discriminated union, so the disabled case cannot carry a half-filled "
            "consumer and topic."
        ),
        json_schema_extra={"feature_flag": True},
    )
    otel_traces_enabled: bool = Field(
        default=False,
        description="Export OpenTelemetry traces to otel_traces_endpoint.",
        json_schema_extra={"feature_flag": True},
    )
    otel_traces_endpoint: str = "http://127.0.0.1:4318/v1/traces"
    otel_traces_headers: dict[str, str] = Field(default_factory=dict)
    otel_traces_export_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    pyroscope_enabled: bool = Field(
        default=False,
        description="Send continuous profiles to pyroscope_server_address.",
        json_schema_extra={"feature_flag": True},
    )
    pyroscope_server_address: str = "http://127.0.0.1:4040"
    pyroscope_sample_rate: int = 100
    memory_profiling_enabled: bool = Field(
        default=False,
        description=(
            "Run the tracemalloc collector. The test suite forces this off, because a "
            "live collector turns an ordinary run into a profiling workload."
        ),
        json_schema_extra={"feature_flag": True},
    )
    memory_profile_top_n: int = 8
    memory_profile_sample_every: int = Field(default=20, ge=1)
    mock_models: bool = Field(
        default=False,
        description=(
            "Replace real inference with canned responses. Changes every downstream "
            "output, so it is a behaviour switch rather than wiring."
        ),
        json_schema_extra={"feature_flag": True},
    )
    artifact_root: Path = Field(
        default_factory=lambda: Path.home() / LOCAL_AGENT_STATE_DIR_NAME / "artifacts"
    )
    coordination_root: Path = Field(
        default_factory=lambda: (
            Path.home() / LOCAL_AGENT_STATE_DIR_NAME / "coordination" / "local_first_agent_os"
        )
    )
    projects_root: Path = Field(
        default_factory=lambda: Path.home() / "ai_projects",
        validation_alias=AliasChoices("LOCAL_AGENT_PROJECTS_ROOT"),
    )
    coordination_backend: Literal["postgres"] = Field(
        default="postgres",
        validation_alias=AliasChoices(
            "LOCAL_AGENT_COORDINATION_BACKEND",
            "AGENT_COORDINATION_BACKEND",
        ),
    )
    coordination_database_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "LOCAL_AGENT_COORDINATION_DATABASE_URL",
            "AGENT_COORDINATION_DATABASE_URL",
        ),
    )
    # Whether this application's own coordination commands cross a process
    # boundary. They always did, because the only transport the factory could
    # build was a subprocess, and the subprocess re-executes the packaged CLI:
    # a fresh interpreter re-importing the same package the caller already has
    # loaded, to call a function it could have called directly. Measured
    # 2026-07-31 against the live Postgres, `list_sagas` took 0.418s that way
    # and 0.007s in process. The stdio bug that `pi_handoff_to_daemon` works
    # around is downstream of the same fork: a child spawned from a resident
    # daemon inherits stdio the daemon no longer owns.
    # `subprocess` remains selectable, and is still the right answer for an
    # external agent calling this repository over MCP - that one is a separate
    # program and the boundary is real.
    coordination_transport: CoordinationTransportKind = Field(
        json_schema_extra={"feature_flag": True},
        default=CoordinationTransportKind.IN_PROCESS,
        validation_alias=AliasChoices(
            "LOCAL_AGENT_COORDINATION_TRANSPORT",
            "AGENT_COORDINATION_TRANSPORT",
        ),
    )
    saga_executor_backend: Literal["dry_run", "fake_process", "cli"] = Field(
        json_schema_extra={"feature_flag": True},
        default="cli",
        validation_alias=AliasChoices(
            "LOCAL_AGENT_SAGA_EXECUTOR",
            "LOCAL_AGENT_SAGA_EXECUTOR_BACKEND",
        ),
    )
    agent_ledger_read_access: bool = Field(
        json_schema_extra={"feature_flag": True},
        default=True,
        validation_alias="LOCAL_AGENT_AGENT_LEDGER_READ_ACCESS",
        description=(
            "Whether a dispatched frontier agent is handed a read-only MCP view of the "
            "coordination ledger: what has executed, who owns the resident loops, and "
            "what dispatch intents exist. The startup skill says execution history is a "
            "query rather than an inference, and until this existed a dispatched agent "
            "had no way to run that query and inferred instead. Read-only by "
            "construction: no verb it exposes can write, so an agent cannot file "
            "evidence about its own run. Off returns the spawn to no MCP configuration "
            "at all. See docs/completed/agent_ledger_read_access_design.md."
        ),
    )
    access_posture: AccessPosture = Field(
        json_schema_extra={"feature_flag": True},
        default=AccessPosture.ENFORCING,
        validation_alias="LOCAL_AGENT_ACCESS_POSTURE",
        description=(
            "Whether an access-control refusal stops the action ('enforcing') or is "
            "recorded while the action proceeds ('observing'). 'observing' exists so "
            "that young rules cannot cost an operator a manual test run; it still "
            "runs every check and logs each refusal it declined to make. It never "
            "relaxes the irreversible capabilities in "
            "access_posture.ALWAYS_ENFORCED, and the process says which posture it "
            "is in at startup and on /health."
        ),
    )
    governed_saga_door: GovernedSagaDoorPosture = Field(
        json_schema_extra={"feature_flag": True},
        default=GovernedSagaDoorPosture.RETIRED,
        validation_alias="LOCAL_AGENT_GOVERNED_SAGA_DOOR",
        description=(
            "Compatibility parser for the removed /start /approved-gawd governed "
            "execution lane. Every historical value now redirects to the "
            "compile_design_doc / start_work_unit path. Production WorkUnit "
            "2f8e57d35257795531717cfc796ef3ac satisfied the retirement gate in "
            "docs/completed/governed_saga_door_retirement_gawd.md."
        ),
    )
    saga_worktree_root: Path = Field(
        default_factory=lambda: (
            Path.home() / LOCAL_AGENT_STATE_DIR_NAME / "worktrees" / "local_first_agent_os"
        )
    )
    saga_task_timeout_seconds: int = Field(
        default=DEFAULT_SAGA_TASK_TIMEOUT_SECONDS,
        validation_alias="LOCAL_AGENT_SAGA_TASK_TIMEOUT_SECONDS",
    )
    refinery_poll_seconds: float = Field(
        default=15.0,
        gt=0,
        validation_alias="LOCAL_AGENT_REFINERY_POLL_SECONDS",
    )
    """How long the refinery sleeps when it had nothing to do.

    Only ever paid on an idle queue: a run that decided something polls again
    without sleeping, because the moment a batch finishes is exactly when its
    siblings are most likely to have arrived, and sleeping there would insert an
    artificial wait into the one instant that matters.

    Fifteen seconds is what a human feels between resolving an approval and
    seeing it picked up, and also how long a milestone blocked behind an
    approved-but-unmerged dependency stays blocked. Short enough that neither
    reads as stuck, long enough that an empty-queue poll is a rounding error
    against a system that spends minutes per model turn.

    A setting rather than a constant because the right value depends on how long
    the target project's verification takes, which is a property of somebody
    else's repository.
    """

    coordination_command_timeout_seconds: int = DEFAULT_COORDINATION_COMMAND_TIMEOUT_SECONDS
    git_operation_timeout_seconds: int = DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS
    progress_assessment_timeout_seconds: int = DEFAULT_PROGRESS_ASSESSMENT_TIMEOUT_SECONDS
    artifact_write_timeout_seconds: int = DEFAULT_ARTIFACT_WRITE_TIMEOUT_SECONDS
    stream_drain_timeout_seconds: int = DEFAULT_STREAM_DRAIN_TIMEOUT_SECONDS
    pi_stream_heartbeat_seconds: int = DEFAULT_PI_STREAM_HEARTBEAT_SECONDS
    pi_stream_idle_timeout_seconds: int = DEFAULT_PI_STREAM_IDLE_TIMEOUT_SECONDS
    saga_max_review_rounds: int = Field(
        default=4,
        validation_alias="LOCAL_AGENT_SAGA_MAX_REVIEW_ROUNDS",
    )
    artifact_backend: Literal["filesystem", "minio"] = "filesystem"
    minio_endpoint: str = "127.0.0.1:9000"
    minio_access_key: str = "localagent"
    minio_secret_key: str = "localagent-secret"
    minio_secure: bool = False
    minio_artifact_bucket: str = "local-agent-artifacts"
    spool_dir: Path = Field(
        default_factory=lambda: Path.home() / LOCAL_AGENT_STATE_DIR_NAME / "spool"
    )
    session_context_export_dir: Path = Field(
        default_factory=lambda: Path.home() / LOCAL_AGENT_STATE_DIR_NAME / "session-contexts"
    )
    session_daemon_host: str = "127.0.0.1"
    session_daemon_port: int = 8765
    pi_daemon_host: str = "127.0.0.1"
    pi_daemon_port: int = 8766
    pi_daemon_url: str | None = Field(
        default=None,
        validation_alias="LOCAL_AGENT_PI_DAEMON_URL",
    )
    pi_daemon_autostart: bool = True
    # Whether `pi` hands a query to the resident pi-daemon or runs it here in the
    # foreground. The daemon listens on 127.0.0.1, so this is a same-machine
    # handoff and has nothing to do with which agent CLI eventually answers:
    # Codex and Claude Code are reached the same way down either path.
    # Named for the working state rather than the mechanism; the old
    # LOCAL_AGENT_PI_FORCE_DIRECT spelled the same decision as an internal knob,
    # inverted, and is still honoured because every handoff doc that works around
    # the daemon's `init_sys_streams` bug tells an operator to set it.
    pi_handoff_to_daemon: bool = Field(
        default=True,
        description=(
            "Hand a pi query to the resident pi-daemon on 127.0.0.1 rather than "
            "running it in this process. Not about which agent CLI answers. Set "
            "false for the documented workaround to the daemon's Bad file "
            "descriptor bug; LOCAL_AGENT_PI_FORCE_DIRECT=1 is the legacy spelling."
        ),
        json_schema_extra={"feature_flag": True},
    )
    pi_direct_fallback: bool = Field(
        default=False,
        description=(
            "When the pi daemon is unreachable, run in-process instead of failing. "
            "Distinct from pi_handoff_to_daemon, which decides whether to try the "
            "daemon at all; this one only covers the daemon being unreachable."
        ),
        json_schema_extra={"feature_flag": True},
    )
    config_dir: Path = Field(default_factory=lambda: Path("configs"))
    llama_base_url: str = "http://127.0.0.1:8080"
    llama_models_dir: Path = Field(default_factory=lambda: Path.home() / "models")

    # --- whisper.cpp (ASR backend) -------------------------------------------
    # Background runtime kept resident by scripts/start-agent-runtime.sh on a port distinct
    # from llama-server. The registry can define the active ASR model and
    # backend capabilities; these settings remain fallback/default process
    # knobs for the whisper-server binary and idle-model swap.
    whisper_server_url: str | None = Field(
        default=None,
        validation_alias="LOCAL_AGENT_WHISPER_BASE_URL",
    )
    whisper_host: str = "127.0.0.1"
    whisper_port: int = 8090
    whisper_bin_path: Path = Field(
        default_factory=lambda: (
            Path.home() / "ai_projects" / "whisper.cpp" / "build" / "bin" / "whisper-server"
        )
    )
    whisper_models_dir: Path = Field(
        default_factory=lambda: Path.home() / "ai_projects" / "whisper.cpp" / "models"
    )
    whisper_idle_model: str = "ggml-base.en.bin"
    whisper_active_model: str = "ggml-large-v3-turbo.bin"
    whisper_threads: int = 8  # M-series: Core ML carries the encoder; ggml ops still use threads.
    whisper_flash_attn: bool = True
    asr_triggers_path: Path = Field(default_factory=lambda: Path("configs/asr_triggers.toml"))

    web_dist: Path | None = Path("web/dist")
    workflowy_dry_run: bool = Field(
        default=True,
        description=(
            "Record intended Workflowy writes without performing them. Defaulting to "
            "true means the shipped default is the non-writing product."
        ),
        json_schema_extra={"feature_flag": True},
    )
    workflowy_api_key: str | None = Field(default=None, validation_alias="WF_API_KEY")
    workflowy_fetch_script: Path | None = None
    workflowy_insert_script: Path | None = None
    apple_notes_fetch_script: Path | None = None
    chrome_devtools_transport: Literal["mcp", "cli"] = Field(
        default="mcp",
        description=(
            "Which Chrome DevTools implementation to drive. The cli path exists as a "
            "diagnostic fallback; its honest end state is deletion, not documentation."
        ),
        json_schema_extra={"feature_flag": True},
    )
    chrome_devtools_command: str = "npx"
    # Pinned Chrome DevTools MCP implementation. The live MCP path refuses to
    # resolve a floating "@latest" spec; bump the pin deliberately after a host
    # preflight proves the new version.
    chrome_devtools_command_args: list[str] = Field(
        default_factory=lambda: ["-y", "chrome-devtools-mcp@1.6.0"]
    )
    chrome_devtools_start_args: list[str] = Field(default_factory=lambda: ["--no-usage-statistics"])
    # How the MCP server reaches a browser. "auto_connect" attaches to an
    # already running eligible Chrome (144+, remote debugging enabled),
    # "browser_url" attaches to an explicit debugging endpoint, and "launch"
    # starts a dedicated instance with chrome_devtools_launch_args. Read-only
    # actions never launch a browser profile in the attach modes.
    chrome_devtools_attach_mode: Literal["auto_connect", "browser_url", "launch"] = Field(
        default="auto_connect",
        description=(
            "Whether the agent drives an already-running browser with your logged-in "
            "sessions, attaches to a named URL, or launches a throwaway one."
        ),
        json_schema_extra={"feature_flag": True},
    )
    chrome_devtools_browser_url: str | None = None
    chrome_devtools_launch_args: list[str] = Field(
        default_factory=lambda: ["--isolated", "--headless"]
    )
    # Observational actions may lazily start the supervised MCP process when
    # it is cleanly stopped; a FAILED generation always requires an explicit
    # /chrome start so failures never trigger automatic restarts.
    chrome_devtools_lazy_start: bool = Field(
        default=True,
        description=(
            "Let an observational action start a browser, rather than failing and "
            "requiring an explicit start. Decides failure semantics the operator sees."
        ),
        json_schema_extra={"feature_flag": True},
    )
    # Whether the CLI transport (the diagnostic fallback) starts Chrome itself.
    # Only consulted when chrome_devtools_transport is "cli".
    chrome_devtools_auto_start: bool = True
    # How long a CLI-transport command may run before it is abandoned.
    chrome_devtools_timeout_seconds: int = 60
    # Phase-specific MCP deadlines.
    chrome_devtools_startup_timeout_seconds: float = Field(default=20.0, gt=0)
    # A cold isolated-profile Chrome launch takes ~9s idle and much longer
    # under host load (proven 2026-07-17), so attach gets a wider budget.
    chrome_devtools_attach_timeout_seconds: float = Field(default=45.0, gt=0)
    chrome_devtools_call_timeout_seconds: float = Field(default=60.0, gt=0)
    chrome_devtools_stop_timeout_seconds: float = Field(default=5.0, gt=0)
    # Stop the supervised process after this much inactivity; 0 disables.
    chrome_devtools_idle_shutdown_seconds: float = Field(default=900.0, ge=0)
    chrome_devtools_log_tail_chars: int = Field(default=4000, gt=0)
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    @model_validator(mode="before")
    @classmethod
    def treat_blank_environment_values_as_unset(cls, data: Any) -> Any:
        """`KEY=` in a .env file means unset, not "the empty value".

        `.env.example` is generated from this model, and an optional field with
        no default renders as a bare `KEY=` line. Copying that template to `.env`
        is the first step of every install - `scripts/bootstrap.sh` and
        `scripts/boot/50-set-default-stack.sh` both do it - so every one of those
        fields arrived as `''` on a freshly installed machine, and an empty
        string is not what any of them means.

        Two of the fourteen broke outright rather than subtly. A `dict | None`
        field rejects `''` during field validation, so `Settings()` could not be
        constructed at all, and `first-run-check.sh` reported the blocked item
        as a broken target-project registry - the registry was fine, and the
        settings object it needed had never existed. A `Path | None` field
        accepts `''` and resolves it to `.`, so `LOCAL_AGENT_WORKFLOWY_FETCH_SCRIPT=`
        pointed the fetch subprocess at the current directory, which fails with
        `PermissionError: [Errno 13] Permission denied: '.'`. That one is why a
        fresh clone's `uv run pytest` failed sixteen tests immediately after
        `make`, while the same suite passed before `.env` existed.

        Restricted to optional fields, because for them "absent" is already a
        representable state and the default is the right answer. A required
        field given a blank value still fails, which is what should happen.
        """

        if not isinstance(data, dict):
            return data
        optional_keys = _optional_field_keys(cls)
        return {
            key: value
            for key, value in data.items()
            if not (isinstance(value, str) and not value.strip() and key in optional_keys)
        }

    @field_validator(
        "artifact_root",
        "lifecycle_log_dir",
        "lifecycle_maintenance_state_path",
        "coordination_root",
        "projects_root",
        "saga_worktree_root",
        "spool_dir",
        "session_context_export_dir",
        "config_dir",
        "llama_models_dir",
        "whisper_bin_path",
        "whisper_models_dir",
        "asr_triggers_path",
        mode="after",
    )
    @classmethod
    def expand_local_paths(cls, value: Path) -> Path:
        """Make checked-in ``~/...`` defaults portable across user accounts."""

        return value.expanduser()

    @model_validator(mode="after")
    def _honour_legacy_force_direct(self) -> Settings:
        """`LOCAL_AGENT_PI_FORCE_DIRECT=1` means the same as handoff disabled.

        Inverted, because the old name described what the process does instead of
        what the operator wants. Handled here rather than at the call site so the
        two spellings cannot disagree, and only when the new field was left at its
        default: an explicit choice beats a legacy one.
        """

        import os

        legacy = os.environ.get("LOCAL_AGENT_PI_FORCE_DIRECT", "").strip().lower()
        if legacy in {"1", "true", "yes", "on"} and self.pi_handoff_to_daemon:
            object.__setattr__(self, "pi_handoff_to_daemon", False)
        return self

    @property
    def model_registry_path(self) -> Path:
        return self.config_dir / "model_registry.toml"

    @property
    def workspace_policy_path(self) -> Path:
        return self.config_dir / "workspace_policies.toml"

    @property
    def directive_config_path(self) -> Path:
        return self.config_dir / "directives.toml"

    @property
    def regimen_path(self) -> Path:
        return self.config_dir / "regimen.toml"

    @property
    def pi_prompts_path(self) -> Path:
        return self.config_dir / "pi_prompts.toml"

    @property
    def linked_projects_path(self) -> Path:
        return self.config_dir / "linked_projects.toml"

    @property
    def ontology_path(self) -> Path:
        return self.config_dir / "ontology.toml"

    @property
    def whisper_base_url(self) -> str:
        if self.whisper_server_url:
            return self.whisper_server_url
        return f"http://{self.whisper_host}:{self.whisper_port}"

    @property
    def pi_daemon_base_url(self) -> str:
        if self.pi_daemon_url:
            return self.pi_daemon_url
        return f"http://{self.pi_daemon_host}:{self.pi_daemon_port}"

    @property
    def database_identity(self) -> DatabaseIdentity:
        """The database this process is pointed at, safe to report anywhere.

        A caller that wants to say which database is in use asks for this rather
        than reading `database_url` and remembering to strip it, so there is one
        place where that decision lives instead of one per reporting surface.
        """

        return DatabaseIdentity.from_url(self.database_url)

    @property
    def ledger_outbox_destination(self) -> tuple[str, str] | None:
        if isinstance(self.ledger_outbox, DisabledLedgerOutbox):
            return None
        return (self.ledger_outbox.consumer, self.ledger_outbox.topic)

    def load_toml(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        return tomllib.loads(path.read_text(encoding="utf-8")) or {}

    @model_validator(mode="after")
    def prefer_process_dbos_conductor_metadata(self) -> Settings:
        # Stripped before the emptiness test, for the same reason
        # `treat_blank_conductor_metadata_as_unset` strips: a blank assignment
        # means unset, and `json.loads` raises on whitespace rather than
        # returning nothing.
        raw = (getenv("DBOS_CONDUCTOR_EXECUTOR_METADATA") or "").strip()
        if not raw:
            return self
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("DBOS_CONDUCTOR_EXECUTOR_METADATA must be a JSON object.")
        self.dbos_conductor_executor_metadata = parsed
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
