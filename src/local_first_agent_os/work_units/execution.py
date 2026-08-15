# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Running one milestone: the boundary between the lifecycle and the world.

The lifecycle decides when a milestone may run and what evidence it owes. How the
work actually happens belongs to a runtime behind this boundary, so a milestone
can be executed by the existing dispatch ledger, by a bounded command, or by a
deterministic simulation without the lifecycle knowing the difference.

Operator approval is deliberately not a runtime concern. The engine owns approval
gates, because a runtime that could decide it had been approved is a runtime that
could grant itself permission.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, runtime_checkable

from ..contracts import (
    DispatchIntentStatus,
    DispatchProgress,
    classify_dispatch_progress,
)
from ..coordination.outcomes import TerminalOutcome
from ..coordination.store import rowdict, tx
from ..ids import sha256_text
from .events import (
    ArtifactKind,
    ArtifactRecord,
    ArtifactType,
    DiagnosticArtifact,
    DiagnosticArtifactKind,
    DispatchIntentCreated,
    RequirableArtifact,
)
from .executors import EXECUTOR_REGISTRY, ExecutorKind
from .lifecycle import FailureClass
from .plan import CompiledMilestone, DocumentContext


@dataclass(frozen=True)
class MilestoneContext:
    """Everything a milestone execution is allowed to know.

    It carries the immutable execution identity, not a handle to mutable state: a
    runtime cannot look up "the latest plan" from here, because there is nothing
    here that would let it.
    """

    work_unit_id: str
    root_workflow_id: str
    child_workflow_id: str
    milestone: CompiledMilestone
    attempt: int
    design_doc_revision_id: str
    compiled_plan_hash: str
    design_doc_excerpt: str = ""
    """The document's own context, rendered by the plan and put in the prompt.

    Empty means a caller built a context without one, which tests do; the prompt
    then omits the preamble entirely rather than carrying empty headings.
    """

    document_context: DocumentContext = field(default_factory=DocumentContext)

    target_project_id: str = ""
    """The plan's target project, carried so the runtime does not have to guess.

    Empty means a caller built a context without one, which older callers and
    tests do; the runtime falls back exactly as it did before rather than failing
    a milestone over a field its plan predates.
    """

    @property
    def executor_kind(self) -> ExecutorKind:
        return self.milestone.executor_kind

    @property
    def permitted_tools(self) -> tuple[str, ...]:
        return self.milestone.tool_policy.permitted_tools


@dataclass(frozen=True)
class MilestoneSucceeded:
    result_summary: str
    artifacts: tuple[ArtifactRecord, ...]


@dataclass(frozen=True)
class MilestoneFailed:
    failure_class: FailureClass
    failure_code: str
    failure_summary: str
    artifacts: tuple[ArtifactRecord, ...] = ()


MilestoneOutcome = MilestoneSucceeded | MilestoneFailed


@dataclass(frozen=True)
class MilestoneAwaitingDispatch:
    """The work was submitted and its result will arrive later, as an event.

    A third answer to "what happened when you ran this milestone", alongside
    succeeded and failed. It exists because the previous contract had only the
    two: a runtime whose answer was "ask me in an hour" had nowhere to say so, so
    it slept inside its own call until it could say one of the other two.

    Modelling the pending case as a value rather than as a blocked call is what
    lets the waiting move out of a DBOS step and into the workflow body, which is
    the only place `DBOS.recv` may be called.
    """

    dispatch_intent_id: str
    timeout_seconds: float


MilestoneStart = MilestoneOutcome | MilestoneAwaitingDispatch


class MilestoneExecutorRuntime(Protocol):
    """What the engine needs from anything that can run a milestone.

    One method, because one is all a runtime that finishes its own work needs.
    A simulation, a bounded command, and a pure computation are all complete at
    this contract and none of them should have to grow methods to say so.
    """

    def run(self, context: MilestoneContext) -> MilestoneOutcome: ...


@runtime_checkable
class DeferrableMilestoneRuntime(Protocol):
    """A runtime whose work outlives the call that started it.

    ``start`` performs the side effect and returns either a finished outcome or
    the token of something to wait for. ``settle`` turns that token into an
    outcome once the wait has ended, however it ended.

    Separate from `MilestoneExecutorRuntime` rather than folded into it because
    the ability to defer is a real distinction between runtimes, not a method
    every runtime owes an implementation of. The engine asks whether a runtime
    is one of these; a runtime that is not keeps working exactly as it did, and
    the engine keeps the wait inside the step where such a runtime finishes it.

    Checked with `isinstance` rather than a capability flag, so the question
    "can this defer" is answered by the type instead of by a boolean someone has
    to remember to set.
    """

    def start(self, context: MilestoneContext) -> MilestoneStart: ...

    def settle(
        self, context: MilestoneContext, awaiting: MilestoneAwaitingDispatch
    ) -> MilestoneOutcome: ...


@runtime_checkable
class DispatchLedgerPoller(Protocol):
    """A runtime that can watch a dispatch intent without a notification channel.

    Asked by type for the same reason ``DeferrableMilestoneRuntime`` is. The
    caller used to probe `getattr(runtime, "wait_for", None)` and raise on a
    missing attribute, which is a capability check spelled as a string lookup:
    it cannot be found by find-references, it does not survive a rename, and it
    silently accepts any object with an attribute of that name.
    """

    def poll_until_stopped(
        self, intent_id: str, timeout_seconds: float | None = None
    ) -> DispatchWaitResult: ...


def evidence_artifact(
    context: MilestoneContext,
    artifact_type: ArtifactType,
    content: str,
    *,
    step_name: str,
    media_type: str = "text/plain",
    metadata: dict[str, Any] | None = None,
) -> ArtifactRecord:
    """Build one content-addressed artifact reference.

    Large contents live outside the row; the database keeps the URI, the hash, and
    the metadata. The hash covers the content, so "the same evidence" is a fact
    rather than a claim.

    Taking an ``ArtifactType`` rather than a string is what keeps a producer from
    inventing a kind. Every artifact this system writes is minted here or in
    ``json_evidence_artifact``, so a name that belongs to neither closed set has
    to be spelled ``UnrecognizedArtifact`` out loud to get through.
    """

    content_hash = sha256_text(content)
    return ArtifactRecord(
        artifact_type=artifact_type,
        uri=(
            f"workunit://{context.work_unit_id}"
            f"/{context.milestone.stable_key}/{artifact_type.value}"
        ),
        content_hash=content_hash,
        media_type=media_type,
        size_bytes=len(content.encode("utf-8")),
        producer_step_name=step_name,
        metadata=dict(metadata or {}),
    )


def json_evidence_artifact(
    context: MilestoneContext,
    artifact_type: ArtifactType,
    payload: Mapping[str, Any],
    *,
    step_name: str,
    metadata: dict[str, Any] | None = None,
) -> ArtifactRecord:
    """The same, for evidence that is a document rather than prose.

    Serialised here rather than at each call site so the bytes that get hashed are
    produced one way. ``sort_keys`` is what makes the hash a fact: the same
    evidence composed twice has to hash the same, and dictionary order is not
    something a caller should have to think about to get that.

    The media type stops being a lie in the process. A ``delivery_record.v1``
    stored as ``text/plain`` reads as prose to anything that consumes artifacts by
    type, which is the sort of detail Hyrum's law eventually turns into a bug.
    """

    return evidence_artifact(
        context,
        artifact_type,
        json.dumps(payload, sort_keys=True),
        step_name=step_name,
        media_type="application/json",
        metadata=metadata,
    )


_DISPATCH_FAILURE_EVIDENCE_TYPE: Final = DiagnosticArtifact(
    DiagnosticArtifactKind.DISPATCH_FAILURE_EVIDENCE
)

# Metadata rides in a ledger row an operator reads, not a blob store.
_FAILURE_EVIDENCE_LIMIT: Final = 2000


def _dispatch_failure_causes(run_result: Mapping[str, Any] | None) -> tuple[str, ...]:
    """What the run said went wrong, nearest cause first.

    A run's own `risks` summarise the dispatch; a failed task's `risks` name the
    harness that produced them. The task-level entries come first because they
    are what an operator acts on: "claude reported: Not logged in" is a command
    to run, and "1 task failed" is not.
    """

    if run_result is None:
        return ()
    causes: list[str] = []
    tasks = run_result.get("tasks")
    if isinstance(tasks, Sequence) and not isinstance(tasks, str | bytes):
        for task in tasks:
            if not isinstance(task, Mapping) or task.get("status") != "failed":
                continue
            name = str(task.get("task_name") or "task")
            for risk in task.get("risks") or ():
                causes.append(f"{name}: {risk}")
    for risk in run_result.get("risks") or ():
        text = str(risk)
        if text not in causes:
            causes.append(text)
    return tuple(causes)


def _dispatch_failure_evidence(
    context: MilestoneContext,
    intent_id: str,
    status: DispatchIntentStatus,
    result_text: str,
) -> tuple[ArtifactRecord, ...]:
    """Keep the evidence a failed dispatch already wrote, instead of dropping it.

    The settled row carries the whole `dispatch_runner_result.v1` payload on
    failure exactly as it does on success, because the dispatcher builds it
    before it branches on the outcome. Reading only the `error` column threw away
    tens of kilobytes of captured agent output that was sitting in the row this
    function was already handed, and left `list_work_unit_artifacts` empty for
    precisely the runs an operator most needs to inspect.

    This is not the milestone's required evidence and must never be mistaken for
    it. A failed run genuinely has no `source_patch`, and manufacturing one is
    the fabrication the evidence gate exists to prevent. What it has is a reason,
    so that is what gets recorded, alongside the intent id that still holds the
    unabridged payload.

    Nothing is minted when the payload is absent. A runner that crashed before
    reporting writes `result=None`, and an artifact asserting evidence that was
    never captured would be the same lie in the other direction.
    """

    run_result = _agent_run_result(result_text)
    if run_result is None:
        return ()
    causes = _dispatch_failure_causes(run_result)
    body = "\n".join(causes) if causes else str(run_result.get("output_summary") or "")
    if not body:
        return ()
    if len(body) > _FAILURE_EVIDENCE_LIMIT:
        body = f"{body[:_FAILURE_EVIDENCE_LIMIT]}..."
    return (
        evidence_artifact(
            context,
            _DISPATCH_FAILURE_EVIDENCE_TYPE,
            content=body,
            step_name=f"dispatch:{intent_id}",
            metadata={
                "dispatch_intent_id": intent_id,
                "dispatch_status": str(status),
                "causes": list(causes[:20]),
                "result_payload_bytes": len(result_text.encode("utf-8")),
            },
        ),
    )


_NO_FAULT_OUTCOMES: Final = frozenset(
    {
        TerminalOutcome.TRANSPORT_INTERRUPTED.value,
        TerminalOutcome.PROVIDER_OVERLOADED.value,
    }
)
"""Dispatch outcomes where the provider failed and the milestone's work did not.

Deliberately narrow rather than every `InfrastructureFailure`. The budget
exists to stop a milestone retrying forever, and most infrastructure outcomes
cannot be distinguished from work that would fail again the same way -
`UNKNOWN_FAILURE` most of all, which is what an unrecognised message becomes. An
interrupted transport is different in kind: the request died in flight, so there
is no attempt to judge and nothing was learned by making it.

`PROVIDER_OVERLOADED` clears the same bar and clears it further. A 529 is the
provider declining to start the work at all, and unlike every other refusal it
is measured in moments, so the retry this exempts is one that genuinely succeeds
rather than one that re-earns the same answer. It was added after an
`API Error: 529 Overloaded` was read as `DEPENDENCY_FAILED` and spent a
milestone attempt on 2026-08-12.

`USAGE_LIMIT` is not here on purpose, though it is equally not the milestone's
fault. Staffing already refuses a usage-limited harness for five hours, so a
milestone that did not spend an attempt on it would retry into the same refusal;
that one wants the bench to answer first, and is its own change.
"""


def _failure_class_for_outcome(outcome: str) -> FailureClass:
    """Whether this dispatch outcome charges the milestone one of its attempts.

    Every settled failure used to be `CORRECTABLE`, which is the class that
    spends a try. That was right for the case it was written for - an agent that
    ran and got it wrong - and wrong for every failure where the agent never got
    to run, because the milestone was then billed for the provider's outage.
    """

    return FailureClass.TRANSIENT if outcome in _NO_FAULT_OUTCOMES else FailureClass.CORRECTABLE


def _agent_run_result(result_text: str) -> Mapping[str, Any] | None:
    """The agent's own run report inside a settled intent's result, if there is one.

    The dispatcher writes `dispatch_runner_result.v1` as the intent's result, and
    that payload carries the agent's output inline: the files it changed, the
    verification it ran, and what it reported. So the evidence is already on the
    row, and reading it needs no second query and no association table.

    ``None`` means this result is not that payload, which is a real case rather
    than a corrupt one: an operator may complete an intent by hand with a plain
    sentence. The caller must not treat that as evidence.
    """

    if not result_text:
        return None
    try:
        payload = json.loads(result_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    if payload.get("schema_version") != "dispatch_runner_result.v1":
        return None
    run_result = payload.get("run_result")
    return run_result if isinstance(run_result, Mapping) else None


def _agent_evidence(kind: ArtifactKind, run_result: Mapping[str, Any]) -> str | None:
    """What in the agent's output proves `kind`, or None if it proved nothing.

    ``None`` is the load-bearing answer. The previous version of this had no way
    to say it: every required type got the settled result string as its body, so
    a run that changed no files still produced a `source_patch`, and the evidence
    gate could not fail. Distinguishing "produced this" from "produced nothing of
    this kind" is the whole point, and the caller fails the milestone on a None
    rather than inventing a body for it.

    Total over `ArtifactKind` by construction: the final case covers the kinds
    whose evidence is the agent's written output. A new kind needing a different
    source has to be added above it deliberately.
    """

    summary = str(run_result.get("output_summary") or "").strip()
    changed = [str(item) for item in run_result.get("changed_files") or ()]
    commands = [str(item) for item in run_result.get("verification_commands") or ()]
    output = [str(item) for item in run_result.get("verification_output") or ()]

    match kind:
        case ArtifactKind.SOURCE_PATCH:
            # Files, not prose. An agent that reports success having touched
            # nothing has not patched anything, and that is the single most
            # valuable thing this function can refuse to vouch for.
            if not changed:
                return None
            return "changed files:\n" + "\n".join(sorted(changed))
        case ArtifactKind.TEST_RESULT | ArtifactKind.ACCEPTANCE_REPORT:
            if not commands and not output:
                return None
            return "commands:\n" + "\n".join(commands) + "\n\noutput:\n" + "\n".join(output)
        case ArtifactKind.OPERATOR_APPROVAL:
            # Never derivable from a run. An approval is an operator's decision,
            # recorded by the engine when a REVIEW_OPERATOR milestone is approved;
            # a dispatch that could produce one would be approving itself.
            return None
        case _:
            return summary or None


@dataclass(frozen=True)
class SimulatedExecutorRuntime:
    """A deterministic runtime that produces the evidence a milestone requires.

    Explicitly named as a simulation so nobody mistakes it for real execution. It
    exists for two honest uses: proving the lifecycle end to end without invoking
    models, and letting an operator dry-run a compiled plan. The failure set makes
    negative paths reproducible.
    """

    failing_milestones: frozenset[str] = frozenset()
    failure_class: FailureClass = FailureClass.NONRECOVERABLE
    delay_seconds: float = 0.0
    started: list[str] = field(default_factory=list)

    def run(self, context: MilestoneContext) -> MilestoneOutcome:
        self.started.append(context.milestone.stable_key)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        if context.milestone.stable_key in self.failing_milestones:
            return MilestoneFailed(
                failure_class=self.failure_class,
                failure_code="simulated_failure",
                failure_summary=(
                    f"simulated runtime failed milestone {context.milestone.stable_key}"
                ),
            )
        declaration = EXECUTOR_REGISTRY[context.executor_kind]
        artifacts = tuple(
            evidence_artifact(
                context,
                RequirableArtifact(ArtifactKind(artifact_type)),
                content=(
                    f"{artifact_type} for {context.milestone.stable_key} "
                    f"attempt {context.attempt} under plan {context.compiled_plan_hash}"
                ),
                step_name=f"simulate:{declaration.kind.value}",
                metadata={"acceptance_criteria": list(context.milestone.acceptance_criteria)},
            )
            for artifact_type in context.milestone.required_artifacts
        )
        return MilestoneSucceeded(
            result_summary=(
                f"{declaration.kind.value} completed {context.milestone.stable_key} "
                f"with {len(artifacts)} artifact(s)"
            ),
            artifacts=artifacts,
        )


class DispatchWaitTimeout(RuntimeError):
    """A dispatch intent did not settle inside its bounded wait.

    Raised rather than reported as a milestone failure, because the work may still
    be running: the caller decides whether to block the milestone and let the
    supervised drainer settle it, which is different from deciding it failed.

    It means only what it says now. It used to be raised for a *parked* intent as
    well, which made "the agent never answered" and "the agent stopped and is
    waiting for a person" indistinguishable at the one place that reported them.
    """

    def __init__(self, intent_id: str, waited_seconds: float) -> None:
        super().__init__(
            f"dispatch intent {intent_id!r} did not settle within {waited_seconds:.0f}s"
        )
        self.intent_id = intent_id


@dataclass(frozen=True)
class DispatchSettled:
    """The intent reached a terminal status; the row is the answer."""

    intent_id: str
    status: DispatchIntentStatus
    row: dict[str, Any]


@dataclass(frozen=True)
class DispatchParked:
    """The intent stopped on purpose and is waiting on a decision.

    Carries the checkpoint that parked it when there is one, because the whole
    point of distinguishing this from a timeout is telling an operator what to go
    and look at.
    """

    intent_id: str
    status: DispatchIntentStatus
    checkpoint_id: str | None = None

    def describe(self) -> str:
        checkpoint = f" at checkpoint {self.checkpoint_id}" if self.checkpoint_id else ""
        return (
            f"dispatch intent {self.intent_id!r} is {self.status.value}{checkpoint} "
            "and will not move again without a decision"
        )


@dataclass(frozen=True)
class DispatchStillActive:
    """The wait ended and the intent had not stopped moving.

    This is the only shape that is honestly a timeout: somebody is or will be
    working on it, and we ran out of patience rather than out of work.
    """

    intent_id: str
    status: DispatchIntentStatus | None
    waited_seconds: float

    def describe(self) -> str:
        status = self.status.value if self.status is not None else "absent from the ledger"
        return (
            f"dispatch intent {self.intent_id!r} was still {status} after "
            f"{self.waited_seconds:.0f}s"
        )


# What one bounded wait on a dispatch intent can have found. Three variants and
# not a nullable row, because "settled", "parked", and "still running" need three
# different responses from the milestone and used to get one.
type DispatchWaitResult = DispatchSettled | DispatchParked | DispatchStillActive


def classify_dispatch_intent(
    intent_id: str,
    row: dict[str, Any] | None,
    *,
    waited_seconds: float,
) -> DispatchWaitResult:
    """Read one intent row as an answer to "may this milestone stop waiting?".

    A missing row is ``DispatchStillActive`` with no status rather than a
    settlement: an intent nothing can find has not been shown to have ended, and
    inventing an outcome for it is how a milestone reports work it never saw.
    """

    if row is None:
        return DispatchStillActive(intent_id=intent_id, status=None, waited_seconds=waited_seconds)
    status = DispatchIntentStatus(str(row["status"]))
    match classify_dispatch_progress(status):
        case DispatchProgress.SETTLED:
            return DispatchSettled(intent_id=intent_id, status=status, row=row)
        case DispatchProgress.PARKED:
            checkpoint = row.get("checkpoint_id")
            return DispatchParked(
                intent_id=intent_id,
                status=status,
                checkpoint_id=str(checkpoint) if checkpoint else None,
            )
        case DispatchProgress.ACTIVE:
            return DispatchStillActive(
                intent_id=intent_id, status=status, waited_seconds=waited_seconds
            )


class DispatchParkedError(RuntimeError):
    """A wait ended because its intent parked, not because it settled.

    A sibling of ``DispatchWaitTimeout`` rather than a reuse of it. The two need
    opposite responses - a timeout may still have work running under it, a park
    is waiting on a person - and one name for both is what made a paused intent
    report `dispatch_wait_elapsed` after sleeping out its full bound.
    """

    def __init__(self, parked: DispatchParked) -> None:
        super().__init__(parked.describe())
        self.parked = parked
        self.intent_id = parked.intent_id


# Read from the ledger's own vocabulary rather than restated as strings. The
# restated set had already drifted: it shared a name with a set in dispatch.py
# that deliberately excludes SUPERSEDED, so two modules disagreed about what
# "settled" meant while spelling it identically.
_DISPATCH_SUCCESS = DispatchIntentStatus.DONE


def dispatch_intent_row(intent_id: str) -> dict[str, Any] | None:
    with tx() as c:
        row = c.execute(
            "SELECT * FROM dispatch_intents WHERE intent_id=?",
            (intent_id,),
        ).fetchone()
    return rowdict(row) if row is not None else None


@dataclass(frozen=True)
class DispatchBackedExecutorRuntime:
    """Execute a milestone by requesting agent work in the dispatch ledger.

    The intent is created as the persisted effect of a legal milestone transition,
    and the supervised drainer runs it. This runtime only waits for the intent to
    settle and translates the settled row into a milestone outcome; it never
    decides a lifecycle transition itself.
    """

    tier: str = "senior"
    kind: str = "code"
    poll_interval_seconds: float = 2.0
    wait_seconds: float = 3600.0
    intent_submitter: Any = None
    # An override for a runtime constructed directly. The plan is the source of
    # truth now and arrives on the context; this only wins when something built
    # this runtime for a specific project on purpose.
    target_project_id: str | None = None
    fact_recorder: Any = None

    def dispatch_source(self, context: MilestoneContext) -> str:
        return (
            f"work_unit:{context.work_unit_id}:milestone_execution:{context.milestone.stable_key}"
        )

    def idempotency_key(self, context: MilestoneContext) -> str:
        """The identity of the work, not of the call that asked for it.

        The attempt is in the key and is the whole reason this is not
        ``dispatch_source``. A retry after a failure is a *different* request and
        must get its own intent; a re-execution of the same attempt after a crash
        is the *same* request and must not. Those two differ only by attempt, so
        a key without it would collapse every retry of a milestone into the first
        intent and the milestone could never be retried at all.

        These are the same three facts `_milestone_workflow_id` uses to name a
        DBOS workflow, deliberately: the durable identity of one milestone
        attempt should not depend on which subsystem is asking for it.
        """

        return (
            f"work_unit:{context.work_unit_id}"
            f":milestone:{context.milestone.stable_key}"
            f":attempt:{context.attempt}"
        )

    def submit(self, context: MilestoneContext) -> str:
        from ..coordination.dispatch import submit_dispatch_intent
        from ..spawn_authority import SpawnAuthority

        submitter = self.intent_submitter or submit_dispatch_intent
        result = submitter(
            self.tier,
            self._prompt(context),
            self.kind,
            self._target_project_id(context),
            self.dispatch_source(context),
            idempotency_key=self.idempotency_key(context),
            notify_workflow_id=context.child_workflow_id,
            # The compiled plan is the authority. The executor declaration has
            # already been intersected with the plan-level permission envelope,
            # and parsing the names here refuses an unknown persisted capability.
            permitted_capabilities=SpawnAuthority.from_names(context.permitted_tools).to_names(),
        )
        if not result.get("ok"):
            raise RuntimeError(f"dispatch intent submission rejected: {result}")
        intent_id = str(result["intent_id"])
        self._record_intent(context, intent_id)
        return intent_id

    def _target_project_id(self, context: MilestoneContext) -> str | None:
        """Which project this milestone's agent work is about.

        Explicit override, then what the plan declares, then a default only for
        `code`. The last step is the distinction that matters: a plan naming a
        project is stating a fact, and advisory work should honour it, because a
        read-only diagnosis is about a repository too. Reaching for the
        project-center default when nothing named one is *inventing* a project,
        and only `code` justifies that, because the drainer refuses a code intent
        without one. Advisory work gets `None` rather than a guess.
        """

        if self.target_project_id is not None:
            return self.target_project_id
        if context.target_project_id:
            return context.target_project_id
        if self.kind != "code":
            return None
        from ..project_center import load_project_center

        return load_project_center().default_saga_project

    def _record_intent(self, context: MilestoneContext, intent_id: str) -> None:
        """Link the milestone to the agent work it asked for, before waiting.

        `DispatchIntentCreated` was defined and handled and never emitted, so
        nothing durable connected a milestone to its intent: a crash between
        submission and completion left an orphaned intent no resume could find.
        Recording the fact first is what makes the link survive the crash.
        """

        from . import repository as repo

        recorder = self.fact_recorder or repo.record_fact
        recorder(
            context.work_unit_id,
            DispatchIntentCreated(
                phase=context.milestone.phase,
                milestone_key=context.milestone.stable_key,
                attempt=context.attempt,
                dispatch_intent_id=intent_id,
                tier=self.tier,
                kind=self.kind,
            ),
        )

    def _prompt(self, context: MilestoneContext) -> str:
        """The whole instruction an agent gets for one milestone.

        The document's own context leads, because an agent that knows only its
        milestone title will satisfy the title. The milestone's own fields follow
        and are last before the tool list, so the specific instruction is what the
        model reads most recently.
        """

        criteria = "\n".join(f"- {item}" for item in context.milestone.acceptance_criteria)
        artifacts = "\n".join(f"- {item}" for item in context.milestone.required_artifacts)
        preamble = f"{context.design_doc_excerpt}\n\n" if context.design_doc_excerpt else ""
        return (
            f"{preamble}"
            f"Milestone {context.milestone.stable_key} ({context.milestone.phase.value}): "
            f"{context.milestone.title}\n\n"
            f"{context.milestone.description}\n\n"
            f"Acceptance criteria:\n{criteria}\n\n"
            f"Required evidence:\n{artifacts}\n\n"
            f"Permitted tools: {', '.join(context.permitted_tools)}\n"
            f"Compiled plan hash: {context.compiled_plan_hash}\n"
        )

    def poll_until_stopped(
        self, intent_id: str, timeout_seconds: float | None = None
    ) -> DispatchWaitResult:
        """Poll one intent until it stops moving, bounded by the plan when it says.

        `wait_seconds` is the fallback for a caller with no milestone in hand. The
        compiled plan carries a per-milestone `timeout_seconds` that the executor
        registry declared - 900 for a clarification, 1800 for planning - and
        ignoring it meant every milestone waited the same flat hour whatever its
        plan said. A plan that states a bound and does not get it is worse than
        one that states nothing.

        It reads before it sleeps, so an intent that is already over costs no
        wait at all, and it ends on a parked intent as well as a settled one. It
        used to end only on terminal, so a paused intent slept out the entire
        bound and was then reported as a timeout.
        """

        bound = self.wait_seconds if timeout_seconds is None else float(timeout_seconds)
        started = time.monotonic()
        deadline = started + bound
        while True:
            result = classify_dispatch_intent(
                intent_id,
                dispatch_intent_row(intent_id),
                waited_seconds=time.monotonic() - started,
            )
            if not isinstance(result, DispatchStillActive):
                return result
            if time.monotonic() >= deadline:
                return result
            time.sleep(self.poll_interval_seconds)

    def wait_for(self, intent_id: str, timeout_seconds: float | None = None) -> dict[str, Any]:
        """The settled row, or the reason there is not one.

        Kept as the row-returning shape for the callers that want an outcome and
        nothing else. Everything that needs to tell a parked intent from an
        expired wait calls ``poll_until_stopped`` and matches the result.
        """

        result = self.poll_until_stopped(intent_id, timeout_seconds)
        bound = self.wait_seconds if timeout_seconds is None else float(timeout_seconds)
        match result:
            case DispatchSettled():
                return result.row
            case DispatchParked():
                raise DispatchParkedError(result)
            case DispatchStillActive():
                raise DispatchWaitTimeout(intent_id, bound)

    def start(self, context: MilestoneContext) -> MilestoneStart:
        """Submit the work and hand back what to wait on.

        This is the whole side effect. Everything after it is a translation of a
        row, which is why the wait no longer has to happen inside the same step.
        """

        intent_id = self.submit(context)
        bound = (
            self.wait_seconds
            if context.milestone.timeout_seconds is None
            else float(context.milestone.timeout_seconds)
        )
        return MilestoneAwaitingDispatch(dispatch_intent_id=intent_id, timeout_seconds=bound)

    def settle(
        self, context: MilestoneContext, awaiting: MilestoneAwaitingDispatch
    ) -> MilestoneOutcome:
        """Translate the settled intent row into a milestone outcome.

        Reads the row rather than trusting the notification's payload. A wake is
        a hint that something changed; the ledger is what changed. Trusting the
        message would make the outcome depend on who sent it.
        """

        result = classify_dispatch_intent(
            awaiting.dispatch_intent_id,
            dispatch_intent_row(awaiting.dispatch_intent_id),
            waited_seconds=awaiting.timeout_seconds,
        )
        match result:
            case DispatchSettled():
                return self._outcome_from_settled_row(
                    context, awaiting.dispatch_intent_id, result.row
                )
            case DispatchParked():
                raise DispatchParkedError(result)
            case DispatchStillActive():
                raise DispatchWaitTimeout(awaiting.dispatch_intent_id, awaiting.timeout_seconds)

    def run(self, context: MilestoneContext) -> MilestoneOutcome:
        """Start, poll, translate: the composition for a caller with no wake channel.

        The poll is not the preferred path and is not what a DBOS workflow uses.
        It remains the honest answer without one, because a process that cannot
        be notified has nothing to wait on but the clock.

        It translates the row ``wait_for`` returned rather than calling
        ``settle``, which would re-read it. ``settle`` re-reads because a
        notification is only a hint that something changed; a poll is itself a
        read of the ledger and has the answer in hand already.
        """

        started = self.start(context)
        if not isinstance(started, MilestoneAwaitingDispatch):
            return started
        settled = self.wait_for(started.dispatch_intent_id, context.milestone.timeout_seconds)
        return self._outcome_from_settled_row(context, started.dispatch_intent_id, settled)

    def _outcome_from_settled_row(
        self, context: MilestoneContext, intent_id: str, settled: dict[str, Any]
    ) -> MilestoneOutcome:
        status = DispatchIntentStatus(str(settled["status"]))
        result_text = str(settled.get("result") or "")
        if status != _DISPATCH_SUCCESS:
            outcome = str(settled.get("outcome") or "DISPATCH_FAILED")
            return MilestoneFailed(
                failure_class=_failure_class_for_outcome(outcome),
                failure_code=outcome,
                failure_summary=str(
                    settled.get("error") or f"dispatch intent {intent_id} {status}"
                ),
                artifacts=_dispatch_failure_evidence(context, intent_id, status, result_text),
            )

        run_result = _agent_run_result(result_text)
        if run_result is None:
            # A DONE intent whose result is not a `dispatch_runner_result.v1`
            # payload was settled by something other than the runner, most often
            # an operator completing it by hand. It may well be finished; what it
            # is not is *checkable*, and a milestone that cannot check its
            # evidence must not report that it verified any.
            return MilestoneFailed(
                failure_class=FailureClass.CORRECTABLE,
                failure_code="unverifiable_dispatch_result",
                failure_summary=(
                    f"dispatch intent {intent_id} completed with a result carrying no "
                    "dispatch_runner_result.v1 payload, so the evidence it claims cannot "
                    "be read"
                ),
            )

        artifacts: list[ArtifactRecord] = []
        missing: list[str] = []
        for artifact_type in context.milestone.required_artifacts:
            kind = ArtifactKind(artifact_type)
            content = _agent_evidence(kind, run_result)
            if content is None:
                missing.append(artifact_type)
                continue
            artifacts.append(
                evidence_artifact(
                    context,
                    RequirableArtifact(kind),
                    content=content,
                    step_name=f"dispatch:{intent_id}",
                    metadata={"dispatch_intent_id": intent_id},
                )
            )
        if missing:
            return MilestoneFailed(
                failure_class=FailureClass.CORRECTABLE,
                failure_code="missing_required_artifacts",
                failure_summary=(
                    f"dispatch intent {intent_id} completed without producing " + ", ".join(missing)
                ),
            )
        return MilestoneSucceeded(
            result_summary=f"dispatch intent {intent_id} completed",
            artifacts=tuple(artifacts),
        )


@dataclass(frozen=True)
class CompositeExecutorRuntime:
    """Route each executor kind to the runtime that owns it.

    The default is explicit rather than implicit: an unrouted kind raises instead
    of quietly falling back to a simulation that would report success.
    """

    routes: dict[ExecutorKind, MilestoneExecutorRuntime]

    def _route(self, context: MilestoneContext) -> MilestoneExecutorRuntime:
        runtime = self.routes.get(context.executor_kind)
        if runtime is None:
            raise RuntimeError(
                f"no runtime is registered for executor {context.executor_kind.value!r}"
            )
        return runtime

    def run(self, context: MilestoneContext) -> MilestoneOutcome:
        return self._route(context).run(context)

    def start(self, context: MilestoneContext) -> MilestoneStart:
        """Defer where the routed runtime can, finish where it cannot.

        A composite may route some kinds to a deferrable runtime and others to a
        synchronous one, so it answers for whichever it reached. A synchronous
        runtime's outcome is already a valid `MilestoneStart`, which is what lets
        the two live behind one router without the caller knowing which it got.
        """

        runtime = self._route(context)
        if isinstance(runtime, DeferrableMilestoneRuntime):
            return runtime.start(context)
        return runtime.run(context)

    def settle(
        self, context: MilestoneContext, awaiting: MilestoneAwaitingDispatch
    ) -> MilestoneOutcome:
        """Routed by the same key as ``start``, so a settle reaches the runtime
        that submitted. The executor kind comes from the plan, which is frozen
        for the life of the attempt, so the two cannot resolve differently.
        """

        runtime = self._route(context)
        if not isinstance(runtime, DeferrableMilestoneRuntime):
            raise RuntimeError(
                f"executor {context.executor_kind.value!r} routes to a runtime that "
                "cannot settle; it never returned anything to wait for"
            )
        return runtime.settle(context, awaiting)


@dataclass(frozen=True)
class DeliveryRecordRuntime:
    """Close a reviewed WorkUnit by recording what its durable evidence says.

    Delivery is bookkeeping, not another model judgment.  The agent work already
    ran and the review phase already decided whether it was acceptable.  Asking a
    fresh agent to paraphrase those facts made terminal success depend on one more
    unverifiable model response, so this runtime composes the ledger evidence
    deterministically instead.
    """

    def run(self, context: MilestoneContext) -> MilestoneOutcome:
        from . import repository as repo

        owed = frozenset(context.milestone.required_artifacts)
        prior = repo.list_work_unit_artifacts(context.work_unit_id)
        # What the work already produced, minus what this milestone is about to
        # write. The record is a statement about the work, so listing itself in
        # it would be circular.
        delivered = tuple(
            sorted(
                {item.artifact_type.value for item in prior if item.artifact_type.value not in owed}
            )
        )
        payload = {
            "schema_version": "delivery_record.v1",
            "work_unit_id": context.work_unit_id,
            "compiled_plan_hash": context.compiled_plan_hash,
            "delivered_artifact_types": list(delivered),
            "not_covered": list(context.document_context.non_goals),
        }
        artifacts = tuple(
            json_evidence_artifact(
                context,
                RequirableArtifact(ArtifactKind(artifact_type)),
                payload,
                step_name="record_delivery",
            )
            for artifact_type in context.milestone.required_artifacts
        )
        return MilestoneSucceeded(
            result_summary="recorded the reviewed WorkUnit delivery",
            artifacts=artifacts,
        )


def dispatch_backed_runtime() -> CompositeExecutorRuntime:
    """The production routing: agent work goes to the dispatch ledger.

    ``review.operator`` is absent by design. Its outcome is an operator decision
    the engine records; there is no agent execution to route.
    """

    agent_runtime = DispatchBackedExecutorRuntime()
    advisory_runtime = DispatchBackedExecutorRuntime(kind="advisory")
    delivery_runtime = DeliveryRecordRuntime()
    return CompositeExecutorRuntime(
        routes={
            ExecutorKind.CLARIFY_REQUIREMENTS: advisory_runtime,
            ExecutorKind.VALIDATE_REPOSITORY: advisory_runtime,
            ExecutorKind.PLAN_IMPLEMENTATION: advisory_runtime,
            ExecutorKind.IMPLEMENT_CODE_CHANGE: agent_runtime,
            ExecutorKind.VERIFY_TESTS: agent_runtime,
            ExecutorKind.VERIFY_ACCEPTANCE: agent_runtime,
            ExecutorKind.REVIEW_AGENT: advisory_runtime,
            ExecutorKind.DELIVER_ARTIFACT: delivery_runtime,
            ExecutorKind.DELIVER_DEPLOYMENT: agent_runtime,
        }
    )


__all__ = [
    "CompositeExecutorRuntime",
    "DeferrableMilestoneRuntime",
    "DispatchBackedExecutorRuntime",
    "DispatchWaitTimeout",
    "MilestoneAwaitingDispatch",
    "MilestoneContext",
    "MilestoneExecutorRuntime",
    "MilestoneFailed",
    "MilestoneOutcome",
    "MilestoneStart",
    "MilestoneSucceeded",
    "SimulatedExecutorRuntime",
    "dispatch_backed_runtime",
    "evidence_artifact",
    "json_evidence_artifact",
    "dispatch_intent_row",
]
