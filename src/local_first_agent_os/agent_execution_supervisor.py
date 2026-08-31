# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Streaming, durable supervision for one Claude or Codex CLI process."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from .constants import (
    DEFAULT_ARTIFACT_WRITE_TIMEOUT_SECONDS,
    DEFAULT_COORDINATION_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS,
    DEFAULT_PROGRESS_ASSESSMENT_TIMEOUT_SECONDS,
    DEFAULT_STREAM_DRAIN_TIMEOUT_SECONDS,
)
from .coordination.contracts import (
    AppendExecutionEvent,
    AttachExecutionArtifact,
    CoordinationCommand,
    CoordinationResult,
    CreateExecutionCheckpoint,
    EntityResult,
    HeartbeatExecutionLease,
    RequestExecutionCancel,
)
from .coordination.outcomes import (
    AgentStatus,
    InfrastructureFailure,
    PersistenceStatus,
    SupervisorStatus,
    TerminalOutcome,
    classify_failure,
    classify_persistence_failure,
    failure_category,
)
from .lifecycle_failure_harness import (
    LifecycleTransitionPoint,
    reach_lifecycle_transition,
)
from .staffing import FrontierHarness
from .toolchains import project_environment

if TYPE_CHECKING:
    from .pow_wow.types import CommandRunCapture, ExecutionAttemptLease

type EventSource = Literal["stdout", "stderr", "lifecycle"]
type CheckpointReason = Literal[
    "deadline", "operator_cancel", "supervisor_error", "stalled_progress"
]

_PROVIDER_UNAVAILABLE_OUTCOMES: Final = frozenset(
    {
        TerminalOutcome.USAGE_LIMIT,
        TerminalOutcome.AUTHENTICATION_FAILED,
        TerminalOutcome.TRANSPORT_INTERRUPTED,
    }
)
"""Failures where the agent stopped because its provider would not serve it.

Each one ends a run that was doing real work a moment earlier, which is what
makes the worktree worth keeping: the next attempt starts from nothing, and
until now nothing recorded how far the last one got. An earlier session had to
reconstruct a lost milestone's diff out of the supervised event stream for
exactly this reason.

Kept to the three that say "the provider refused", not every
`InfrastructureFailure`. A worktree per failed run is disk, and outcomes like
`UNKNOWN_FAILURE` cannot be told apart from a run that would fail the same way
again, so preserving those would accumulate trees nobody resumes.
"""
type ProgressAssessor = Callable[[Mapping[str, object]], Mapping[str, object]]

_MAX_EVENT_LINE_BYTES = 256 * 1024
_MAX_EVENT_PAYLOAD_BYTES = 64 * 1024
# asyncio's subprocess default is 64 KiB. Frontier JSONL events can contain a
# single command result larger than that, so the default would terminate the
# reader before later agent messages and the final verdict are observed. Keep
# the transport limit above our own bounded event-line limit; normalization
# still truncates persisted payloads to the limits above.
_STREAM_READER_LIMIT_BYTES = _MAX_EVENT_LINE_BYTES * 4
_SENSITIVE_KEY = re.compile(
    r"(^|_)(thinking|reasoning|chain_of_thought|secret|password|token|credential|api_key)($|_)",
    re.IGNORECASE,
)
_SENSITIVE_TEXT = re.compile(r"(?i)(api[_-]?key|password|secret|bearer)\s*[:=]\s*[^\s,;]+")


class ArtifactWriter(Protocol):
    def write_text(
        self,
        *,
        role: str,
        text: str,
        workflow_id: str | None,
        schema_version: str,
        mime_type: str = "text/plain",
    ) -> Any: ...

    def read_text(self, artifact_id: str) -> str: ...


@dataclass(frozen=True)
class AgentStreamEvent:
    lease_id: str
    sequence: int
    occurred_at: float
    source: EventSource
    kind: str
    payload: dict[str, object]
    payload_sha256: str


@dataclass(frozen=True)
class SupervisedCommandResult:
    capture: CommandRunCapture
    deadline_reached: bool
    cancel_requested: bool
    transcript_artifact_id: str | None
    checkpoint_id: str | None
    checkpoint_artifact_ids: tuple[str, ...]
    checkpoint_reason: CheckpointReason | None
    preserve_worktree: bool
    event_count: int
    supervisor_error: str | None = None
    agent_status: AgentStatus = AgentStatus.PENDING
    agent_failure: str | None = None
    agent_failure_category: str | None = None
    supervisor_status: SupervisorStatus = SupervisorStatus.PENDING
    supervisor_failure: str | None = None
    persistence_status: PersistenceStatus = PersistenceStatus.PENDING
    persistence_failure: str | None = None
    activity_status: str = "STARTING"
    progress_recommendation: str | None = None


def _redact_text(value: str, *, limit: int = 16_000) -> str:
    clean = _SENSITIVE_TEXT.sub(r"\1=[REDACTED]", value)
    return clean if len(clean) <= limit else f"{clean[:limit]}…[truncated]"


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[depth-limited]"
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if _SENSITIVE_KEY.search(key):
                safe[key] = "[REDACTED]"
            elif key == "content" and isinstance(item, list):
                safe[key] = [
                    _safe_value(block, depth=depth + 1)
                    for block in item
                    if not (
                        isinstance(block, Mapping)
                        and str(block.get("type") or "").lower()
                        in {"thinking", "reasoning", "analysis"}
                    )
                ]
            else:
                safe[key] = _safe_value(item, depth=depth + 1)
        return safe
    if isinstance(value, list):
        return [_safe_value(item, depth=depth + 1) for item in value[:1000]]
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _redact_text(str(value))


def _event_kind(harness: str, payload: Mapping[str, Any]) -> str:
    """Name one streamed event, refining it where the harness makes it possible.

    The harness arrives as a string because it is read back off stored lease
    rows as well as passed in live, so an unrecognised value is a stale row and
    not a programmer error. It names the two frontier harnesses through the enum
    rather than as literals, so renaming one is a find-references away rather
    than a grep.

    Unrecognised falls through to the raw kind, which is the honest answer:
    unlike the command builder's old fall-through, no wrong thing is executed by
    declining to refine a label.
    """

    raw = payload.get("type") or payload.get("event") or payload.get("kind")
    kind = str(raw or "unknown")
    if harness == FrontierHarness.CODEX.value and kind.startswith("item."):
        item = payload.get("item")
        if isinstance(item, Mapping) and item.get("type"):
            return f"{kind}:{item['type']}"
    if harness == FrontierHarness.CLAUDE.value and kind == "assistant":
        return "assistant.message"
    return kind


def normalize_jsonl_line(
    *,
    harness: str,
    source: EventSource,
    line: bytes,
) -> tuple[str, dict[str, object]]:
    """Normalize one harness line without retaining private reasoning."""

    raw_hash = hashlib.sha256(line).hexdigest()
    if len(line) > _MAX_EVENT_LINE_BYTES:
        return "oversized", {
            "raw_sha256": raw_hash,
            "size_bytes": len(line),
            "omitted": True,
        }
    text = line.decode("utf-8", errors="replace").rstrip("\r\n")
    if source == "stderr":
        return "stderr", {"text": _redact_text(text), "raw_sha256": raw_hash}
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        return "unknown", {
            "raw_sha256": raw_hash,
            "text": _redact_text(text, limit=2000),
            "malformed_json": True,
        }
    if not isinstance(decoded, Mapping):
        return "unknown", {
            "raw_sha256": raw_hash,
            "value": _safe_value(decoded),
        }
    payload = _safe_value(decoded)
    assert isinstance(payload, dict)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(canonical.encode("utf-8")) > _MAX_EVENT_PAYLOAD_BYTES:
        return "oversized", {
            "raw_sha256": raw_hash,
            "normalized_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            "size_bytes": len(canonical.encode("utf-8")),
            "omitted": True,
        }
    return _event_kind(harness, decoded), payload


def _payload_hash(payload: Mapping[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def has_meaningful_agent_progress(source: EventSource, kind: str) -> bool:
    """Classify visible agent output; liveness noise is deliberately excluded."""

    if source != "stdout":
        return False
    normalized = kind.casefold()
    return not any(
        marker in normalized
        for marker in (
            "heartbeat",
            "warning",
            "keepalive",
            "rate_limit",
            "oversized",
            "unknown",
        )
    )


def _git_capture(
    worktree: Path,
    args: Sequence[str],
    timeout_seconds: float = DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS,
) -> str:
    completed = subprocess.run(
        ["git", "-C", str(worktree), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout


async def _execute_bounded_blocking_call(
    func: Callable[..., Any],
    *args: Any,
    timeout_seconds: float,
    **kwargs: Any,
) -> Any:
    """Run non-preemptible library code without letting it own the event loop.

    A raw daemon thread is intentional. ``asyncio.to_thread`` uses the loop's
    default executor, and ``asyncio.run`` waits for canceled executor jobs at
    shutdown. A wedged filesystem or in-process test transport would therefore
    defeat an outer ``wait_for``. The operation should still carry its own
    lower-level timeout when possible; this is the supervisor's final boundary.
    """

    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()

    def settle(result: Any, error: BaseException | None) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)

    def invoke() -> None:
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:  # propagate into the supervising task
            if not loop.is_closed():
                loop.call_soon_threadsafe(settle, None, exc)
        else:
            if not loop.is_closed():
                loop.call_soon_threadsafe(settle, result, None)

    threading.Thread(target=invoke, daemon=True, name="agent-supervisor-blocking-call").start()
    return await asyncio.wait_for(future, timeout=timeout_seconds)


class StreamingCommandSupervisor:
    """Own one process group, its stream, heartbeats, and recovery checkpoint."""

    def __init__(
        self,
        *,
        coordination_command: Callable[[CoordinationCommand], CoordinationResult],
        artifact_writer: ArtifactWriter,
        heartbeat_seconds: float = 30.0,
        warning_seconds: float = 300.0,
        termination_grace_seconds: float = 30.0,
        quiet_seconds: float = 300.0,
        stalled_seconds: float = 600.0,
        progress_assessor: ProgressAssessor | None = None,
        coordination_timeout_seconds: float = DEFAULT_COORDINATION_COMMAND_TIMEOUT_SECONDS,
        git_timeout_seconds: float = DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS,
        progress_assessment_timeout_seconds: float = DEFAULT_PROGRESS_ASSESSMENT_TIMEOUT_SECONDS,
        artifact_write_timeout_seconds: float = DEFAULT_ARTIFACT_WRITE_TIMEOUT_SECONDS,
        stream_drain_timeout_seconds: float = DEFAULT_STREAM_DRAIN_TIMEOUT_SECONDS,
    ) -> None:
        self.coordination_command = coordination_command
        self.artifact_writer = artifact_writer
        self.heartbeat_seconds = heartbeat_seconds
        self.warning_seconds = warning_seconds
        self.termination_grace_seconds = termination_grace_seconds
        if quiet_seconds <= 0 or stalled_seconds <= quiet_seconds:
            raise ValueError("stalled_seconds must be greater than quiet_seconds > 0")
        self.quiet_seconds = quiet_seconds
        self.stalled_seconds = stalled_seconds
        self.progress_assessor = progress_assessor
        self.coordination_timeout_seconds = coordination_timeout_seconds
        self.git_timeout_seconds = git_timeout_seconds
        self.progress_assessment_timeout_seconds = progress_assessment_timeout_seconds
        self.artifact_write_timeout_seconds = artifact_write_timeout_seconds
        self.stream_drain_timeout_seconds = stream_drain_timeout_seconds

    async def _coord(self, command: CoordinationCommand) -> CoordinationResult:
        return await _execute_bounded_blocking_call(
            self.coordination_command,
            command,
            timeout_seconds=self.coordination_timeout_seconds,
        )

    async def run(
        self,
        command: Sequence[str],
        cwd: Path,
        *,
        lease: ExecutionAttemptLease,
        harness: str,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        complete_environment: bool = False,
        source_repo_path: Path | None = None,
        base_head_sha: str | None = None,
        saga_id: str | None = None,
        pow_wow_id: str | None = None,
        task_contract: str = "",
    ) -> SupervisedCommandResult:
        if not lease.lease_id:
            raise ValueError("streaming supervision requires an opened execution lease")
        lease_id = lease.lease_id
        started = time.monotonic()
        sequence = 0
        sequence_lock = asyncio.Lock()
        transcript: list[str] = []
        safe_stdout: list[str] = []
        safe_stderr: list[str] = []
        event_tail: list[str] = []
        fatal_error: str | None = None
        cancel_event = asyncio.Event()
        stop_heartbeat = asyncio.Event()
        stop_activity = asyncio.Event()
        last_progress_monotonic = started
        last_progress_sequence = 0
        activity_status = "STARTING"
        assessment_started = False
        progress_recommendation: str | None = None
        requested_checkpoint_reason: CheckpointReason | None = None
        recent_progress_signatures: list[str] = []

        async def persist(source: EventSource, kind: str, payload: dict[str, object]) -> int:
            nonlocal sequence, fatal_error
            async with sequence_lock:
                sequence += 1
                event = AgentStreamEvent(
                    lease_id=lease_id,
                    sequence=sequence,
                    occurred_at=time.time(),
                    source=source,
                    kind=kind,
                    payload=payload,
                    payload_sha256=_payload_hash(payload),
                )
                transcript_line = json.dumps(
                    {
                        "sequence": event.sequence,
                        "occurred_at": event.occurred_at,
                        "source": source,
                        "kind": kind,
                        "payload": payload,
                        "payload_sha256": event.payload_sha256,
                    },
                    sort_keys=True,
                )
                transcript.append(transcript_line)
                event_tail.append(f"{event.sequence}:{source}:{kind}")
                del event_tail[:-30]
                try:
                    await self._coord(
                        AppendExecutionEvent(
                            lease_id=lease_id,
                            sequence=event.sequence,
                            occurred_at=event.occurred_at,
                            source=source,
                            kind=kind,
                            payload=payload,
                            payload_sha256=event.payload_sha256,
                        )
                    )
                except Exception as exc:  # fail closed; preserve the worktree
                    fatal_error = f"event persistence failed: {type(exc).__name__}: {exc}"
                    cancel_event.set()
                return event.sequence

        async def mark_progress(observed_sequence: int, observed_kind: str) -> None:
            nonlocal last_progress_monotonic, last_progress_sequence, activity_status
            last_progress_monotonic = time.monotonic()
            activity_status = "PROGRESSING"
            progress_sequence = await persist(
                "lifecycle",
                "activity.progress",
                {
                    "observed_sequence": observed_sequence,
                    "observed_kind": observed_kind,
                },
            )
            last_progress_sequence = progress_sequence

        await persist(
            "lifecycle",
            "process.starting",
            {
                "harness": harness,
                "cwd": str(cwd),
                "command": [str(part) for part in command[:-1]],
                "timeout_seconds": timeout_seconds,
            },
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=(dict(env or {}) if complete_environment else project_environment(cwd, env)),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=_STREAM_READER_LIMIT_BYTES,
        )
        started_sequence = await persist("lifecycle", "process.started", {"pid": process.pid})
        await mark_progress(started_sequence, "process.started")

        async def read_stream(stream: asyncio.StreamReader, source: EventSource) -> None:
            nonlocal fatal_error
            while True:
                line = await stream.readline()
                if not line:
                    return
                kind, payload = normalize_jsonl_line(
                    harness=harness,
                    source=source,
                    line=line,
                )
                safe_line = json.dumps(payload, sort_keys=True)
                (safe_stdout if source == "stdout" else safe_stderr).append(safe_line)
                observed_sequence = await persist(source, kind, payload)
                try:
                    reach_lifecycle_transition(
                        LifecycleTransitionPoint.DURING_AGENT_STREAM,
                        lease_id=lease_id,
                        pid=process.pid,
                        source=source,
                        kind=kind,
                        sequence=observed_sequence,
                    )
                except Exception as exc:
                    fatal_error = f"injected lifecycle stream failure: {type(exc).__name__}: {exc}"
                    cancel_event.set()
                    return
                if has_meaningful_agent_progress(source, kind):
                    signature = f"{kind}:{_payload_hash(payload)}"
                    if signature not in recent_progress_signatures:
                        recent_progress_signatures.append(signature)
                        del recent_progress_signatures[:-128]
                        await mark_progress(observed_sequence, kind)

        async def heartbeat() -> None:
            while not stop_heartbeat.is_set():
                try:
                    result = await self._coord(
                        HeartbeatExecutionLease(lease_id=lease_id, worker_id=lease.worker_id)
                    )
                    cancel_requested = False
                    if isinstance(result, EntityResult):
                        cancel_requested = (
                            bool(result.metadata.values.get("cancel_requested"))
                            or result.entity.values.get("status") == "CANCEL_REQUESTED"
                        )
                    await persist(
                        "lifecycle",
                        "lease.heartbeat",
                        {"cancel_requested": cancel_requested},
                    )
                    if cancel_requested:
                        cancel_event.set()
                        return
                except Exception as exc:
                    nonlocal fatal_error
                    fatal_error = f"lease heartbeat failed: {type(exc).__name__}: {exc}"
                    cancel_event.set()
                    return
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_heartbeat.wait(), timeout=self.heartbeat_seconds)

        async def activity_monitor() -> None:
            nonlocal activity_status, assessment_started, progress_recommendation
            nonlocal requested_checkpoint_reason
            poll_seconds = max(0.01, min(self.heartbeat_seconds, self.quiet_seconds / 4))
            while not stop_activity.is_set():
                silent_seconds = time.monotonic() - last_progress_monotonic
                if silent_seconds >= self.stalled_seconds and not assessment_started:
                    activity_status = "STALLED_SUSPECTED"
                    await persist(
                        "lifecycle",
                        "activity.stalled_suspected",
                        {
                            "silent_seconds": round(silent_seconds, 3),
                            "last_meaningful_progress_sequence": last_progress_sequence,
                            "heartbeat_is_progress": False,
                        },
                    )
                    assessment_started = True
                    if self.progress_assessor is not None:
                        evidence: dict[str, object] = {
                            "schema_version": "execution_progress_evidence.v1",
                            "lease_id": lease_id,
                            "harness": harness,
                            "pid": process.pid,
                            "elapsed_seconds": round(time.monotonic() - started, 3),
                            "silent_seconds": round(silent_seconds, 3),
                            "last_meaningful_progress_sequence": last_progress_sequence,
                            "recent_events": list(event_tail),
                            "task_contract": task_contract[:12_000],
                        }
                        with contextlib.suppress(Exception):
                            evidence["git_status"] = await asyncio.to_thread(
                                _git_capture,
                                cwd,
                                ["status", "--short"],
                                self.git_timeout_seconds,
                            )
                            evidence["git_diff_stat"] = await asyncio.to_thread(
                                _git_capture,
                                cwd,
                                ["diff", "--stat", "HEAD"],
                                self.git_timeout_seconds,
                            )
                        await persist("lifecycle", "progress_assessment.started", evidence)
                        try:
                            decision = dict(
                                await _execute_bounded_blocking_call(
                                    self.progress_assessor,
                                    evidence,
                                    timeout_seconds=self.progress_assessment_timeout_seconds,
                                )
                            )
                            recommendation = str(decision.get("recommendation") or "").upper()
                            if recommendation not in {
                                "CONTINUE",
                                "CHECKPOINT",
                                "SPLIT",
                                "PAUSE_OPERATOR",
                            }:
                                raise ValueError(
                                    "junior recommendation must be CONTINUE, CHECKPOINT, "
                                    "SPLIT, or PAUSE_OPERATOR"
                                )
                            progress_recommendation = recommendation
                            raw_continuations = decision.get("continuations")
                            continuations = (
                                list(raw_continuations)
                                if isinstance(raw_continuations, list)
                                else []
                            )
                            decision_payload: dict[str, object] = {
                                "schema_version": "execution_progress_assessment.v1",
                                "recommendation": recommendation,
                                "rationale": str(decision.get("rationale") or ""),
                                "continuations": continuations,
                            }
                            await persist(
                                "lifecycle", "progress_assessment.completed", decision_payload
                            )
                            if recommendation != "CONTINUE":
                                requested_checkpoint_reason = "stalled_progress"
                                cancel_event.set()
                                return
                        except Exception as exc:  # advisory failure never owns the process
                            await persist(
                                "lifecycle",
                                "progress_assessment.failed",
                                {"error": _redact_text(f"{type(exc).__name__}: {exc}")},
                            )
                elif silent_seconds >= self.quiet_seconds and activity_status not in {
                    "QUIET",
                    "STALLED_SUSPECTED",
                }:
                    activity_status = "QUIET"
                    await persist(
                        "lifecycle",
                        "activity.quiet",
                        {
                            "silent_seconds": round(silent_seconds, 3),
                            "last_meaningful_progress_sequence": last_progress_sequence,
                            "heartbeat_is_progress": False,
                        },
                    )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_activity.wait(), timeout=poll_seconds)

        if process.stdout is None or process.stderr is None:
            raise RuntimeError("supervised process pipes were not created")
        stdout_task = asyncio.create_task(read_stream(process.stdout, "stdout"))
        stderr_task = asyncio.create_task(read_stream(process.stderr, "stderr"))
        heartbeat_task = asyncio.create_task(heartbeat())
        activity_task = asyncio.create_task(activity_monitor())
        process_task = asyncio.create_task(process.wait())

        async def observe_process_exit() -> None:
            # asyncio's subprocess wait future can remain pending after the
            # supervised PID exits when an escaped descendant inherits one of
            # the captured pipes. The transport still records returncode as
            # soon as the direct child exits, so observe that independently
            # and let the bounded stream-drain path close orphaned pipes.
            while process.returncode is None:
                await asyncio.sleep(0.05)

        process_exit_task = asyncio.create_task(observe_process_exit())
        deadline_task = asyncio.create_task(asyncio.sleep(timeout_seconds))
        cancel_task = asyncio.create_task(cancel_event.wait())
        warning_task: asyncio.Task[None] | None = None
        warning_delay = timeout_seconds - self.warning_seconds
        if warning_delay > 0:

            async def warn() -> None:
                await asyncio.sleep(warning_delay)
                await persist(
                    "lifecycle",
                    "deadline.warning",
                    {"remaining_seconds": self.warning_seconds},
                )

            warning_task = asyncio.create_task(warn())

        done, _ = await asyncio.wait(
            {process_task, process_exit_task, deadline_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        deadline_reached = deadline_task in done
        cancel_requested = cancel_task in done and not deadline_reached
        checkpoint_reason: CheckpointReason | None = None
        if requested_checkpoint_reason is not None:
            checkpoint_reason = requested_checkpoint_reason
        elif fatal_error:
            checkpoint_reason = "supervisor_error"
        elif deadline_reached:
            checkpoint_reason = "deadline"
        elif cancel_requested:
            checkpoint_reason = "operator_cancel"

        if checkpoint_reason is not None and process.returncode is None:
            await persist(
                "lifecycle",
                f"{checkpoint_reason}.reached",
                {"elapsed_seconds": round(time.monotonic() - started, 3)},
            )
            if checkpoint_reason == "deadline":
                with contextlib.suppress(Exception):
                    await self._coord(
                        RequestExecutionCancel(
                            lease_id=lease_id,
                            reason="frontier execution deadline reached",
                            requested_by="streaming-supervisor",
                        )
                    )
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(
                    asyncio.shield(process_task),
                    timeout=self.termination_grace_seconds,
                )
            except TimeoutError:
                await persist(
                    "lifecycle",
                    "process.sigkill",
                    {"grace_seconds": self.termination_grace_seconds},
                )
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                try:
                    await asyncio.wait_for(
                        asyncio.shield(process_task),
                        timeout=self.termination_grace_seconds,
                    )
                except TimeoutError:
                    # asyncio's subprocess wait future does not resolve until
                    # every captured pipe reaches EOF. A descendant that
                    # escapes the process group can inherit stdout/stderr and
                    # keep that future pending even after the supervised PID
                    # has been SIGKILLed. Bound this second wait and close the
                    # subprocess transport so checkpointing and lease
                    # terminalization can continue.
                    await persist(
                        "lifecycle",
                        "process.wait_abandoned",
                        {
                            "after_signal": "SIGKILL",
                            "grace_seconds": self.termination_grace_seconds,
                            "returncode_observed": process.returncode,
                        },
                    )
                    transport = getattr(process, "_transport", None)
                    if transport is not None:
                        transport.close()
                    if not process_task.done():
                        process_task.cancel()
                    await asyncio.gather(process_task, return_exceptions=True)

        stop_heartbeat.set()
        stop_activity.set()
        for task in (deadline_task, cancel_task, warning_task, process_exit_task):
            if task is not None and not task.done():
                task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(stdout_task, stderr_task, return_exceptions=True),
                timeout=self.stream_drain_timeout_seconds,
            )
        except TimeoutError:
            transport = getattr(process, "_transport", None)
            if transport is not None:
                transport.close()
            for stream_task in (stdout_task, stderr_task):
                if not stream_task.done():
                    stream_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            await persist(
                "lifecycle",
                "stream.drain_abandoned",
                {"timeout_seconds": self.stream_drain_timeout_seconds},
            )
        if not process_task.done():
            process_task.cancel()
        await asyncio.gather(process_task, return_exceptions=True)
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        if not activity_task.done():
            activity_task.cancel()
        await asyncio.gather(activity_task, return_exceptions=True)
        returncode = process.returncode if process.returncode is not None else 1
        await persist(
            "lifecycle",
            "process.exited",
            {
                "returncode": returncode,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            },
        )

        combined_output = "\n".join((*safe_stdout, *safe_stderr))
        agent_outcome = classify_failure(combined_output) if returncode else None
        agent_status = AgentStatus.COMPLETED if returncode == 0 else AgentStatus.FAILED
        agent_failure = agent_outcome.value if agent_outcome is not None else None
        category = failure_category(agent_outcome)
        await persist(
            "lifecycle",
            "agent.finished",
            {
                "status": agent_status.value,
                "failure": agent_failure,
                "failure_category": category.value if category else None,
                "returncode": returncode,
            },
        )

        # A frontier execution lease is not an application workflow. Artifacts
        # therefore retain a NULL workflow_id and are attached to the lease by
        # the coordination ledger's append-only execution-artifact relation.
        artifact_workflow_id: str | None = None
        persistence_errors: list[str] = []
        persistence_failure: str | None = None

        async def write_and_attach_artifact(
            *,
            role: str,
            text: str,
            schema_version: str,
            mime_type: str = "text/plain",
        ) -> str | None:
            nonlocal persistence_failure
            try:
                ref = await _execute_bounded_blocking_call(
                    self.artifact_writer.write_text,
                    role=role,
                    text=text,
                    workflow_id=artifact_workflow_id,
                    schema_version=schema_version,
                    mime_type=mime_type,
                    timeout_seconds=self.artifact_write_timeout_seconds,
                )
                artifact_id = str(ref.artifact_id)
                await self._coord(
                    AttachExecutionArtifact(
                        lease_id=lease_id,
                        artifact_id=artifact_id,
                        role=role,
                        schema_version=schema_version,
                    )
                )
                return artifact_id
            except Exception as exc:  # preserve the primary agent outcome
                classified = classify_persistence_failure(exc)
                persistence_failure = classified.value
                detail = f"{role}: {type(exc).__name__}: {exc}"
                persistence_errors.append(detail)
                await persist(
                    "lifecycle",
                    "artifact.persist.failed",
                    {"role": role, "failure": classified.value, "error": _redact_text(detail)},
                )
                return None

        transcript_artifact_id = await write_and_attach_artifact(
            role="agent_execution_transcript",
            text="\n".join(transcript) + ("\n" if transcript else ""),
            schema_version="agent_execution_transcript.v1",
            mime_type="application/x-ndjson",
        )

        checkpoint_id: str | None = None
        checkpoint_artifact_ids: list[str] = []
        # A provider that refused mid-run leaves the same thing behind as a run we
        # stopped ourselves: a worktree whose state is the only record of how far
        # the work got. It gets the snapshot for that reason, and it does not get
        # a checkpoint row, because `create_execution_checkpoint` moves the intent
        # out of CLAIMED and enqueues a junior review of it. That is right for a
        # run being parked for later and wrong here: this intent is about to
        # settle FAILED, and parking it would race the settlement while asking a
        # reviewer to look at work nobody resumed.
        #
        # Splitting the two is the whole change. The snapshot is evidence, the
        # checkpoint is a lifecycle transition, and only the first is wanted when
        # the provider is the thing that died.
        provider_unavailable = checkpoint_reason is None and agent_outcome in (
            _PROVIDER_UNAVAILABLE_OUTCOMES
        )
        capture_snapshot = checkpoint_reason is not None or provider_unavailable
        preserve_worktree = capture_snapshot
        checkpoint_error = fatal_error
        status_artifact_id: str | None = None
        patch_artifact_id: str | None = None
        test_summary_artifact_id: str | None = None
        if capture_snapshot:
            try:
                head = await asyncio.to_thread(
                    _git_capture, cwd, ["rev-parse", "HEAD"], self.git_timeout_seconds
                )
                status_text = await asyncio.to_thread(
                    _git_capture, cwd, ["status", "--short"], self.git_timeout_seconds
                )
                patch_text = await asyncio.to_thread(
                    _git_capture,
                    cwd,
                    ["diff", "--binary", "HEAD"],
                    self.git_timeout_seconds,
                )
                stat_text = await asyncio.to_thread(
                    _git_capture,
                    cwd,
                    ["diff", "--stat", "HEAD"],
                    self.git_timeout_seconds,
                )
                base_head_sha = base_head_sha or head.strip()
                status_artifact_id = await write_and_attach_artifact(
                    role="agent_checkpoint_git_status",
                    text=status_text,
                    schema_version="agent_checkpoint_git_status.v1",
                )
                patch_artifact_id = await write_and_attach_artifact(
                    role="agent_checkpoint_patch",
                    text=patch_text,
                    schema_version="agent_checkpoint_patch.v1",
                    mime_type="text/x-diff",
                )
                test_summary_artifact_id = await write_and_attach_artifact(
                    role="agent_checkpoint_test_summary",
                    text=stat_text,
                    schema_version="agent_checkpoint_test_summary.v1",
                )
                checkpoint_artifact_ids.extend(
                    artifact_id
                    for artifact_id in (
                        transcript_artifact_id,
                        patch_artifact_id,
                        status_artifact_id,
                        test_summary_artifact_id,
                    )
                    if artifact_id
                )
            except Exception as exc:
                checkpoint_error = (
                    f"{checkpoint_error}; " if checkpoint_error else ""
                ) + f"snapshot failed: {type(exc).__name__}: {exc}"

        if checkpoint_reason is not None:
            checkpoint_status = (
                "FAILED"
                if checkpoint_error
                else (
                    "PAUSED"
                    if checkpoint_reason in {"operator_cancel", "stalled_progress"}
                    else "PENDING_JUNIOR"
                )
            )
            reach_lifecycle_transition(
                LifecycleTransitionPoint.BEFORE_CHECKPOINT_PERSISTED,
                lease_id=lease_id,
                reason=checkpoint_reason,
                status=checkpoint_status,
                worktree_path=str(cwd),
                base_head_sha=base_head_sha,
            )
            result = await self._coord(
                CreateExecutionCheckpoint(
                    lease_id=lease_id,
                    reason=checkpoint_reason,
                    status=checkpoint_status,
                    saga_id=saga_id,
                    pow_wow_id=pow_wow_id,
                    worktree_path=str(cwd),
                    source_repo_path=str(source_repo_path) if source_repo_path else None,
                    base_head_sha=base_head_sha,
                    transcript_artifact_id=transcript_artifact_id,
                    patch_artifact_id=patch_artifact_id,
                    git_status_artifact_id=status_artifact_id,
                    test_summary_artifact_id=test_summary_artifact_id,
                    task_contract=task_contract[:50_000],
                    event_summary="\n".join(event_tail),
                    submit_review=checkpoint_status == "PENDING_JUNIOR",
                    error=checkpoint_error,
                )
            )
            if isinstance(result, EntityResult):
                checkpoint_id = str(result.entity.values.get("checkpoint_id") or "") or None
            await persist(
                "lifecycle",
                "checkpoint.created",
                {
                    "checkpoint_id": checkpoint_id,
                    "reason": checkpoint_reason,
                    "status": checkpoint_status,
                    "artifact_ids": checkpoint_artifact_ids,
                },
            )

        if provider_unavailable:
            # Named in the stream so the worktree is findable later. Without it
            # the only trace is the absence of a cleanup event, and "the thing
            # that did not happen" is not something an operator can search for.
            await persist(
                "lifecycle",
                "provider_unavailable.worktree_preserved",
                {
                    "failure": agent_failure,
                    "worktree_path": str(cwd),
                    "base_head_sha": base_head_sha,
                    "patch_artifact_id": patch_artifact_id,
                    "git_status_artifact_id": status_artifact_id,
                },
            )

        if checkpoint_reason == "deadline":
            exit_code = 124
            stderr = "process timed out; checkpoint preserved"
        elif checkpoint_reason is not None:
            exit_code = 130
            stderr = f"process canceled; checkpoint preserved ({checkpoint_reason})"
        else:
            exit_code = returncode
            stderr = "\n".join(safe_stderr)
        from .pow_wow.types import CommandRunCapture

        capture = CommandRunCapture(
            command=shlex.join(str(part) for part in command),
            cwd=str(cwd),
            stdout="\n".join(safe_stdout),
            stderr=stderr,
            exit_code=exit_code,
        )
        if checkpoint_reason == "deadline":
            agent_failure = InfrastructureFailure.DEADLINE_EXCEEDED.value
            category = failure_category(agent_failure)
        if fatal_error and fatal_error.startswith("event persistence failed"):
            persistence_failure = InfrastructureFailure.EVENT_WRITE_FAILED.value
            persistence_errors.append(fatal_error)
        supervisor_failure = fatal_error if checkpoint_reason == "supervisor_error" else None
        return SupervisedCommandResult(
            capture=capture,
            deadline_reached=deadline_reached,
            cancel_requested=cancel_requested,
            transcript_artifact_id=transcript_artifact_id,
            checkpoint_id=checkpoint_id,
            checkpoint_artifact_ids=tuple(checkpoint_artifact_ids),
            checkpoint_reason=checkpoint_reason,
            preserve_worktree=preserve_worktree,
            event_count=sequence,
            supervisor_error=checkpoint_error,
            agent_status=agent_status,
            agent_failure=agent_failure,
            agent_failure_category=category.value if category else None,
            supervisor_status=(
                SupervisorStatus.FAILED if supervisor_failure else SupervisorStatus.COMPLETED
            ),
            supervisor_failure=supervisor_failure,
            persistence_status=(
                PersistenceStatus.FAILED if persistence_errors else PersistenceStatus.COMPLETED
            ),
            persistence_failure=persistence_failure,
            activity_status="TERMINAL",
            progress_recommendation=progress_recommendation,
        )


__all__ = [
    "AgentStreamEvent",
    "StreamingCommandSupervisor",
    "SupervisedCommandResult",
    "has_meaningful_agent_progress",
    "normalize_jsonl_line",
]
