# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The landed commit settles its milestone, and nothing else does.

docs/landed_commit_settles_its_milestone_gawd.md names the gap: the promotion
chain ended at MERGED and an operator noticed by hand. These pin the settle
path: the landing transaction writes the durable signal, the consumer
completes the milestone with both shas in evidence, settling twice writes
once, no milestone completes without an Integrated row naming its intent, and
a stale plan refuses with the cause while the row remains history.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from refinery_support import build_target_repository, code_merge_payload, write_registry_config
from work_unit_support import compile_acceptance_doc

from local_first_agent_os.coordination.approvals import (
    resolve_approval_request,
    submit_approval_request,
)
from local_first_agent_os.coordination.dispatch import submit_dispatch_intent
from local_first_agent_os.coordination.execution import list_ledger_events
from local_first_agent_os.coordination.integration_queue import (
    INTEGRATION_LANDED_EVENT,
    apply_integration_transition,
    read_integration_requests,
    record_queued_request,
)
from local_first_agent_os.coordination.projects import create_saga
from local_first_agent_os.coordination.store import now, tx
from local_first_agent_os.refinery.requests import (
    InFlight,
    Integrated,
    IntegrationAttemptId,
    IntegrationBatchId,
    IntegrationRequestId,
    IntegrationSubject,
    Queued,
)
from local_first_agent_os.settings import get_settings
from local_first_agent_os.work_units import dispatch_adoption, service
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.events import (
    MilestoneTransition,
    WorkUnitTransition,
)
from local_first_agent_os.work_units.integration_settlement import (
    MilestoneSettled,
    SettlementRefused,
    SettlementSkipped,
    settle_landed_integration,
    settle_landed_integrations,
)
from local_first_agent_os.work_units.lifecycle import (
    FailureClass,
    LifecyclePhase,
    MilestoneExecutionStatus,
    WorkUnitStatus,
)

MILESTONE = "b"
BASE_SHA = "a" * 40
APPROVED_SHA = "b" * 40
MERGE_SHA = "c" * 40


def _blocked_unit(design_doc_id: str) -> str:
    compiled = compile_acceptance_doc(design_doc_id=design_doc_id)
    assert compiled.compiled_plan_revision_id is not None
    started = repo.start_work_unit(compiled.compiled_plan_revision_id, title="settle")
    work_unit_id = started.work_unit.work_unit_id
    repo.mark_enqueue_delivered(work_unit_id)
    for status in (MilestoneExecutionStatus.READY, MilestoneExecutionStatus.RUNNING):
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase.IMPLEMENT,
                milestone_key=MILESTONE,
                status=status,
                attempt=1,
            ),
        )
    repo.record_fact(
        work_unit_id,
        MilestoneTransition(
            phase=LifecyclePhase.IMPLEMENT,
            milestone_key=MILESTONE,
            status=MilestoneExecutionStatus.BLOCKED,
            attempt=1,
            failure_code="merge pending",
            failure_class=FailureClass.TRANSIENT,
        ),
    )
    repo.record_fact(
        work_unit_id,
        WorkUnitTransition(status=WorkUnitStatus.RUNNING, current_phase=LifecyclePhase.IMPLEMENT),
    )
    repo.record_fact(
        work_unit_id,
        WorkUnitTransition(
            status=WorkUnitStatus.BLOCKED,
            current_phase=LifecyclePhase.IMPLEMENT,
            failure_code="implement_phase_blocked",
            failure_summary="phase IMPLEMENT cannot proceed without intervention",
        ),
    )
    return work_unit_id


def _milestone_intent(work_unit_id: str, *, milestone_key: str | None = MILESTONE) -> str:
    source = (
        f"work_unit:{work_unit_id}:milestone_execution:{milestone_key}"
        if milestone_key is not None
        else "saga:standalone"
    )
    submitted = submit_dispatch_intent(
        "senior",
        "implement the milestone",
        kind="code",
        target_project_id="local_first_agent_os",
        source=source,
    )
    return str(submitted["intent_id"])


def _land(
    intent_id: str,
    *,
    request_id: str,
    milestone_key: str | None = MILESTONE,
    integration_sha: str = APPROVED_SHA,
) -> None:
    t = now()
    saga_id = str(create_saga("settle test approvals")["saga_id"])
    approval = submit_approval_request(
        saga_id,
        "CODE_MERGE",
        payload={
            "target_project_id": "local_first_agent_os",
            "branch": "agent/settle-test",
            "base_sha": BASE_SHA,
            "commit_sha": APPROVED_SHA,
            "intent_id": intent_id,
        },
        requested_by="dispatcher_runner",
    )
    subject = IntegrationSubject(
        request_id=IntegrationRequestId(request_id),
        target_project_id="local_first_agent_os",
        branch_name="agent/settle-test",
        base_head_sha=BASE_SHA,
        commit_sha=APPROVED_SHA,
        approval_id=str(approval["approval_id"]),
        intent_id=intent_id,
        pow_wow_id="pw_settle_test",
        milestone_key=milestone_key,
        changed_files=("src/x.py",),
        enqueued_at=t,
    )
    queued = Queued(subject=subject)
    in_flight = InFlight(
        subject=subject,
        batch_id=IntegrationBatchId("batch_settle"),
        attempt_id=IntegrationAttemptId("attempt_settle"),
    )
    integrated = Integrated(
        subject=subject,
        batch_id=IntegrationBatchId("batch_settle"),
        integration_commit_sha=integration_sha,
        integrated_at=t,
    )
    with tx() as c:
        record_queued_request(c, queued, recorded_at=t)
        apply_integration_transition(c, queued, in_flight, recorded_at=t)
        apply_integration_transition(c, in_flight, integrated, recorded_at=t)


def _landed_events(status: str) -> list[dict[str, object]]:
    listed = list_ledger_events(status)
    return [event for event in listed["events"] if event["event_type"] == INTEGRATION_LANDED_EVENT]


def _pending_resume_rows() -> list[repo.EnqueueOutboxRow]:
    return [row for row in repo.list_pending_enqueues(10) if row.kind is repo.EnqueueKind.RESUME]


def test_a_landed_request_completes_its_milestone_without_an_operator(
    work_unit_ledger: Path,
) -> None:
    work_unit_id = _blocked_unit("settle_end_to_end")
    intent_id = _milestone_intent(work_unit_id)
    _land(intent_id, request_id="ir_settle_e2e")
    assert len(_landed_events("PENDING")) == 1

    outcomes = settle_landed_integrations()

    assert len(outcomes) == 1
    settled = outcomes[0]
    assert isinstance(settled, MilestoneSettled)
    assert settled.landing == "fast_forward"
    assert settled.applied is True
    assert settled.resume_enqueued is True
    executions = {item.stable_key: item for item in repo.list_milestone_executions(work_unit_id)}
    assert executions[MILESTONE].status is MilestoneExecutionStatus.SUCCEEDED
    events = repo.list_work_unit_events(work_unit_id, limit=10_000)
    completion = next(
        event
        for event in events
        if event.payload.get("settlement_kind") == "integration_landed.v1"
        and event.payload.get("status") == MilestoneExecutionStatus.SUCCEEDED.value
    )
    assert completion.payload["integration_request_id"] == "ir_settle_e2e"
    assert completion.payload["approved_commit_sha"] == APPROVED_SHA
    assert completion.payload["integration_commit_sha"] == APPROVED_SHA
    assert [row.work_unit_id for row in _pending_resume_rows()] == [work_unit_id]
    assert _landed_events("PENDING") == []
    assert len(_landed_events("PROCESSED")) == 1


def test_a_merge_commit_landing_records_both_shas(work_unit_ledger: Path) -> None:
    work_unit_id = _blocked_unit("settle_merge_commit")
    intent_id = _milestone_intent(work_unit_id)
    _land(intent_id, request_id="ir_settle_merge", integration_sha=MERGE_SHA)

    outcomes = settle_landed_integrations()

    settled = outcomes[0]
    assert isinstance(settled, MilestoneSettled)
    assert settled.landing == "merge_commit"
    assert settled.approved_commit_sha == APPROVED_SHA
    assert settled.integration_commit_sha == MERGE_SHA


def test_settling_twice_writes_once(work_unit_ledger: Path) -> None:
    work_unit_id = _blocked_unit("settle_idempotent")
    intent_id = _milestone_intent(work_unit_id)
    _land(intent_id, request_id="ir_settle_twice")
    first = settle_landed_integrations()
    assert isinstance(first[0], MilestoneSettled)
    events_after_first = len(repo.list_work_unit_events(work_unit_id, limit=10_000))

    second = settle_landed_integration({"intent_id": intent_id, "milestone_key": MILESTONE})

    assert isinstance(second, SettlementSkipped)
    assert "already SUCCEEDED" in second.reason
    assert len(repo.list_work_unit_events(work_unit_id, limit=10_000)) == events_after_first


def test_no_milestone_completes_without_an_integrated_row(work_unit_ledger: Path) -> None:
    work_unit_id = _blocked_unit("settle_invariant")
    intent_id = _milestone_intent(work_unit_id)

    outcome = settle_landed_integration({"intent_id": intent_id, "milestone_key": MILESTONE})

    assert isinstance(outcome, SettlementRefused)
    assert outcome.code == "integrated_row_missing"
    executions = {item.stable_key: item for item in repo.list_milestone_executions(work_unit_id)}
    assert executions[MILESTONE].status is MilestoneExecutionStatus.BLOCKED


def test_a_cancelled_unit_is_not_completed_and_the_refusal_names_the_cause(
    work_unit_ledger: Path,
) -> None:
    work_unit_id = _blocked_unit("settle_stale_plan")
    intent_id = _milestone_intent(work_unit_id)
    _land(intent_id, request_id="ir_settle_stale")
    service.cancel_work_unit(work_unit_id, reason="superseded by a new plan")

    outcomes = settle_landed_integrations()

    refused = outcomes[0]
    assert isinstance(refused, SettlementRefused)
    assert refused.code == "stale_plan"
    assert "CANCELLED" in refused.reason
    failed = _landed_events("FAILED")
    assert len(failed) == 1
    assert "stale_plan" in str(failed[0]["error"])
    executions = {item.stable_key: item for item in repo.list_milestone_executions(work_unit_id)}
    assert executions[MILESTONE].status is not MilestoneExecutionStatus.SUCCEEDED


def test_a_request_from_no_milestone_settles_nothing_and_is_not_an_error(
    work_unit_ledger: Path,
) -> None:
    work_unit_id = _blocked_unit("settle_no_milestone")
    intent_id = _milestone_intent(work_unit_id, milestone_key=None)
    _land(intent_id, request_id="ir_settle_none", milestone_key=None)

    outcomes = settle_landed_integrations()

    skipped = outcomes[0]
    assert isinstance(skipped, SettlementSkipped)
    assert skipped.intent_id == intent_id
    assert not repo.list_work_unit_artifacts(work_unit_id)
    assert (
        next(
            row
            for row in repo.list_milestone_executions(work_unit_id)
            if row.stable_key == MILESTONE
        ).status
        is MilestoneExecutionStatus.BLOCKED
    )
    assert len(_landed_events("PROCESSED")) == 1


def test_the_manual_adopt_verb_routes_through_the_shared_core() -> None:
    """The manual and automatic paths cannot drift because they share the write.

    Pinned at the source level: both callers name
    `record_integrated_completion`, and the completion triple lives only there.
    """

    adopt_source = inspect.getsource(dispatch_adoption.adopt_integrated_milestone)
    assert "record_integrated_completion(" in adopt_source
    from local_first_agent_os.work_units import integration_settlement

    settle_source = inspect.getsource(integration_settlement.settle_landed_integration)
    assert "record_integrated_completion(" in settle_source


def _running_unit(design_doc_id: str) -> tuple[str, str]:
    work_unit_id = _blocked_unit(design_doc_id)
    intent_id = _milestone_intent(work_unit_id)
    for status in (MilestoneExecutionStatus.READY, MilestoneExecutionStatus.RUNNING):
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase.IMPLEMENT,
                milestone_key=MILESTONE,
                status=status,
                attempt=2,
                child_workflow_id="live-integration-wait",
                dispatch_intent_id=intent_id,
            ),
        )
    return work_unit_id, intent_id


def _running_landing(design_doc_id: str) -> tuple[str, str]:
    work_unit_id, intent_id = _running_unit(design_doc_id)
    _land(intent_id, request_id=f"ir_{design_doc_id}")
    return work_unit_id, intent_id


def test_normal_approved_payload_without_legacy_milestone_settles_bound_work(
    work_unit_ledger: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from local_first_agent_os.work_units import root_workflow

    work_unit_id, intent_id = _running_unit("normal_approval_dispatch_identity")
    target = build_target_repository(tmp_path / "target")
    config = tmp_path / "registry"
    write_registry_config(config, target.path, project_id="local_first_agent_os")
    monkeypatch.setenv("LOCAL_AGENT_CONFIG_DIR", str(config))
    get_settings.cache_clear()
    payload = code_merge_payload(target, project_id="local_first_agent_os")
    payload.pop("milestone_id")
    payload["intent_id"] = intent_id
    saga_id = str(create_saga("normal approval settlement")["saga_id"])
    approval = submit_approval_request(
        saga_id, "CODE_MERGE", payload=payload, requested_by="dispatcher_runner"
    )
    resolution = resolve_approval_request(
        str(approval["approval_id"]), approved=True, resolved_by="operator"
    )
    assert resolution["status"] == "APPROVED"
    with tx() as connection:
        requests = read_integration_requests(connection)
    assert len(requests) == 1
    queued = requests[0]
    assert isinstance(queued, Queued)
    assert queued.subject.intent_id == intent_id
    assert queued.subject.milestone_key is None
    in_flight = InFlight(
        subject=queued.subject,
        batch_id=IntegrationBatchId("batch_normal_approval"),
        attempt_id=IntegrationAttemptId("attempt_normal_approval"),
    )
    integrated = Integrated(
        subject=queued.subject,
        batch_id=in_flight.batch_id,
        integration_commit_sha=target.commit_sha,
        integrated_at=now(),
    )
    with tx() as connection:
        apply_integration_transition(connection, queued, in_flight, recorded_at=now())
        apply_integration_transition(connection, in_flight, integrated, recorded_at=now())
    landed = _landed_events("PENDING")
    assert len(landed) == 1
    landed_payload = landed[0]["payload"]
    assert isinstance(landed_payload, dict)
    assert landed_payload["milestone_key"] is None
    notifications = []
    monkeypatch.setattr(
        root_workflow,
        "notify_integration_settlement",
        lambda workflow_id, dispatch_id: notifications.append((workflow_id, dispatch_id)),
    )
    outcomes = settle_landed_integrations()
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], MilestoneSettled)
    assert outcomes[0].attempt == 2
    assert not outcomes[0].resume_enqueued
    milestone = next(
        row for row in repo.list_milestone_executions(work_unit_id) if row.stable_key == MILESTONE
    )
    assert milestone.status is MilestoneExecutionStatus.SUCCEEDED
    assert milestone.attempt == 2
    artifacts = repo.list_work_unit_artifacts(work_unit_id)
    assert len(artifacts) == 1
    assert artifacts[0].artifact_type.value == "source_patch"
    assert artifacts[0].metadata["approved_commit_sha"] == target.commit_sha
    assert artifacts[0].metadata["integration_commit_sha"] == target.commit_sha
    assert notifications == [("live-integration-wait", intent_id)]
    events = repo.list_work_unit_events(work_unit_id)
    assert isinstance(settle_landed_integration(landed_payload), SettlementSkipped)
    assert repo.list_work_unit_events(work_unit_id) == events
    assert repo.list_work_unit_artifacts(work_unit_id) == artifacts
    assert notifications == [("live-integration-wait", intent_id)]
    assert len(_landed_events("PROCESSED")) == 1


def test_null_legacy_key_does_not_bypass_current_dispatch_ownership() -> None:
    work_unit_id, current_intent = _running_unit("superseded_landing_dispatch")
    old_intent = _milestone_intent(work_unit_id)
    assert old_intent != current_intent
    _land(old_intent, request_id="ir_superseded_dispatch", milestone_key=None)
    events = repo.list_work_unit_events(work_unit_id)
    outcomes = settle_landed_integrations()
    assert outcomes == ()
    assert repo.list_work_unit_events(work_unit_id) == events
    assert not repo.list_work_unit_artifacts(work_unit_id)
    failed = _landed_events("FAILED")
    assert len(failed) == 1
    assert "superseded dispatch" in str(failed[0]["error"])


def _expire_integration_wait(work_unit_id: str) -> dict:
    from local_first_agent_os.work_units.execution import DispatchBackedExecutorRuntime
    from local_first_agent_os.work_units.root_workflow import WorkUnitEngine

    return WorkUnitEngine(runtime=DispatchBackedExecutorRuntime())._block_on_halted_dispatch(
        work_unit_id=work_unit_id,
        phase=LifecyclePhase.IMPLEMENT,
        milestone_key=MILESTONE,
        attempt=2,
        child_workflow_id="live-integration-wait",
        failure_code="integration_wait_elapsed",
        failure_summary="test deadline elapsed",
    )


@pytest.mark.parametrize("timeout_first", [True, False])
def test_timeout_and_landing_converge_without_duplicate_success(monkeypatch, timeout_first) -> None:
    from local_first_agent_os.work_units import integration_settlement

    work_unit_id, intent_id = _running_landing(f"landing_race_{timeout_first}")
    original = integration_settlement.record_integrated_completion

    def interleave(*args, **kwargs):
        # The consumer has already observed RUNNING and prepared its context.
        if timeout_first:
            assert _expire_integration_wait(work_unit_id)["status"] == "BLOCKED"
        return original(*args, **kwargs)

    monkeypatch.setattr(integration_settlement, "record_integrated_completion", interleave)
    outcomes = settle_landed_integrations()
    settled = outcomes[0]
    assert isinstance(settled, MilestoneSettled)
    if not timeout_first:
        assert _expire_integration_wait(work_unit_id)["status"] == "SUCCEEDED"
    milestone = next(
        m for m in repo.list_milestone_executions(work_unit_id) if m.stable_key == MILESTONE
    )
    assert milestone.status is MilestoneExecutionStatus.SUCCEEDED
    assert milestone.attempt == settled.attempt == (3 if timeout_first else 2)
    assert settled.resume_enqueued is timeout_first
    successes = [
        e
        for e in repo.list_work_unit_events(work_unit_id)
        if e.payload.get("milestone_key") == MILESTONE and e.payload.get("status") == "SUCCEEDED"
    ]
    assert len(successes) == 1
    assert len(_landed_events("PROCESSED")) == 1
    assert not _landed_events("FAILED")
    assert isinstance(
        settle_landed_integration({"intent_id": intent_id, "milestone_key": MILESTONE}),
        SettlementSkipped,
    )


def test_integration_recovery_facts_roll_back_together(monkeypatch) -> None:
    work_unit_id = _blocked_unit("landing_atomic_rollback")
    intent_id = _milestone_intent(work_unit_id)
    _land(intent_id, request_id="ir_atomic_rollback")
    before = repo.list_work_unit_events(work_unit_id)
    original = repo._record_fact_on_connection

    def fail_between_facts(connection, unit_id, fact, **kwargs):
        if (
            isinstance(fact, MilestoneTransition)
            and fact.status is MilestoneExecutionStatus.RUNNING
        ):
            raise RuntimeError("injected failure between recovery facts")
        return original(connection, unit_id, fact, **kwargs)

    monkeypatch.setattr(repo, "_record_fact_on_connection", fail_between_facts)
    with pytest.raises(RuntimeError, match="injected failure"):
        settle_landed_integration({"intent_id": intent_id, "milestone_key": MILESTONE})
    assert repo.list_work_unit_events(work_unit_id) == before
    milestone = next(
        m for m in repo.list_milestone_executions(work_unit_id) if m.stable_key == MILESTONE
    )
    assert milestone.status is MilestoneExecutionStatus.BLOCKED
    assert milestone.attempt == 1
    assert not repo.list_work_unit_artifacts(work_unit_id)


def test_a_stale_timeout_cannot_park_a_replacement_attempt() -> None:
    work_unit_id, _ = _running_landing("landing_stale_timeout")
    before = repo.list_work_unit_events(work_unit_id)
    current = repo.record_dispatch_wait_halt(
        work_unit_id,
        phase=LifecyclePhase.IMPLEMENT,
        milestone_key=MILESTONE,
        attempt=1,
        child_workflow_id="obsolete-wait",
        failure_code="integration_wait_elapsed",
        failure_summary="old deadline",
    )
    assert current.status is MilestoneExecutionStatus.RUNNING
    assert current.attempt == 2
    assert repo.list_work_unit_events(work_unit_id) == before
