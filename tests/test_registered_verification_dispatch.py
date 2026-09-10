# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from test_host_verification_receipts import GateFixture
from test_host_verification_receipts import gate_fixture as gate_fixture

from local_first_agent_os import host_verification as host
from local_first_agent_os import registered_verification_dispatch as driver
from local_first_agent_os.coordination.dispatch import complete_dispatch_intent
from local_first_agent_os.coordination.execution import (
    complete_execution_lease,
    request_execution_cancel,
)
from local_first_agent_os.coordination.outcomes import TerminalOutcome
from local_first_agent_os.coordination.store import tx
from local_first_agent_os.dispatch_payloads import InterruptedRunObservation
from local_first_agent_os.dispatch_results import (
    AvailableDispatchReport,
    DispatchEvidenceSubject,
    decode_dispatch_runner_result,
)
from local_first_agent_os.dispatcher import DispatchDeferralReason, IntentDeferred, LedgerDispatcher
from local_first_agent_os.dispatcher_runner import DispatcherIntentRunner
from local_first_agent_os.execution_admission import ExecutionAdmissionError
from local_first_agent_os.pow_wow.types import CommandRunCapture
from local_first_agent_os.settings import Settings
from local_first_agent_os.verification_resources import NeonVerificationResourceIdentity
from local_first_agent_os.work_units import dispatch_adoption
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.execution import (
    MilestoneAwaitingDispatch,
    MilestoneContext,
    MilestoneFailed,
    RegisteredVerificationRuntime,
)
from local_first_agent_os.work_units.lifecycle import FailureClass, MilestoneExecutionStatus
from local_first_agent_os.work_units.retry import ChargedFailure, UnchargedFailure, attempt_charge


@pytest.fixture
def registered_request(gate_fixture: GateFixture, monkeypatch) -> driver.RegisteredGateDispatch:
    complete_execution_lease(gate_fixture.lease_id, "COMPLETED")
    with tx() as connection:
        connection.execute(
            "UPDATE dispatch_intents SET base_commit_sha=? WHERE intent_id=?",
            (gate_fixture.commit, gate_fixture.intent_id),
        )
    monkeypatch.setattr(driver, "load_project_center", host.load_project_center)
    request = driver.registered_gate_dispatch(gate_fixture.intent_id)
    assert request is not None
    return replace(request, timeout_seconds=15)


def _settle_registered_failure(request: driver.RegisteredGateDispatch) -> MilestoneFailed:
    unit = repo.get_work_unit(request.subject.work_unit_id)
    plan = repo.get_compiled_plan_revision(unit.compiled_plan_revision_id).plan
    context = MilestoneContext(
        work_unit_id=unit.work_unit_id,
        root_workflow_id=unit.root_workflow_id,
        child_workflow_id=f"{unit.root_workflow_id}:milestone:{request.subject.milestone_key}:1",
        milestone=plan.milestone(request.subject.milestone_key),
        attempt=request.subject.attempt,
        design_doc_revision_id=unit.design_doc_revision_id,
        compiled_plan_hash=unit.compiled_plan_hash,
        target_project_id=plan.target_project_id,
    )
    settled = RegisteredVerificationRuntime().settle(
        context, MilestoneAwaitingDispatch(request.subject.intent_id, request.timeout_seconds)
    )
    assert isinstance(settled, MilestoneFailed)
    return settled


@pytest.mark.parametrize(
    "reason", ["snapshot unavailable", "verification failed dependency deadline"]
)
def test_typed_verification_unavailable_is_an_uncharged_scheduling_result(
    registered_request: driver.RegisteredGateDispatch, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    monkeypatch.setattr(
        host, "run_registered_verification", lambda **kwargs: host.VerificationUnavailable(reason)
    )
    result = driver.run_registered_gate_dispatch(registered_request)
    assert not isinstance(result, IntentDeferred)
    status, report, error = result
    completed = complete_dispatch_intent(
        registered_request.subject.intent_id, status, result=report, error=error
    )
    assert completed["ok"], completed
    settled = _settle_registered_failure(registered_request)
    assert settled.failure_class is FailureClass.SCHEDULING
    assert settled.failure_code == "VERIFICATION_UNAVAILABLE"
    assert isinstance(attempt_charge(settled.failure_class), UnchargedFailure)
    with tx() as connection:
        row = connection.execute(
            "SELECT outcome FROM dispatch_intents WHERE intent_id=?",
            (registered_request.subject.intent_id,),
        ).fetchone()
        receipts = connection.execute(
            "SELECT count(*) AS count FROM host_verification_receipts WHERE intent_id=?",
            (registered_request.subject.intent_id,),
        ).fetchone()
    assert row is not None and row["outcome"] == "VERIFICATION_UNAVAILABLE"
    assert receipts is not None and receipts["count"] == 0


@pytest.mark.parametrize("field", ["intent_id", "target_project_id"])
def test_unavailable_classification_cannot_bypass_subject_binding(
    registered_request: driver.RegisteredGateDispatch, field: str
) -> None:
    status, report, error = driver._report(
        registered_request, host.VerificationUnavailable("snapshot unavailable")
    )
    assert report is not None
    payload = json.loads(report)
    payload[field] = "a-different-subject"
    refused = complete_dispatch_intent(
        registered_request.subject.intent_id, status, result=json.dumps(payload), error=error
    )
    assert not refused["ok"]
    assert refused["error"] == "dispatch_report_contract_violation"
    with tx() as connection:
        row = connection.execute(
            "SELECT status, outcome FROM dispatch_intents WHERE intent_id=?",
            (registered_request.subject.intent_id,),
        ).fetchone()
    assert row is not None and row["status"] == "CLAIMED" and row["outcome"] is None


def test_public_runner_routes_real_gate_without_constructing_model_work(
    registered_request: driver.RegisteredGateDispatch, monkeypatch
) -> None:
    def model_path_forbidden(*args, **kwargs):
        pytest.fail("registered verification entered model decomposition")

    monkeypatch.setattr(DispatcherIntentRunner, "run_intent", model_path_forbidden)
    runner = object.__new__(DispatcherIntentRunner)
    terminal = runner({"intent_id": registered_request.subject.intent_id})
    assert not isinstance(terminal, IntentDeferred)
    status, payload, error = terminal
    assert status == "DONE", error or payload
    parsed = decode_dispatch_runner_result(
        intent_result=payload,
        approval_payload={},
        expected_subject=DispatchEvidenceSubject(
            registered_request.subject.intent_id, registered_request.subject.target_project_id
        ),
    )
    assert isinstance(parsed, AvailableDispatchReport)
    observation = parsed.result.observation
    assert observation is not None
    references = tuple(
        artifact
        for artifact in observation.artifacts
        if artifact.artifact_type == host.REFERENCE_KIND
    )
    assert len(references) == 1
    receipt_id = references[0].content["receipt_id"]
    assert isinstance(receipt_id, str)
    evidence = host.resolve_verification_receipt(receipt_id, registered_request.subject)
    assert isinstance(evidence, host.VerifiedReceiptReference)
    assert evidence.receipt.source.commit == registered_request.source_commit
    assert all(capture.exit_code == 0 for capture in evidence.captures)
    assert "source write and outside read denied" in evidence.captures[0].stdout
    complete = complete_dispatch_intent(
        registered_request.subject.intent_id, status, result=payload, error=error
    )
    assert complete["ok"], complete
    adopted = dispatch_adoption.adopt_settled_dispatch(
        registered_request.subject.work_unit_id, registered_request.subject.milestone_key
    )
    assert adopted.applied
    assert adopted.work_unit_id == registered_request.subject.work_unit_id
    assert adopted.milestone_key == registered_request.subject.milestone_key
    assert adopted.intent_id == registered_request.subject.intent_id
    milestone = next(
        item
        for item in repo.list_milestone_executions(registered_request.subject.work_unit_id)
        if item.stable_key == registered_request.subject.milestone_key
    )
    assert milestone.status is MilestoneExecutionStatus.SUCCEEDED
    artifacts = repo.list_work_unit_artifacts(registered_request.subject.work_unit_id)
    assert [artifact.artifact_type.value for artifact in artifacts] == ["test_result"]
    assert artifacts[0].metadata["host_verification_receipt_ids"] == [receipt_id]
    replay = dispatch_adoption.adopt_settled_dispatch(adopted.work_unit_id, adopted.milestone_key)
    assert not replay.applied
    assert repo.list_work_unit_artifacts(adopted.work_unit_id) == artifacts


def test_same_claim_cannot_launch_a_second_gate(registered_request, monkeypatch) -> None:
    first = driver.run_registered_gate_dispatch(registered_request)
    assert not isinstance(first, IntentDeferred)
    assert first[0] == "DONE", first

    def second_launch_forbidden(**kwargs):
        pytest.fail("same claim launched verification twice")

    monkeypatch.setattr(host, "run_registered_verification", second_launch_forbidden)
    second = driver.run_registered_gate_dispatch(registered_request)
    assert second == first


@pytest.mark.parametrize("capability", ["invoke_model", "write_repository", "network_access"])
def test_gate_refuses_authority_outside_registered_contract(registered_request, capability) -> None:
    with tx() as connection:
        connection.execute(
            "UPDATE dispatch_intents SET permitted_capabilities=? WHERE intent_id=?",
            (
                json.dumps(["read_repository", "run_command", capability]),
                registered_request.subject.intent_id,
            ),
        )
    with pytest.raises(ExecutionAdmissionError):
        driver.registered_gate_dispatch(registered_request.subject.intent_id)


def test_gate_requires_exact_retained_source(registered_request) -> None:
    with tx() as connection:
        connection.execute(
            "UPDATE dispatch_intents SET base_commit_sha=NULL WHERE intent_id=?",
            (registered_request.subject.intent_id,),
        )
    with pytest.raises(ValueError, match="exact dependency source"):
        driver.registered_gate_dispatch(registered_request.subject.intent_id)


def test_non_workunit_intent_does_not_select_registered_commands(work_unit_ledger) -> None:
    assert driver.registered_gate_dispatch("unlinked-intent") is None


def test_cancelled_capture_keeps_its_declared_result_variant(
    registered_request: driver.RegisteredGateDispatch,
) -> None:
    # A serializer control, not execution evidence or a claim that this receipt exists.
    capture = CommandRunCapture(
        command="cancelled fixture",
        cwd=str(registered_request.source_repository),
        stdout="",
        stderr="terminated",
        exit_code=-15,
    )
    status, payload, _ = driver._report(
        registered_request, host.VerificationCancelled("serializer-fixture", (capture,))
    )
    assert status == "FAILED"
    parsed = decode_dispatch_runner_result(
        intent_result=payload,
        approval_payload={},
        expected_subject=DispatchEvidenceSubject(
            registered_request.subject.intent_id, registered_request.subject.target_project_id
        ),
    )
    assert isinstance(parsed, AvailableDispatchReport)
    assert isinstance(parsed.result.observation, InterruptedRunObservation)
    assert parsed.result.observation.status == "CANCELED"


def test_public_cancel_after_first_gate_prevents_the_next_command(
    registered_request: driver.RegisteredGateDispatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    center = host.load_project_center()
    project = center.project_by_id(registered_request.subject.target_project_id)
    commands = (*project.verification_commands, "printf must-not-run-after-cancellation")
    project = replace(project, verification_commands=list(commands))
    monkeypatch.setattr(host, "load_project_center", lambda: replace(center, projects=(project,)))
    monkeypatch.setattr(driver, "load_project_center", host.load_project_center)
    request = replace(registered_request, commands=commands)
    real_runner = host.run_captured_command
    gate_commands: list[str] = []

    def cancel_after_gate(
        command: Sequence[str],
        cwd: Path,
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        complete_environment: bool = False,
    ) -> CommandRunCapture:
        capture = real_runner(
            command,
            cwd,
            timeout_seconds=timeout_seconds,
            env=env,
            complete_environment=complete_environment,
        )
        if "-c" in command:
            gate_commands.append(command[-1])
        if "-c" in command and len(gate_commands) == 1:
            with tx() as connection:
                lease = connection.execute(
                    "SELECT lease_id FROM agent_execution_leases WHERE intent_id=? "
                    "AND status='ACTIVE'",
                    (request.subject.intent_id,),
                ).fetchone()
            assert lease is not None
            cancellation = request_execution_cancel(str(lease["lease_id"]))
            assert cancellation["ok"], cancellation
        return capture

    monkeypatch.setattr(host, "run_captured_command", cancel_after_gate)
    terminal = driver.run_registered_gate_dispatch(request)
    assert not isinstance(terminal, IntentDeferred)
    status, _, _ = terminal
    assert status == "FAILED"
    assert gate_commands == [commands[0]]
    with tx() as connection:
        assert not connection.execute(
            "SELECT receipt_id FROM host_verification_receipts"
        ).fetchall()


def test_active_replay_preserves_owned_execution_and_recovers_receipt_afterward(
    registered_request: driver.RegisteredGateDispatch,
    monkeypatch: pytest.MonkeyPatch,
    work_unit_ledger: Path,
) -> None:
    started, release = threading.Event(), threading.Event()
    real_runner = host.run_registered_verification
    launches: list[str] = []

    def blocked_gate(
        *,
        intent_id: str,
        lease_id: str,
        worker_id: str,
        source_repository: Path,
        source_commit: str,
        base_commit: str,
        timeout_seconds: int,
    ) -> host.VerificationOutcome:
        launches.append(lease_id)
        started.set()
        assert release.wait(10), "test did not release the real gate"
        return real_runner(
            intent_id=intent_id,
            lease_id=lease_id,
            worker_id=worker_id,
            source_repository=source_repository,
            source_commit=source_commit,
            base_commit=base_commit,
            timeout_seconds=timeout_seconds,
        )

    monkeypatch.setattr(host, "run_registered_verification", blocked_gate)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(driver.run_registered_gate_dispatch, registered_request)
        try:
            assert started.wait(10)
            deferred = driver.run_registered_gate_dispatch(registered_request)
            assert isinstance(deferred, IntentDeferred)
            assert deferred.reason is DispatchDeferralReason.EXECUTION_ACTIVE
            dispatcher = LedgerDispatcher(
                settings=Settings(coordination_root=work_unit_ledger), runner=lambda _: deferred
            )
            result = dispatcher._settle(
                {"intent_id": registered_request.subject.intent_id, "tier": "senior"}
            )
            assert result == deferred
            with tx() as connection:
                row = connection.execute(
                    "SELECT status,result FROM dispatch_intents WHERE intent_id=?",
                    (registered_request.subject.intent_id,),
                ).fetchone()
            assert row["status"] == "CLAIMED" and row["result"] is None
        finally:
            release.set()
        terminal = first.result(timeout=20)
    assert not isinstance(terminal, IntentDeferred)
    assert terminal[0] == "DONE", terminal
    assert driver.run_registered_gate_dispatch(registered_request) == terminal
    assert len(launches) == 1


def test_replay_cannot_use_a_tampered_retained_receipt(
    registered_request: driver.RegisteredGateDispatch,
) -> None:
    first = driver.run_registered_gate_dispatch(registered_request)
    assert not isinstance(first, IntentDeferred)
    assert first[0] == "DONE", first
    with tx() as connection:
        connection.execute("UPDATE host_verification_receipts SET output_json='[]'")
    replay = driver.run_registered_gate_dispatch(registered_request)
    assert isinstance(replay, IntentDeferred)
    assert replay.reason is DispatchDeferralReason.RETAINED_OUTCOME_UNAVAILABLE


@pytest.mark.parametrize(
    ("command", "expected_outcome", "expected_class"),
    [
        ("false", TerminalOutcome.VERIFICATION_FAILED, FailureClass.CORRECTABLE),
        ("kill -TERM $$", TerminalOutcome.VERIFICATION_CANCELED, FailureClass.REQUIRES_OPERATOR),
    ],
)
def test_replay_preserves_a_real_failure_or_cancellation_without_rerun(
    registered_request: driver.RegisteredGateDispatch,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    expected_outcome: TerminalOutcome,
    expected_class: FailureClass,
) -> None:
    center = host.load_project_center()
    project = replace(
        center.project_by_id(registered_request.subject.target_project_id),
        verification_commands=[command],
    )
    monkeypatch.setattr(host, "load_project_center", lambda: replace(center, projects=(project,)))
    monkeypatch.setattr(driver, "load_project_center", host.load_project_center)
    request = replace(registered_request, commands=(command,))
    first = driver.run_registered_gate_dispatch(request)
    assert not isinstance(first, IntentDeferred)
    assert first[0] == "FAILED", first

    def forbidden(**kwargs: object) -> host.VerificationOutcome:
        pytest.fail("retained failure replay started another verification process")

    monkeypatch.setattr(host, "run_registered_verification", forbidden)
    assert driver.run_registered_gate_dispatch(request) == first
    status, report, error = first
    completed = complete_dispatch_intent(
        request.subject.intent_id, status, result=report, error=error
    )
    assert completed["ok"], completed
    settled = _settle_registered_failure(request)
    assert settled.failure_code == expected_outcome.value
    assert settled.failure_class is expected_class
    assert isinstance(attempt_charge(settled.failure_class), ChargedFailure) is (
        expected_class is FailureClass.CORRECTABLE
    )


def test_cleanup_pending_preserves_process_truth_but_cannot_complete_dispatch(
    registered_request: driver.RegisteredGateDispatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adapter port control over a real local gate, not a Neon cleanup proof."""
    identity = NeonVerificationResourceIdentity(
        target_project_id=registered_request.subject.target_project_id,
        neon_project_id="fixture-neon",
        lease_id="a" * 32,
        role="aidashos_verify_" + "a" * 32,
        source_common_directory=str(registered_request.source_repository / ".git"),
        host="ep-fixture.us-east-1.aws.neon.tech",
        hostaddr="8.8.8.8",
        relay_port=65432,
        expires_at="2099-01-01T00:00:00+00:00",
    )
    pending = host.PendingVerificationResource(identity=identity)
    real_runner = host.run_registered_verification
    retained: list[host.VerificationCleanupPending] = []

    def pending_after_real_gate(
        *,
        intent_id: str,
        lease_id: str,
        worker_id: str,
        source_repository: Path,
        source_commit: str,
        base_commit: str,
        timeout_seconds: int,
    ) -> host.VerificationOutcome:
        outcome = real_runner(
            intent_id=intent_id,
            lease_id=lease_id,
            worker_id=worker_id,
            source_repository=source_repository,
            source_commit=source_commit,
            base_commit=base_commit,
            timeout_seconds=timeout_seconds,
        )
        assert isinstance(outcome, host.VerificationPassed)
        wrapped = host.VerificationCleanupPending(outcome, pending)
        retained.append(wrapped)
        return wrapped

    monkeypatch.setattr(host, "run_registered_verification", pending_after_real_gate)
    result = driver.run_registered_gate_dispatch(registered_request)
    assert isinstance(result, IntentDeferred)
    assert result.reason is DispatchDeferralReason.RESOURCE_CLEANUP_PENDING
    with tx() as connection:
        lease = connection.execute(
            "SELECT status,result_json FROM agent_execution_leases WHERE lease_id=?",
            (result.lease_id,),
        ).fetchone()
        intent = connection.execute(
            "SELECT status,result FROM dispatch_intents WHERE intent_id=?", (result.intent_id,)
        ).fetchone()
    assert lease["status"] == "COMPLETED"
    retained_result = json.loads(lease["result_json"])
    assert "receipt_id" in retained_result
    assert retained_result["resource_cleanup"]["identity"]["lease_id"] == identity.lease_id
    assert intent["status"] == "CLAIMED" and intent["result"] is None
    monkeypatch.setattr(host, "resolve_retained_verification_outcome", lambda *args: retained[0])
    replay = driver.run_registered_gate_dispatch(registered_request)
    assert isinstance(replay, IntentDeferred)
    assert replay.reason is DispatchDeferralReason.RESOURCE_CLEANUP_PENDING
    assert len(retained) == 1
