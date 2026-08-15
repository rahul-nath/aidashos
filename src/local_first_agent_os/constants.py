# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Globally-shared constants for local_first_agent_os.

Centralises tunables that don't belong in environment-driven `Settings`
(see settings.py for those) and that are referenced from more than one
module. Keep this file small and dependency-free.
"""

from __future__ import annotations

# --- Merge policy -------------------------------------------------------------

# Architecture invariant: no agent branch merges into a target project without
# an operator-resolved CODE_MERGE approval. Every dispatch contract, run
# result, and durable payload derives its merge behaviour from this single
# decision.
AGENT_BRANCH_AUTO_MERGE: bool = False

# --- Dispatch settlement notification ----------------------------------------

# The DBOS topic a milestone waits on and the dispatch ledger sends to when the
# intent settles. It lives here, rather than in either of them, because a sender
# and a receiver that each spelled their own topic would fail silently: the
# waiter would simply never wake and time out an hour later, which reads exactly
# like a slow agent. One function, imported by both, makes that undetectable
# mismatch unrepresentable.
#
# Keyed by intent rather than by milestone so a settle can only ever wake the
# wait that submitted it.


def dispatch_settlement_topic(intent_id: str) -> str:
    return f"dispatch_settled:{intent_id}"


# The same arrangement for the other thing a milestone waits on. An operator
# decision was the one wait that stayed a poll after the dispatch wait became an
# event, and the cost was not the latency: `read_operator_decision_step` is a DBOS
# step, so every poll checkpointed a row, and the number of rows depended on how
# long a human took to click. That made the workflow body non-deterministic on
# replay, which is the defect. Waiting on a topic writes nothing while it waits.
#
# Keyed by request rather than by milestone for the same reason as above: a
# decision can only wake the wait that asked for it.


def operator_decision_topic(request_id: str) -> str:
    return f"operator_decided:{request_id}"


# --- Coordination vocabulary --------------------------------------------------

# Branches created for agent work in isolated worktrees are namespaced under
# one prefix so operators and recovery flows can recognize agent-owned refs.
AGENT_WORKTREE_BRANCH_PREFIX: str = "agent/"

# Closed vocabulary of operator approval request types. The typed view is
# contracts.ApprovalRequestType, verified against this tuple at import time.
# The tuple lives here because the import-light coordination CLI validates
# request types without loading pydantic-backed contracts.
APPROVAL_REQUEST_TYPES: tuple[str, ...] = (
    "PURCHASE",
    "EXTERNAL_COMMS",
    "CODE_MERGE",
    "MODEL_ESCALATION",
    "GENERAL",
)

# Durable label for the executor's retry stance, stamped into failure payloads
# so later readers know which retry semantics produced the record.
DISPATCH_RETRY_POLICY: str = "coarse_single_attempt_then_requeue_or_fallback"

# Claimant identity the background dispatcher records on intents it claims
# when no explicit dispatcher name is configured.
DEFAULT_DISPATCHER_NAME: str = "pi-dispatcher"

# Artifact type vocabulary shared by the pow-wow executor (emitter) and the
# planning, prompt-view, intake, and dispatcher-runner consumers.
CLI_AGENT_RUN_ARTIFACT_TYPE: str = "cli_agent_run"
DELEGATED_TASK_RUN_ARTIFACT_TYPE: str = "delegated_task_run"
FRONTIER_FALLBACK_RUN_ARTIFACT_TYPE: str = "frontier_fallback_run"
REPO_AUDIT_ARTIFACT_TYPE: str = "repo_audit"

# --- Local state root ---------------------------------------------------------

# Single dot-directory under the user's home that owns durable local agent
# state: logs, artifacts, worktrees, spool, coordination, and daemon files.
LOCAL_AGENT_STATE_DIR_NAME: str = ".local-agent"

# Resident daemons (pi-daemon, session-daemon) share one state directory for
# pid, lock, and log files; tests point the env var at isolated directories.
DAEMON_STATE_DIR_ENV_VAR: str = "LOCAL_AGENT_DAEMON_DIR"
DEFAULT_DAEMON_STATE_DIR: str = f"~/{LOCAL_AGENT_STATE_DIR_NAME}/daemon"

# --- Agent and model execution ------------------------------------------------

# A delegated agent or model request is inherently nondeterministic: it may
# explore, run tools, compile, or wait on a provider. Give it a full hour before
# the control plane considers it timed out and records a durable recovery
# boundary. This does not apply to health checks, database locks, or interactive
# product-level latency budgets.
DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS: int = 3600

# How long the supervisor lets one dispatched agent process run.
#
# Deliberately above the largest budget any milestone declares for itself
# (`implement.code_change` asks for 5400s in `work_units/executors.py`), because
# this clock and that one race and this one always wins. Sharing
# `DEFAULT_AGENT_MODEL_TIMEOUT_SECONDS` meant the implement milestone was killed
# at 3600s and parked BLOCKED with `dispatch_paused`, so its declared 90 minutes
# could never be reached - a bound that cannot fire, which is the same shape as a
# check that cannot fail.
#
# `test_no_milestone_budget_is_unreachable_under_the_shipped_process_cap` fails
# if a milestone ever declares more than this again.
DEFAULT_SAGA_TASK_TIMEOUT_SECONDS: int = 7200

# How long one declared verification command may run before the gate calls it
# failed.
#
# Its own bound, well under the task cap, because the two clocks measure
# different things. The task cap is how long an agent may think; this is how long
# a deterministic check may take, and a check that has not answered in fifteen
# minutes is not answering.
#
# Every verification call site used to pass the task timeout, so a hanging test
# consumed the agent's entire budget and reported nothing until the two-hour
# process cap fired. That happened: a project declaring a bare `uv run pytest`
# sat in one for half an hour with a live worktree and no signal, which reads
# exactly like a working run to anybody watching. A gate that cannot report red
# promptly is the same shape as a gate that cannot fail.
#
# Generous rather than tight because it must not fail an honest suite: this
# repository's own filtered suite runs in about three and a half minutes against
# a warm cache, and a cold one in an agent worktree is slower.
DEFAULT_VERIFICATION_COMMAND_TIMEOUT_SECONDS: int = 900

# A coordination command is one small typed ledger transition, not a model
# turn.  Keep this budget short enough that heartbeats and terminalization
# cannot be starved by a wedged CLI/database call.
DEFAULT_COORDINATION_COMMAND_TIMEOUT_SECONDS: int = 30

# Git metadata and worktree operations are normally sub-second, but can block
# on stale locks, filesystem faults, hooks, credential helpers, or a wedged
# network filesystem.  The timeout is a failure detector, not a latency target.
DEFAULT_GIT_OPERATION_TIMEOUT_SECONDS: int = 30

# The junior progress assessor is created only after the deterministic
# supervisor detects a stall.  It is advisory and gets a model-sized budget of
# its own; it must not inherit the full frontier-agent hour.
DEFAULT_PROGRESS_ASSESSMENT_TIMEOUT_SECONDS: int = 300

# Artifact persistence includes fsync, a database insert, and optionally a
# MinIO upload.  Bound the supervisor's wait so a failed disk or object store
# cannot prevent lease terminalization.
DEFAULT_ARTIFACT_WRITE_TIMEOUT_SECONDS: int = 60

# Once the supervised PID is terminal, pipe readers should reach EOF promptly.
# Descendants can retain inherited descriptors, so draining still needs a hard
# recovery boundary.
DEFAULT_STREAM_DRAIN_TIMEOUT_SECONDS: int = 10

# The Pi daemon keeps long commands observable with NDJSON liveness events.
# The client idle deadline is deliberately longer than the heartbeat cadence:
# model work may run for an hour, but a silent/dead transport must not.
DEFAULT_PI_STREAM_HEARTBEAT_SECONDS: int = 10
DEFAULT_PI_STREAM_IDLE_TIMEOUT_SECONDS: int = 30

# --- ASR (whisper.cpp streaming) ---------------------------------------------

# Maximum inactivity (no *transcribed speech*) before an `/asr` streaming
# session ends. The clock resets only when whisper returns real text — raw VAD
# frames (which fire on ambient noise) do not keep the session alive.
ASR_INACTIVITY_TIMEOUT_S: int = 600  # 10 minutes

# Absolute wall-clock cap on a single `/asr` session, never reset by activity.
# A hard ceiling so the microphone can never be held open indefinitely (e.g.
# if VAD keeps mis-firing on background noise).
ASR_MAX_SESSION_S: int = 3600  # 1 hour

# Audio capture & VAD framing for the ASR client.
# whisper.cpp expects 16-bit mono PCM at 16 kHz.
ASR_SAMPLE_RATE_HZ: int = 16_000
ASR_FRAME_MS: int = 30  # webrtcvad accepts 10/20/30 ms; 30 ms = 480 samples
ASR_VAD_AGGRESSIVENESS: int = 2  # 0..3, higher = filters more non-speech

# Minimum voiced frames to count a burst as "real speech" (cuts pops/clicks).
ASR_MIN_VOICED_FRAMES: int = 6  # ~180 ms
# Silence frames that close out an in-progress voiced segment (flush to whisper).
ASR_SILENCE_FRAMES_TO_CUT: int = 25  # ~750 ms
# Hard cap on a single voiced segment before force-flushing, so a long
# monologue still produces rolling transcription chunks.
ASR_MAX_SEGMENT_SECONDS: float = 12.0
