# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The production milestone runtime's contract with the dispatch ledger.

Nothing here needs DBOS or a real agent. These are the assumptions the runtime
makes about the far end of the ledger, and each one was wrong in a way no test
could see, because every green WorkUnit trace ran through the simulated runtime
instead and never submitted an intent at all.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from work_unit_support import compile_acceptance_doc

from local_first_agent_os.contracts import (
    TERMINAL_DISPATCH_INTENT_STATUSES,
    DispatchIntentStatus,
)
from local_first_agent_os.coordination import DispatchKind
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.events import (
    DiagnosticArtifact,
    DiagnosticArtifactKind,
    DispatchIntentCreated,
)
from local_first_agent_os.work_units.execution import (
    DispatchBackedExecutorRuntime,
    MilestoneAwaitingIntegration,
    MilestoneContext,
)


def _context() -> MilestoneContext:
    """A real compiled milestone, so the runtime sees what production sends it."""

    compiled = compile_acceptance_doc(design_doc_id="dispatch_runtime")
    assert compiled.compiled_plan_revision_id is not None
    plan = repo.get_compiled_plan_revision(compiled.compiled_plan_revision_id).plan
    milestone = plan.ordered_milestones()[0]
    return MilestoneContext(
        work_unit_id="wu-1",
        root_workflow_id="work-unit:wu-1",
        child_workflow_id=f"work-unit:wu-1:milestone:{milestone.stable_key}:1",
        milestone=milestone,
        attempt=1,
        design_doc_revision_id="ddr-1",
        compiled_plan_hash=plan.plan_hash(),
    )


class _Submitter:
    """Records the arguments the runtime actually sends.

    Keywords are captured as well as positionals. `intent_submitter` is typed
    `Any`, so nothing makes a substitute match `submit_dispatch_intent`; when the
    real function grew `idempotency_key` this double kept accepting only
    positionals and five tests failed with `TypeError` rather than telling
    anyone what had changed. Recording keywords is also what lets a test assert
    the key was sent at all.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.keywords: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(args)
        self.keywords.append(kwargs)
        return {"ok": True, "intent_id": "intent-1"}


def test_a_code_intent_carries_a_target_project() -> None:
    """The drainer refuses a code intent without one, so submitting None was fatal.

    `dispatcher_runner` raises `code dispatch intent ... requires target_project_id`
    before it plans anything, so every WorkUnit code milestone would have died at
    the far end of the ledger on the first real run.
    """

    submitter = _Submitter()
    runtime = DispatchBackedExecutorRuntime(
        intent_submitter=submitter,
        target_project_id="ai_business_portfolio",
        fact_recorder=lambda *_: None,
    )

    runtime.submit(_context())

    (tier, _prompt, kind, target_project_id, source) = submitter.calls[0]
    assert (tier, kind) == ("senior", "code")
    assert target_project_id == "ai_business_portfolio", "the drainer requires this"
    assert source.startswith("work_unit:wu-1:")
    # Without this the intent is identified by when it was submitted, and a
    # crash mid-wait re-submits it as a second agent doing the same milestone.
    assert submitter.keywords[0]["idempotency_key"] == runtime.idempotency_key(_context())


def test_an_advisory_intent_needs_no_target_project() -> None:
    """Only `code` is refused, so advisory work must not invent a project."""

    submitter = _Submitter()
    runtime = DispatchBackedExecutorRuntime(
        kind=DispatchKind.ADVISORY,
        intent_submitter=submitter,
        fact_recorder=lambda *_: None,
    )

    runtime.submit(_context())

    assert submitter.calls[0][3] is None


def test_an_advisory_intent_carries_a_target_the_plan_declared() -> None:
    """Honouring a declared project is not the same as inventing one.

    The test above guards against reaching for a default when nothing named a
    project. Once the plan names one, advisory work is about that repository and
    should say so: a read-only diagnosis of the wrong checkout is worthless.
    Both properties have to hold at once, which is why the distinguishing input
    is whether the context carries a target rather than what `kind` is.
    """

    submitter = _Submitter()
    runtime = DispatchBackedExecutorRuntime(
        kind=DispatchKind.ADVISORY,
        intent_submitter=submitter,
        fact_recorder=lambda *_: None,
    )
    context = replace(_context(), target_project_id="ai_business_portfolio")

    runtime.submit(context)

    assert submitter.calls[0][3] == "ai_business_portfolio"


def test_submission_records_the_link_before_the_wait() -> None:
    """`DispatchIntentCreated` was defined, handled, and never emitted.

    Without it nothing durable connects a milestone to the agent work it asked
    for, so a crash between submission and completion leaves an intent no resume
    can find. The fact has to be recorded before the wait, not after it.
    """

    recorded: list[tuple[str, Any]] = []
    runtime = DispatchBackedExecutorRuntime(
        intent_submitter=_Submitter(),
        target_project_id="proj",
        fact_recorder=lambda work_unit_id, fact: recorded.append((work_unit_id, fact)),
    )

    runtime.submit(_context())

    assert len(recorded) == 1
    work_unit_id, fact = recorded[0]
    assert work_unit_id == "wu-1"
    assert fact == DispatchIntentCreated(
        phase=_context().milestone.phase,
        milestone_key=_context().milestone.stable_key,
        attempt=1,
        dispatch_intent_id="intent-1",
        tier="senior",
        kind=DispatchKind.CODE,
    )


def test_the_waiter_and_the_quorum_ask_different_questions() -> None:
    """Two sets that once shared the name `_DISPATCH_SETTLED` and disagreed.

    A waiter wants "has this intent stopped moving", which includes SUPERSEDED.
    A quorum parent wants "can I settle on this child", which does not: a
    superseded child is not an answer, and the parent waits for its replacement.
    The runtime had restated the first set as string literals and named it after
    the second.
    """

    from local_first_agent_os.coordination.dispatch import _QUORUM_SETTLING_STATUSES

    assert DispatchIntentStatus.SUPERSEDED in TERMINAL_DISPATCH_INTENT_STATUSES
    assert DispatchIntentStatus.SUPERSEDED not in _QUORUM_SETTLING_STATUSES
    # Neither set may contain a status an intent can still move out of.
    live = {
        DispatchIntentStatus.PENDING,
        DispatchIntentStatus.CLAIMED,
        DispatchIntentStatus.IN_PROGRESS,
        DispatchIntentStatus.CHECKPOINT_REVIEW,
        DispatchIntentStatus.PAUSED,
    }
    assert not (TERMINAL_DISPATCH_INTENT_STATUSES & live)
    assert not (set(_QUORUM_SETTLING_STATUSES) & live)


def test_a_swept_intent_ends_the_wait_as_a_failure() -> None:
    """The GC interaction, asserted rather than assumed.

    `gc_ledger` flips a stale CLAIMED intent with no live lease to CANCELED with
    outcome ORPHANED_CLAIM_EXPIRED. That is terminal, so the waiter stops and the
    milestone fails with the sweep's own reason instead of polling for an hour.
    """

    from local_first_agent_os.work_units.execution import MilestoneFailed

    settled = {
        "status": DispatchIntentStatus.CANCELED.value,
        "outcome": "ORPHANED_CLAIM_EXPIRED",
        "error": "claim expired",
        "result": None,
    }
    runtime = DispatchBackedExecutorRuntime(
        intent_submitter=_Submitter(),
        target_project_id="proj",
        fact_recorder=lambda *_: None,
    )
    runtime_wait = runtime.wait_for
    try:
        object.__setattr__(runtime, "wait_for", lambda _intent_id, _timeout=None: settled)
        outcome = runtime.run(_context())
    finally:
        object.__setattr__(runtime, "wait_for", runtime_wait)

    assert isinstance(outcome, MilestoneFailed)
    assert outcome.failure_code == "ORPHANED_CLAIM_EXPIRED"


def _failed_runner_payload() -> str:
    """The shape a real failed dispatch writes, from the run that prompted this.

    `to_intent_result` builds this payload *before* it branches on the outcome,
    so a FAILED intent carries the same complete report a DONE one does. The run
    this is modelled on wrote 58,104 bytes of it and the milestone reported
    `exit=1`.
    """

    import json

    return json.dumps(
        {
            "schema_version": "dispatch_runner_result.v1",
            "intent_id": "intent-1",
            "run_result": {
                "status": "FAILED",
                "output_summary": "1 task failed",
                "risks": ("1 of 2 tasks failed",),
                "tasks": [
                    {"task_name": "junior_context", "status": "completed", "risks": []},
                    {
                        "task_name": "senior_synthesis",
                        "status": "failed",
                        "risks": [
                            "claude advisory exited 1",
                            "claude reported: Not logged in · Please run /login",
                        ],
                    },
                ],
            },
        }
    )


def _run_with_settled(settled: dict[str, Any]) -> Any:
    runtime = DispatchBackedExecutorRuntime(
        intent_submitter=_Submitter(),
        target_project_id="proj",
        fact_recorder=lambda *_: None,
    )
    runtime_wait = runtime.wait_for
    try:
        object.__setattr__(runtime, "wait_for", lambda _intent_id, _timeout=None: settled)
        return runtime.run(_context())
    finally:
        object.__setattr__(runtime, "wait_for", runtime_wait)


def test_a_reviewed_source_patch_waits_for_its_exact_landing() -> None:
    import json

    context = replace(
        _context(),
        milestone=replace(_context().milestone, required_artifacts=("source_patch",)),
    )
    payload = json.dumps(
        {
            "schema_version": "dispatch_runner_result.v1",
            "promotion_state": "MERGE_PENDING",
            "run_result": {
                "status": "COMPLETED",
                "output_summary": "reviewed patch",
                "changed_files": ["src/example.py"],
            },
        }
    )
    runtime = DispatchBackedExecutorRuntime(
        intent_submitter=_Submitter(),
        target_project_id="proj",
        fact_recorder=lambda *_: None,
    )

    outcome = runtime._outcome_from_settled_row(
        context,
        "intent-1",
        {
            "status": DispatchIntentStatus.DONE.value,
            "outcome": "COMPLETED",
            "error": None,
            "result": payload,
        },
    )

    assert outcome == MilestoneAwaitingIntegration(
        dispatch_intent_id="intent-1",
        timeout_seconds=context.milestone.timeout_seconds,
    )


def test_a_failed_dispatch_keeps_the_evidence_its_payload_already_carried() -> None:
    """The defect: the row held the cause and the reader returned only `error`.

    An operator running `list_work_unit_artifacts` on a failed WorkUnit got an
    empty list, and had to read loop logs to learn the harness was logged out.
    """

    from local_first_agent_os.work_units.execution import MilestoneFailed

    outcome = _run_with_settled(
        {
            "status": DispatchIntentStatus.FAILED.value,
            "outcome": "DISPATCH_FAILED",
            "error": "claude advisory exited 1",
            "result": _failed_runner_payload(),
        }
    )

    assert isinstance(outcome, MilestoneFailed)
    assert len(outcome.artifacts) == 1
    evidence = outcome.artifacts[0]
    assert evidence.artifact_type == DiagnosticArtifact(
        DiagnosticArtifactKind.DISPATCH_FAILURE_EVIDENCE
    )
    assert evidence.satisfies_requirement is False
    assert evidence.metadata["dispatch_intent_id"] == "intent-1"
    assert any("Not logged in" in cause for cause in evidence.metadata["causes"])


def test_the_failure_evidence_is_never_a_requirable_artifact_kind() -> None:
    """It is produced and never required.

    `ArtifactKind` membership is the compiler's satisfiability check, so a kind
    that exists only on failure would be a requirement no successful run could
    ever meet. Adding a member would also rewrite every plan hash ever computed.
    """

    from local_first_agent_os.work_units.events import ArtifactKind
    from local_first_agent_os.work_units.execution import _DISPATCH_FAILURE_EVIDENCE_TYPE

    assert _DISPATCH_FAILURE_EVIDENCE_TYPE not in {kind.value for kind in ArtifactKind}


def test_a_crashed_runner_mints_no_evidence_it_never_captured() -> None:
    """`result=None` is the dispatcher's crash path; nothing was captured.

    An artifact asserting evidence that never existed is the same fabrication as
    a `source_patch` for a run that changed nothing, pointed the other way.
    """

    from local_first_agent_os.work_units.execution import MilestoneFailed

    outcome = _run_with_settled(
        {
            "status": DispatchIntentStatus.FAILED.value,
            "outcome": "DISPATCH_FAILED",
            "error": "RuntimeError: boom",
            "result": None,
        }
    )

    assert isinstance(outcome, MilestoneFailed)
    assert outcome.artifacts == ()


def test_the_nearest_cause_is_ordered_before_the_dispatch_summary() -> None:
    """ "1 of 2 tasks failed" is not something an operator can act on."""

    outcome = _run_with_settled(
        {
            "status": DispatchIntentStatus.FAILED.value,
            "outcome": "DISPATCH_FAILED",
            "error": "claude advisory exited 1",
            "result": _failed_runner_payload(),
        }
    )

    causes = outcome.artifacts[0].metadata["causes"]
    assert causes[0].startswith("senior_synthesis:")
    assert causes[-1] == "1 of 2 tasks failed"


def test_a_crashed_runner_now_writes_a_traceback_a_reader_can_open() -> None:
    """The one case where the write side really did drop context.

    `result=None` left one exception line in `error` as the only trace of a
    defect in our own code. The payload uses the schema every reader already
    parses, so the traceback arrives somewhere something knows to look.
    """

    import json

    from local_first_agent_os.dispatcher import _runner_crash_payload

    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        payload = json.loads(_runner_crash_payload("intent-1", exc))

    assert payload["schema_version"] == "dispatch_runner_result.v1"
    assert payload["result_origin"] == "runner_crash"
    assert "RuntimeError: boom" in payload["run_result"]["output_summary"]
    assert "Traceback" in payload["run_result"]["traceback"]


def test_a_crashed_runners_payload_reaches_the_milestone_as_evidence() -> None:
    """The two halves meet: the crash writes a payload, the reader opens it."""

    import json

    from local_first_agent_os.dispatcher import _runner_crash_payload
    from local_first_agent_os.work_units.execution import MilestoneFailed

    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        payload = _runner_crash_payload("intent-1", exc)

    outcome = _run_with_settled(
        {
            "status": DispatchIntentStatus.FAILED.value,
            "outcome": "DISPATCH_FAILED",
            "error": "RuntimeError: boom",
            "result": payload,
        }
    )

    assert isinstance(outcome, MilestoneFailed)
    assert len(outcome.artifacts) == 1
    causes = outcome.artifacts[0].metadata["causes"]
    assert any("RuntimeError: boom" in cause for cause in causes)
    assert json.loads(payload)["run_result"]["traceback"]


def test_diagnostic_kinds_are_disjoint_from_requirable_ones() -> None:
    """Closed sets, and nothing may sit in more than one.

    A kind in both would be requirable by a document and emitted only on
    failure, which is the unsatisfiable contract the split exists to prevent.
    The same argument covers traces: `parse_artifact_type` tries the sets in
    order, so an overlapping value would resolve to whichever is tried first and
    the loser would become unreachable rather than ambiguous.
    """

    from local_first_agent_os.work_units.events import (
        ArtifactKind,
        DiagnosticArtifactKind,
        TraceArtifactKind,
    )

    requirable = {kind.value for kind in ArtifactKind}
    diagnostic = {kind.value for kind in DiagnosticArtifactKind}
    trace = {kind.value for kind in TraceArtifactKind}
    assert requirable.isdisjoint(diagnostic)
    assert requirable.isdisjoint(trace)
    assert diagnostic.isdisjoint(trace)
