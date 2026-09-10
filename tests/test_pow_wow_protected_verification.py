# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A retained gate result cannot lose its cleanup obligation during projection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_host_verification_receipts import GateFixture, _receipt_payload
from test_host_verification_receipts import gate_fixture as gate_fixture
from test_host_verification_resources import _resource_fixture
from test_host_verification_resources import pinned_relay as pinned_relay

from local_first_agent_os import host_verification as host
from local_first_agent_os.coordination.dispatch import complete_dispatch_intent
from local_first_agent_os.coordination.execution import complete_execution_lease
from local_first_agent_os.pow_wow.executor import _protected_verification_artifacts
from local_first_agent_os.pow_wow.git_ops import WorktreeCommitCheckpoint
from local_first_agent_os.pow_wow.types import (
    ExecutionAttemptLease,
    PowWowArtifact,
    PowWowExecutionContext,
)
from local_first_agent_os.verification_resources import (
    NeonVerificationResourceIdentity,
    _PinnedLoopbackRelay,
)
from local_first_agent_os.work_units import dispatch_adoption
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.lifecycle import MilestoneExecutionStatus


def _project_result(fixture: GateFixture) -> tuple[PowWowArtifact, ...]:
    context = PowWowExecutionContext(
        saga_id="verification-projection",
        goal="retain the verification obligation",
        directive="verify the committed checkpoint",
        target_project_id="local-first-agent-os",
        target_project_path=str(fixture.repository),
        target_project_kind="code",
        target_project_status="active",
        target_project_read_only=False,
        dispatch_intent_id=fixture.intent_id,
    )
    return _protected_verification_artifacts(
        task_name="verify",
        context=context,
        execution_attempt=ExecutionAttemptLease(
            idempotency_key="verification-projection",
            worker_id="host-gate-worker",
            lease_id=fixture.lease_id,
        ),
        worktree_path=fixture.repository,
        checkpoint=WorktreeCommitCheckpoint(
            branch_name="codex/verification-projection",
            base_head_sha=fixture.commit,
            commit_sha=fixture.commit,
            commit_created=True,
            changed_from_base=False,
            checkpointed_files=(),
        ),
        timeout_seconds=15,
    )


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize(
    "process",
    [
        host.VerificationPassed("passed-receipt", ()),
        host.VerificationFailed("failed-receipt", ()),
        host.VerificationCancelled("cancelled-receipt", ()),
        host.VerificationUnavailable("executor unavailable"),
    ],
)
def test_every_host_outcome_preserves_its_process_and_cleanup_variant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pending: bool,
    process: host.VerificationProcessOutcome,
) -> None:
    resource = host.PendingVerificationResource(
        identity=NeonVerificationResourceIdentity(
            target_project_id="fixture_project",
            neon_project_id="fixture-neon",
            lease_id="a" * 32,
            role="aidashos_verify_" + "a" * 32,
            source_common_directory=str(tmp_path / ".git"),
            host="ep-fixture.us-east-1.aws.neon.tech",
            hostaddr="8.8.8.8",
            relay_port=65432,
            expires_at="2099-01-01T00:00:00+00:00",
        )
    )
    outcome: host.VerificationOutcome = (
        host.VerificationCleanupPending(process, resource) if pending else process
    )

    def observed_gate(**_kwargs: object) -> host.VerificationOutcome:
        return outcome

    monkeypatch.setattr(host, "run_registered_verification", observed_gate)
    fixture = GateFixture(tmp_path, "a" * 40, "intent", "lease", "work-unit", "verify")
    artifacts = _project_result(fixture)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    if pending:
        assert artifact.artifact_type == "host_verification_cleanup_pending"
        assert artifact.schema_version == "host_verification_cleanup_pending.v1"
        assert artifact.content["resource_cleanup"] == resource.model_dump(mode="json")
        retained = artifact.content["process"]
        if isinstance(process, host.VerificationUnavailable):
            assert retained == {"outcome": "unavailable", "reason": process.reason}
        else:
            assert retained == {
                "outcome": process.receipt_id.removesuffix("-receipt"),
                "receipt_id": process.receipt_id,
            }
        assert all(artifact.artifact_type != host.REFERENCE_KIND for artifact in artifacts)
    elif isinstance(process, host.VerificationUnavailable):
        assert artifact.artifact_type == "host_verification_unavailable"
        assert artifact.content == {"reason": process.reason}
    else:
        assert artifact.artifact_type == host.REFERENCE_KIND
        assert artifact.content == {"receipt_id": process.receipt_id}


@pytest.mark.parametrize("pending", [False, True])
def test_actual_projected_gate_cannot_discharge_verify_until_cleanup_is_proven(
    gate_fixture: GateFixture,
    monkeypatch: pytest.MonkeyPatch,
    pinned_relay: _PinnedLoopbackRelay,
    pending: bool,
) -> None:
    _resource_fixture(
        gate_fixture, monkeypatch, pinned_relay, cleanup="pending" if pending else "closed"
    )
    artifacts = _project_result(gate_fixture)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    if pending:
        assert artifact.artifact_type == "host_verification_cleanup_pending"
        process = artifact.content["process"]
        assert process["outcome"] == "passed"
        receipt_id = process["receipt_id"]
    else:
        assert artifact.artifact_type == host.REFERENCE_KIND
        receipt_id = artifact.content["receipt_id"]
    complete_execution_lease(gate_fixture.lease_id, "COMPLETED")
    payload = json.loads(_receipt_payload(gate_fixture, receipt_id))
    payload["run_result"]["artifacts"][0] = artifact.to_payload()
    complete_dispatch_intent(gate_fixture.intent_id, "DONE", result=json.dumps(payload))
    if pending:
        with pytest.raises(dispatch_adoption.DispatchAdoptionRefused):
            dispatch_adoption.adopt_settled_dispatch(
                gate_fixture.work_unit_id, gate_fixture.milestone_key
            )
        milestone = next(
            item
            for item in repo.list_milestone_executions(gate_fixture.work_unit_id)
            if item.stable_key == gate_fixture.milestone_key
        )
        assert milestone.status is MilestoneExecutionStatus.BLOCKED
        assert not repo.list_work_unit_artifacts(gate_fixture.work_unit_id)
    else:
        adopted = dispatch_adoption.adopt_settled_dispatch(
            gate_fixture.work_unit_id, gate_fixture.milestone_key
        )
        assert adopted.applied
        published = repo.list_work_unit_artifacts(gate_fixture.work_unit_id)
        assert [item.artifact_type.value for item in published] == ["test_result"]
        assert published[0].metadata["host_verification_receipt_ids"] == [receipt_id]
