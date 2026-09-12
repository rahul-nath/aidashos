# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Durably supervise one frontier process, its lease, and recovery evidence."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, TypeVar

from .codex_review_failure import inspection_process_failure
from .constants import (
    AGENT_ACTIVITY_MINIMUM_POLL_SECONDS,
    AGENT_CHECKPOINT_TASK_CONTRACT_LIMIT,
    AGENT_EVENT_STREAM_READER_LIMIT_BYTES,
    AGENT_EVENT_SUMMARY_TAIL_SIZE,
    AGENT_PROCESS_EXIT_POLL_SECONDS,
    AGENT_PROGRESS_EVIDENCE_TASK_CONTRACT_LIMIT,
    AGENT_PROGRESS_SIGNATURE_WINDOW_SIZE,
    DEFAULT_AGENT_SUPERVISOR_HEARTBEAT_SECONDS,
    DEFAULT_AGENT_SUPERVISOR_QUIET_SECONDS,
    DEFAULT_AGENT_SUPERVISOR_STALLED_SECONDS,
    DEFAULT_AGENT_SUPERVISOR_TERMINATION_GRACE_SECONDS,
    DEFAULT_AGENT_SUPERVISOR_WARNING_SECONDS,
    DEFAULT_ARTIFACT_WRITE_TIMEOUT_SECONDS,
    DEFAULT_COORDINATION_COMMAND_TIMEOUT_SECONDS,
    DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS,
    DEFAULT_PROGRESS_ASSESSMENT_TIMEOUT_SECONDS,
    DEFAULT_STREAM_DRAIN_TIMEOUT_SECONDS,
    PROCESS_CANCELED_EXIT_CODE,
    PROCESS_TIMEOUT_EXIT_CODE,
)
from .contracts import ArtifactRef, CheckpointStatus
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
    CheckpointReason,
    ExecutionActivityStatus,
    InfrastructureFailure,
    PersistenceStatus,
    ProgressRecommendation,
    SupervisorStatus,
    TerminalOutcome,
    classify_failure,
    classify_persistence_failure,
    failure_category,
)
from .execution_events import (
    ExecutionArtifactStore,
    ExecutionStreamEvent,
    ProcessEventSource,
    execution_payload_hash,
    has_meaningful_agent_progress,
    normalize_jsonl_line,
    redact_execution_text,
)
from .lifecycle_failure_harness import (
    LifecycleTransitionPoint,
    reach_lifecycle_transition,
)
from .process_ownership import prepare_process_launch
from .runtime_metrics import SupervisionMetrics
from .toolchains import project_environment, unprivileged_process_environment
from .worktree_observation import WorktreeLost, WorktreeObservation, observe_worktree

if TYPE_CHECKING:
    from .pow_wow.types import CommandRunCapture, ExecutionAttemptLease

_ResultT = TypeVar("_ResultT")

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


def require_checkpoint_identity(
    command: CreateExecutionCheckpoint, result: CoordinationResult
) -> str:
    """A checkpoint exists for this command only after its owner acknowledges its identity."""
    if (
        not isinstance(result, EntityResult)
        or result.command is not command.name
        or result.field != "checkpoint"
    ):
        raise TypeError("CreateExecutionCheckpoint requires a checkpoint entity")
    checkpoint_id = result.entity.require_str("checkpoint_id")
    if not checkpoint_id.strip():
        raise ValueError("checkpoint identity cannot be whitespace")
    return checkpoint_id


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
    activity_status: ExecutionActivityStatus = ExecutionActivityStatus.STARTING
    progress_recommendation: ProgressRecommendation | None = None
    worktree_observation: WorktreeObservation | None = None

    @property
    def allows_task_completion(self) -> bool:
        """A successful process cannot discharge failed supervision or persistence."""
        return (
            self.capture.exit_code == 0
            and self.agent_status is AgentStatus.COMPLETED
            and self.agent_failure is None
            and self.supervisor_status is SupervisorStatus.COMPLETED
            and self.supervisor_failure is None
            and self.supervisor_error is None
            and self.persistence_status is PersistenceStatus.COMPLETED
            and self.persistence_failure is None
            and self.checkpoint_reason is None
            and not self.deadline_reached
            and not self.cancel_requested
        )


@dataclass
class _SupervisionState:
    """Mutable facts shared by the supervisor's concurrent coroutines."""

    started_monotonic: float
    sequence: int = 0
    fatal_error: str | None = None
    last_progress_monotonic: float = 0.0
    last_progress_sequence: int = 0
    activity_status: ExecutionActivityStatus = ExecutionActivityStatus.STARTING
    assessment_started: bool = False
    progress_recommendation: ProgressRecommendation | None = None
    requested_checkpoint_reason: CheckpointReason | None = None
    persistence_failure: str | None = None

    def __post_init__(self) -> None:
        self.last_progress_monotonic = self.started_monotonic


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


@dataclass(frozen=True)
class _BoundedSupervisorIO:
    """The three blocking ports the async supervisor is allowed to invoke."""

    coordination_command: Callable[[CoordinationCommand], CoordinationResult]
    artifact_store: ExecutionArtifactStore
    progress_assessor: ProgressAssessor | None
    coordination_timeout_seconds: float
    artifact_timeout_seconds: float
    progress_timeout_seconds: float

    async def coordination(self, command: CoordinationCommand) -> CoordinationResult:
        result = await self._invoke(
            lambda: self.coordination_command(command),
            timeout_seconds=self.coordination_timeout_seconds,
        )
        return result

    async def write_text(
        self,
        *,
        role: str,
        text: str,
        workflow_id: str | None,
        schema_version: str,
        mime_type: str,
    ) -> ArtifactRef:
        return await self._invoke(
            lambda: self.artifact_store.write_text(
                role=role,
                text=text,
                workflow_id=workflow_id,
                schema_version=schema_version,
                mime_type=mime_type,
            ),
            timeout_seconds=self.artifact_timeout_seconds,
        )

    async def assess_progress(self, evidence: Mapping[str, object]) -> Mapping[str, object]:
        assessor = self.progress_assessor
        if assessor is None:
            raise RuntimeError("progress assessment was not configured")
        result = await self._invoke(
            lambda: assessor(evidence),
            timeout_seconds=self.progress_timeout_seconds,
        )
        return result

    @staticmethod
    async def _invoke(
        operation: Callable[[], _ResultT],
        *,
        timeout_seconds: float,
    ) -> _ResultT:
        """Bound a non-preemptible port without putting it on the loop executor."""

        loop = asyncio.get_running_loop()
        future: asyncio.Future[_ResultT] = loop.create_future()

        def settle_result(result: _ResultT) -> None:
            if future.done():
                return
            future.set_result(result)

        def settle_error(error: BaseException) -> None:
            if not future.done():
                future.set_exception(error)

        def invoke() -> None:
            try:
                result = operation()
            except BaseException as exc:
                if not loop.is_closed():
                    loop.call_soon_threadsafe(settle_error, exc)
            else:
                if not loop.is_closed():
                    loop.call_soon_threadsafe(settle_result, result)

        threading.Thread(target=invoke, daemon=True, name="agent-supervisor-io").start()
        return await asyncio.wait_for(future, timeout=timeout_seconds)


class StreamingCommandSupervisor:
    """Own one frontier process group, lease, event stream, and recovery checkpoint."""

    def __init__(
        self,
        *,
        coordination_command: Callable[[CoordinationCommand], CoordinationResult],
        artifact_writer: ExecutionArtifactStore,
        heartbeat_seconds: float = DEFAULT_AGENT_SUPERVISOR_HEARTBEAT_SECONDS,
        warning_seconds: float = DEFAULT_AGENT_SUPERVISOR_WARNING_SECONDS,
        termination_grace_seconds: float = DEFAULT_AGENT_SUPERVISOR_TERMINATION_GRACE_SECONDS,
        quiet_seconds: float = DEFAULT_AGENT_SUPERVISOR_QUIET_SECONDS,
        stalled_seconds: float = DEFAULT_AGENT_SUPERVISOR_STALLED_SECONDS,
        progress_assessor: ProgressAssessor | None = None,
        coordination_timeout_seconds: float = DEFAULT_COORDINATION_COMMAND_TIMEOUT_SECONDS,
        git_timeout_seconds: float = DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS,
        progress_assessment_timeout_seconds: float = DEFAULT_PROGRESS_ASSESSMENT_TIMEOUT_SECONDS,
        artifact_write_timeout_seconds: float = DEFAULT_ARTIFACT_WRITE_TIMEOUT_SECONDS,
        stream_drain_timeout_seconds: float = DEFAULT_STREAM_DRAIN_TIMEOUT_SECONDS,
    ) -> None:
        self.io = _BoundedSupervisorIO(
            coordination_command=coordination_command,
            artifact_store=artifact_writer,
            progress_assessor=progress_assessor,
            coordination_timeout_seconds=coordination_timeout_seconds,
            artifact_timeout_seconds=artifact_write_timeout_seconds,
            progress_timeout_seconds=progress_assessment_timeout_seconds,
        )
        self.heartbeat_seconds = heartbeat_seconds
        self.warning_seconds = warning_seconds
        self.termination_grace_seconds = termination_grace_seconds
        if quiet_seconds <= 0 or stalled_seconds <= quiet_seconds:
            raise ValueError("stalled_seconds must be greater than quiet_seconds > 0")
        self.quiet_seconds = quiet_seconds
        self.stalled_seconds = stalled_seconds
        self.git_timeout_seconds = git_timeout_seconds
        self.stream_drain_timeout_seconds = stream_drain_timeout_seconds

    async def _coord(self, command: CoordinationCommand) -> CoordinationResult:
        return await self.io.coordination(command)

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
        state = _SupervisionState(started_monotonic=started)
        measurements = SupervisionMetrics(harness)
        sequence_lock = asyncio.Lock()
        transcript: list[str] = []
        safe_stdout: list[str] = []
        safe_stderr: list[str] = []
        event_tail: list[str] = []
        cancel_event = asyncio.Event()
        stop_heartbeat = asyncio.Event()
        stop_activity = asyncio.Event()
        recent_progress_signatures: list[str] = []

        async def persist(
            source: ProcessEventSource | str,
            kind: str,
            payload: dict[str, object],
        ) -> int:
            source = ProcessEventSource(source)
            with measurements.pending(source):
                return await persist_sequenced(source, kind, payload)

        async def persist_sequenced(
            source: ProcessEventSource,
            kind: str,
            payload: dict[str, object],
        ) -> int:
            async with sequence_lock:
                state.sequence += 1
                event = ExecutionStreamEvent(
                    lease_id=lease_id,
                    sequence=state.sequence,
                    occurred_at=time.time(),
                    source=source,
                    kind=kind,
                    payload=payload,
                    payload_sha256=execution_payload_hash(payload),
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
                del event_tail[:-AGENT_EVENT_SUMMARY_TAIL_SIZE]
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
                    state.fatal_error = f"event persistence failed: {type(exc).__name__}: {exc}"
                    cancel_event.set()
                return event.sequence

        async def mark_progress(observed_sequence: int, observed_kind: str) -> None:
            state.last_progress_monotonic = time.monotonic()
            state.activity_status = ExecutionActivityStatus.PROGRESSING
            progress_sequence = await persist(
                "lifecycle",
                "activity.progress",
                {
                    "observed_sequence": observed_sequence,
                    "observed_kind": observed_kind,
                },
            )
            state.last_progress_sequence = progress_sequence

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
        launch = prepare_process_launch(
            command,
            cwd,
            unprivileged_process_environment(
                dict(env or {}) if complete_environment else project_environment(cwd, env)
            ),
        )
        process = await asyncio.create_subprocess_exec(
            *launch.command,
            cwd=cwd,
            env=launch.environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=launch.ownership.start_new_session,
            limit=AGENT_EVENT_STREAM_READER_LIMIT_BYTES,
        )
        started_sequence = await persist("lifecycle", "process.started", {"pid": process.pid})
        await mark_progress(started_sequence, "process.started")

        async def read_stream(
            stream: asyncio.StreamReader,
            source: ProcessEventSource,
        ) -> None:
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
                    state.fatal_error = (
                        f"injected lifecycle stream failure: {type(exc).__name__}: {exc}"
                    )
                    cancel_event.set()
                    return
                if has_meaningful_agent_progress(source, kind):
                    signature = f"{kind}:{execution_payload_hash(payload)}"
                    if signature not in recent_progress_signatures:
                        recent_progress_signatures.append(signature)
                        del recent_progress_signatures[:-AGENT_PROGRESS_SIGNATURE_WINDOW_SIZE]
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
                        if cancel_requested:
                            measurements.cancel_observed(
                                result.entity.values.get("cancel_requested_at"),
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
                    state.fatal_error = f"lease heartbeat failed: {type(exc).__name__}: {exc}"
                    cancel_event.set()
                    return
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_heartbeat.wait(), timeout=self.heartbeat_seconds)

        async def activity_monitor() -> None:
            poll_seconds = max(
                AGENT_ACTIVITY_MINIMUM_POLL_SECONDS,
                min(self.heartbeat_seconds, self.quiet_seconds / 4),
            )
            while not stop_activity.is_set():
                silent_seconds = time.monotonic() - state.last_progress_monotonic
                if silent_seconds >= self.stalled_seconds and not state.assessment_started:
                    state.activity_status = ExecutionActivityStatus.STALLED_SUSPECTED
                    await persist(
                        "lifecycle",
                        "activity.stalled_suspected",
                        {
                            "silent_seconds": round(silent_seconds, 3),
                            "last_meaningful_progress_sequence": state.last_progress_sequence,
                            "heartbeat_is_progress": False,
                        },
                    )
                    state.assessment_started = True
                    if self.io.progress_assessor is not None:
                        evidence: dict[str, object] = {
                            "schema_version": "execution_progress_evidence.v1",
                            "lease_id": lease_id,
                            "harness": harness,
                            "pid": process.pid,
                            "elapsed_seconds": round(time.monotonic() - started, 3),
                            "silent_seconds": round(silent_seconds, 3),
                            "last_meaningful_progress_sequence": state.last_progress_sequence,
                            "recent_events": list(event_tail),
                            "task_contract": task_contract[
                                :AGENT_PROGRESS_EVIDENCE_TASK_CONTRACT_LIMIT
                            ],
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
                            decision = dict(await self.io.assess_progress(evidence))
                            try:
                                recommendation = ProgressRecommendation(
                                    str(decision.get("recommendation") or "").upper()
                                )
                            except ValueError:
                                raise ValueError(
                                    "junior recommendation must be CONTINUE, CHECKPOINT, "
                                    "SPLIT, or PAUSE_OPERATOR"
                                ) from None
                            state.progress_recommendation = recommendation
                            raw_continuations = decision.get("continuations")
                            continuations = (
                                list(raw_continuations)
                                if isinstance(raw_continuations, list)
                                else []
                            )
                            decision_payload: dict[str, object] = {
                                "schema_version": "execution_progress_assessment.v1",
                                "recommendation": recommendation.value,
                                "rationale": str(decision.get("rationale") or ""),
                                "continuations": continuations,
                            }
                            await persist(
                                "lifecycle", "progress_assessment.completed", decision_payload
                            )
                            if recommendation is not ProgressRecommendation.CONTINUE:
                                state.requested_checkpoint_reason = (
                                    CheckpointReason.STALLED_PROGRESS
                                )
                                cancel_event.set()
                                return
                        except Exception as exc:  # advisory failure never owns the process
                            await persist(
                                "lifecycle",
                                "progress_assessment.failed",
                                {"error": redact_execution_text(f"{type(exc).__name__}: {exc}")},
                            )
                elif silent_seconds >= self.quiet_seconds and state.activity_status not in {
                    ExecutionActivityStatus.QUIET,
                    ExecutionActivityStatus.STALLED_SUSPECTED,
                }:
                    state.activity_status = ExecutionActivityStatus.QUIET
                    await persist(
                        "lifecycle",
                        "activity.quiet",
                        {
                            "silent_seconds": round(silent_seconds, 3),
                            "last_meaningful_progress_sequence": state.last_progress_sequence,
                            "heartbeat_is_progress": False,
                        },
                    )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_activity.wait(), timeout=poll_seconds)

        if process.stdout is None or process.stderr is None:
            raise RuntimeError("supervised process pipes were not created")
        stdout_stream, stderr_stream = process.stdout, process.stderr
        stdout_task = asyncio.create_task(read_stream(stdout_stream, ProcessEventSource.STDOUT))
        stderr_task = asyncio.create_task(read_stream(stderr_stream, ProcessEventSource.STDERR))
        heartbeat_task = asyncio.create_task(heartbeat())
        activity_task = asyncio.create_task(activity_monitor())
        process_task = asyncio.create_task(process.wait())

        async def sample_stream_buffers() -> None:
            try:
                while True:
                    measurements.sample_buffer("stdout", stdout_stream)
                    measurements.sample_buffer("stderr", stderr_stream)
                    await asyncio.sleep(AGENT_PROCESS_EXIT_POLL_SECONDS)
            finally:
                measurements.close_buffers()

        buffer_task = asyncio.create_task(sample_stream_buffers())

        async def observe_process_exit() -> None:
            # asyncio's subprocess wait future can remain pending after the
            # supervised PID exits when an escaped descendant inherits one of
            # the captured pipes. The transport still records returncode as
            # soon as the direct child exits, so observe that independently
            # and let the bounded stream-drain path close orphaned pipes.
            while process.returncode is None:
                await asyncio.sleep(AGENT_PROCESS_EXIT_POLL_SECONDS)
            measurements.exit_observed()

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
        if state.requested_checkpoint_reason is not None:
            checkpoint_reason = state.requested_checkpoint_reason
        elif state.fatal_error:
            checkpoint_reason = CheckpointReason.SUPERVISOR_ERROR
        elif deadline_reached:
            checkpoint_reason = CheckpointReason.DEADLINE
        elif cancel_requested:
            checkpoint_reason = CheckpointReason.OPERATOR_CANCEL

        termination_reason = checkpoint_reason if process.returncode is None else None
        if termination_reason is not None:
            await persist(
                "lifecycle",
                f"{checkpoint_reason}.reached",
                {"elapsed_seconds": round(time.monotonic() - started, 3)},
            )
            if checkpoint_reason is CheckpointReason.DEADLINE:
                with contextlib.suppress(Exception):
                    await self._coord(
                        RequestExecutionCancel(
                            lease_id=lease_id,
                            reason="frontier execution deadline reached",
                            requested_by="streaming-supervisor",
                        )
                    )
            with contextlib.suppress(ProcessLookupError):
                measurements.signal_sent()
                launch.ownership.send_signal(process.pid, signal.SIGTERM)
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
                    launch.ownership.send_signal(process.pid, signal.SIGKILL)
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

        if process.returncode is not None:
            measurements.exit_observed()
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
        buffer_task.cancel()
        await asyncio.gather(buffer_task, return_exceptions=True)
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
                # Preserve monotonic precision so serialization keeps observation order.
                "elapsed_seconds": time.monotonic() - started,
                "exit_observed_elapsed_seconds": (
                    measurements.exit_observed_at - started
                    if measurements.exit_observed_at is not None
                    else None
                ),
            },
        )

        combined_output = "\n".join((*safe_stdout, *safe_stderr))
        inspection_failure = inspection_process_failure(
            command, stdout="\n".join(safe_stdout), exit_code=returncode
        )
        agent_outcome = (
            TerminalOutcome(inspection_failure.terminal_outcome)
            if inspection_failure is not None
            else classify_failure(combined_output)
            if returncode
            else None
        )
        worktree_observation = None
        if returncode and source_repo_path is not None and base_head_sha is not None:
            worktree_observation = await asyncio.to_thread(observe_worktree, cwd)
            if isinstance(worktree_observation, WorktreeLost):
                agent_outcome = TerminalOutcome.EXECUTION_ENVIRONMENT_LOST
            await persist(
                "lifecycle",
                "worktree.observed",
                {
                    "observation": type(worktree_observation).__name__,
                    "path": str(worktree_observation.path),
                },
            )
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

        async def write_and_attach_artifact(
            *,
            role: str,
            text: str,
            schema_version: str,
            mime_type: str = "text/plain",
        ) -> str | None:
            try:
                ref = await self.io.write_text(
                    role=role,
                    text=text,
                    workflow_id=artifact_workflow_id,
                    schema_version=schema_version,
                    mime_type=mime_type,
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
                state.persistence_failure = classified.value
                detail = f"{role}: {type(exc).__name__}: {exc}"
                persistence_errors.append(detail)
                await persist(
                    "lifecycle",
                    "artifact.persist.failed",
                    {
                        "role": role,
                        "failure": classified.value,
                        "error": redact_execution_text(detail),
                    },
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
        checkpoint_error = state.fatal_error
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
                CheckpointStatus.FAILED
                if checkpoint_error
                else (
                    CheckpointStatus.PAUSED
                    if checkpoint_reason
                    in {CheckpointReason.OPERATOR_CANCEL, CheckpointReason.STALLED_PROGRESS}
                    else CheckpointStatus.PENDING_JUNIOR
                )
            )
            reach_lifecycle_transition(
                LifecycleTransitionPoint.BEFORE_CHECKPOINT_PERSISTED,
                lease_id=lease_id,
                reason=checkpoint_reason,
                status=checkpoint_status.value,
                worktree_path=str(cwd),
                base_head_sha=base_head_sha,
            )
            checkpoint_command = CreateExecutionCheckpoint(
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
                task_contract=task_contract[:AGENT_CHECKPOINT_TASK_CONTRACT_LIMIT],
                event_summary="\n".join(event_tail),
                submit_review=checkpoint_status is CheckpointStatus.PENDING_JUNIOR,
                error=checkpoint_error,
            )
            try:
                result = await self._coord(checkpoint_command)
            except (TypeError, ValueError, AssertionError):
                # A malformed coordination contract is not a storage outage.
                raise
            except Exception as exc:
                state.persistence_failure = InfrastructureFailure.CHECKPOINT_WRITE_FAILED.value
                detail = redact_execution_text(
                    f"checkpoint persistence failed: {type(exc).__name__}: {exc}"
                )
                persistence_errors.append(detail)
                checkpoint_error = f"{checkpoint_error}; {detail}" if checkpoint_error else detail
                await persist(
                    "lifecycle",
                    "checkpoint.persist.failed",
                    {
                        "failure": InfrastructureFailure.CHECKPOINT_WRITE_FAILED.value,
                        "reason": checkpoint_reason.value,
                        "artifact_ids": checkpoint_artifact_ids,
                        "error": detail,
                    },
                )
            else:
                checkpoint_id = require_checkpoint_identity(checkpoint_command, result)
                await persist(
                    "lifecycle",
                    "checkpoint.created",
                    {
                        "checkpoint_id": checkpoint_id,
                        "reason": checkpoint_reason.value,
                        "status": checkpoint_status.value,
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

        termination_notice: str | None = None
        if termination_reason is CheckpointReason.DEADLINE:
            exit_code = PROCESS_TIMEOUT_EXIT_CODE
            termination_notice = "process timed out"
        elif termination_reason is not None:
            exit_code = PROCESS_CANCELED_EXIT_CODE
            termination_notice = f"process canceled ({termination_reason.value})"
        else:
            exit_code = returncode
        stderr_parts = list(safe_stderr)
        if termination_notice is not None:
            stderr_parts.append(termination_notice)
        if checkpoint_reason is not None:
            stderr_parts.append(
                "checkpoint recorded" if checkpoint_id else "checkpoint persistence failed"
            )
        stderr = "\n".join(stderr_parts)
        from .pow_wow.types import CommandRunCapture

        capture = CommandRunCapture(
            command=shlex.join(str(part) for part in command),
            cwd=str(cwd),
            stdout="\n".join(safe_stdout),
            stderr=stderr,
            exit_code=exit_code,
        )
        if termination_reason is CheckpointReason.DEADLINE and not isinstance(
            worktree_observation, WorktreeLost
        ):
            agent_failure = InfrastructureFailure.DEADLINE_EXCEEDED.value
            category = failure_category(agent_failure)
        if state.fatal_error and state.fatal_error.startswith("event persistence failed"):
            state.persistence_failure = (
                state.persistence_failure or InfrastructureFailure.EVENT_WRITE_FAILED.value
            )
            persistence_errors.append(state.fatal_error)
        supervisor_failure = (
            state.fatal_error if checkpoint_reason is CheckpointReason.SUPERVISOR_ERROR else None
        )
        return SupervisedCommandResult(
            capture=capture,
            deadline_reached=deadline_reached,
            cancel_requested=cancel_requested,
            transcript_artifact_id=transcript_artifact_id,
            checkpoint_id=checkpoint_id,
            checkpoint_artifact_ids=tuple(checkpoint_artifact_ids),
            checkpoint_reason=checkpoint_reason,
            preserve_worktree=preserve_worktree,
            event_count=state.sequence,
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
            persistence_failure=state.persistence_failure,
            activity_status=ExecutionActivityStatus.TERMINAL,
            progress_recommendation=state.progress_recommendation,
            worktree_observation=worktree_observation,
        )


__all__ = [
    "StreamingCommandSupervisor",
    "SupervisedCommandResult",
    "require_checkpoint_identity",
    "has_meaningful_agent_progress",
    "normalize_jsonl_line",
]
