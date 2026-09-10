# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Real local gate processes certify only their protected, bound source snapshot."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_settled_dispatch_adoption import _runner_result, _settled_intent, _wait_elapsed_milestone

from local_first_agent_os import host_verification as host
from local_first_agent_os.coordination.dispatch import complete_dispatch_intent
from local_first_agent_os.coordination.execution import (
    complete_execution_lease,
    open_execution_lease,
    request_execution_cancel,
)
from local_first_agent_os.coordination.store import tx
from local_first_agent_os.project_access import AccessMode, ProjectAccessPolicy
from local_first_agent_os.project_center import LinkedProject, ProjectCenter
from local_first_agent_os.work_units import dispatch_adoption
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.lifecycle import LifecyclePhase, MilestoneExecutionStatus


@dataclass(frozen=True)
class GateFixture:
    repository: Path
    commit: str
    intent_id: str
    lease_id: str
    work_unit_id: str
    milestone_key: str

    def run(self) -> host.VerificationOutcome:
        return host.run_registered_verification(
            intent_id=self.intent_id,
            lease_id=self.lease_id,
            worker_id="host-gate-worker",
            source_repository=self.repository,
            source_commit=self.commit,
            base_commit=self.commit,
            timeout_seconds=15,
        )


@pytest.fixture
def gate_fixture(work_unit_ledger, tmp_path, monkeypatch) -> GateFixture:
    monkeypatch.setattr(
        host, "acquire_verification_resources", lambda *_args: host.VerificationResourcesAbsent()
    )
    repository = tmp_path / "registered-project"
    repository.mkdir()
    secret = tmp_path / "outside-canary.txt"
    secret.write_text("not-readable-by-gate")
    (repository / "value.txt").write_text("sealed source")
    (repository / "gate.py").write_text(
        "from pathlib import Path\nimport os\n"
        "assert Path('value.txt').read_text() == 'sealed source'\n"
        "for action in [lambda: Path('value.txt').write_text('tamper'), "
        f"lambda: Path({str(secret)!r}).read_text()]:\n"
        "    try: action()\n"
        "    except PermissionError: pass\n"
        "    else: raise AssertionError('containment canary was allowed')\n"
        "Path(os.environ['AIDASHOS_VERIFICATION_OUTPUT_DIR'], 'result.txt').write_text('ok')\n"
        "print('real gate passed; source write and outside read denied')\n"
    )
    for arguments in [
        ("init", "-q"),
        ("add", "."),
        (
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@invalid",
            "commit",
            "-qm",
            "fixture source",
        ),
    ]:
        subprocess.run(("git", "-C", str(repository), *arguments), check=True, capture_output=True)
    commit = subprocess.check_output(
        ("git", "-C", str(repository), "rev-parse", "HEAD"), text=True
    ).strip()
    intent_id = _settled_intent(status="CLAIMED")
    work_unit_id, milestone_key = _wait_elapsed_milestone(intent_id, phase=LifecyclePhase.VERIFY)
    unit = repo.get_work_unit(work_unit_id)
    plan = repo.get_compiled_plan_revision(unit.compiled_plan_revision_id).plan
    project = LinkedProject(
        id=plan.target_project_id,
        kind="code",
        path=repository,
        status="active",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="test",
        verification_commands=[f"{shlex.quote(sys.executable)} -I gate.py"],
    )
    center = ProjectCenter(
        id="fixture",
        description="fixture",
        control_plane_project=project.id,
        default_saga_project=project.id,
        default_memory_project=project.id,
        projects=(project,),
    )
    monkeypatch.setattr(host, "load_project_center", lambda: center)
    lease = open_execution_lease(
        "host-receipt-fixture",
        "host-gate-worker",
        intent_id=intent_id,
        target_project_id=project.id,
        permission_envelope_sha256="a" * 64,
        timeout_seconds=120,
    )
    return GateFixture(
        repository, commit, intent_id, str(lease["lease"]["lease_id"]), work_unit_id, milestone_key
    )


def _require_real_pass(fixture: GateFixture) -> host.VerificationPassed:
    outcome = fixture.run()
    assert isinstance(outcome, host.VerificationPassed), outcome
    assert "source write and outside read denied" in outcome.captures[0].stdout
    return outcome


def _receipt_payload(fixture: GateFixture, receipt_id: str, *, commit: str | None = None) -> str:
    payload = json.loads(_runner_result(changed_files=(), intent_id=fixture.intent_id))
    payload["run_result"]["artifacts"] = [
        {
            "artifact_type": host.REFERENCE_KIND,
            "schema_version": "host_verification_receipt_reference.v1",
            "content": {"receipt_id": receipt_id},
        },
        {
            "artifact_type": "worktree_commit_checkpoint",
            "schema_version": "worktree_commit_checkpoint.v1",
            "content": {"commit_sha": commit or fixture.commit, "base_head_sha": fixture.commit},
        },
    ]
    return json.dumps(payload)


def test_real_contained_gate_receipt_can_discharge_verify_once(gate_fixture) -> None:
    result = _require_real_pass(gate_fixture)
    complete_execution_lease(gate_fixture.lease_id, "COMPLETED")
    subject = host.verification_subject_for_dispatch(gate_fixture.intent_id)
    assert subject is not None
    verified = host.resolve_verification_receipt(result.receipt_id, subject)
    assert isinstance(verified, host.VerifiedReceiptReference)
    assert verified.receipt.source.commit == gate_fixture.commit
    assert (
        verified.receipt.lease_created_at
        <= verified.receipt.started_at
        <= verified.receipt.completed_at
    )
    assert all(capture.exit_code == 0 for capture in verified.captures)
    identity = json.loads(verified.receipt.runtime_identity)
    assert len(identity["containment_profile_sha256"]) == 64
    assert len(identity["shell_sha256"]) == 64
    assert len(identity["python_executable_sha256"]) == 64
    complete_dispatch_intent(
        gate_fixture.intent_id, "DONE", result=_receipt_payload(gate_fixture, result.receipt_id)
    )
    first = dispatch_adoption.adopt_settled_dispatch(
        gate_fixture.work_unit_id, gate_fixture.milestone_key
    )
    replay = dispatch_adoption.adopt_settled_dispatch(
        gate_fixture.work_unit_id, gate_fixture.milestone_key
    )
    assert first.applied and not replay.applied
    artifacts = repo.list_work_unit_artifacts(gate_fixture.work_unit_id)
    assert [a.artifact_type.value for a in artifacts] == ["test_result"]
    assert artifacts[0].metadata["host_verification_receipt_ids"] == [result.receipt_id]


@pytest.mark.parametrize(
    "corruption", ["unknown_receipt", "wrong_commit", "wrong_subject", "output_tamper"]
)
def test_operator_supplied_result_cannot_relabel_or_fabricate_receipt(
    gate_fixture, corruption
) -> None:
    result = _require_real_pass(gate_fixture)
    receipt_id = "fabricated" if corruption == "unknown_receipt" else result.receipt_id
    payload = _receipt_payload(
        gate_fixture, receipt_id, commit="f" * 40 if corruption == "wrong_commit" else None
    )
    if corruption == "wrong_subject":
        subject = host.verification_subject_for_dispatch(gate_fixture.intent_id)
        assert subject is not None
        other = subject.model_copy(update={"intent_id": "different-intent"})
        assert isinstance(
            host.resolve_verification_receipt(receipt_id, other), host.ContradictoryEvidence
        )
        payload = json.loads(payload)
        payload["intent_id"] = "different-intent"
        payload = json.dumps(payload)
    if corruption == "output_tamper":
        with tx() as connection:
            connection.execute(
                "UPDATE host_verification_receipts SET output_json=? WHERE receipt_id=?",
                ("[]", receipt_id),
            )
    complete_dispatch_intent(gate_fixture.intent_id, "DONE", result=payload)
    with pytest.raises(dispatch_adoption.DispatchAdoptionRefused):
        dispatch_adoption.adopt_settled_dispatch(
            gate_fixture.work_unit_id, gate_fixture.milestone_key
        )
    milestone = next(
        m
        for m in repo.list_milestone_executions(gate_fixture.work_unit_id)
        if m.stable_key == gate_fixture.milestone_key
    )
    assert milestone.status is MilestoneExecutionStatus.BLOCKED
    assert not repo.list_work_unit_artifacts(gate_fixture.work_unit_id)


def test_success_prose_from_nonzero_process_never_certifies(gate_fixture, monkeypatch) -> None:
    center = host.load_project_center()
    project = replace(
        center.projects[0], verification_commands=["printf 'all tests passed'; exit 9"]
    )
    monkeypatch.setattr(host, "load_project_center", lambda: replace(center, projects=(project,)))
    outcome = gate_fixture.run()
    assert isinstance(outcome, host.VerificationFailed), outcome
    subject = host.verification_subject_for_dispatch(gate_fixture.intent_id)
    assert subject is not None
    assert isinstance(
        host.resolve_verification_receipt(outcome.receipt_id, subject),
        host.RetainedLegacyObservation,
    )


@pytest.mark.parametrize("stop", ["cancel", "expire"])
def test_ownership_change_after_process_before_receipt_insert_is_unavailable(
    gate_fixture, monkeypatch, stop
) -> None:
    real_runner = host.run_captured_command

    def stop_after_gate(command, *args, **kwargs):
        capture = real_runner(command, *args, **kwargs)
        if "-c" in command:
            if stop == "cancel":
                request_execution_cancel(gate_fixture.lease_id)
            else:
                with tx() as connection:
                    connection.execute(
                        "UPDATE agent_execution_leases SET lease_expires_at=0 WHERE lease_id=?",
                        (gate_fixture.lease_id,),
                    )
        return capture

    monkeypatch.setattr(host, "run_captured_command", stop_after_gate)
    assert isinstance(gate_fixture.run(), host.VerificationUnavailable)
    with tx() as connection:
        assert not connection.execute(
            "SELECT receipt_id FROM host_verification_receipts"
        ).fetchall()


@pytest.mark.parametrize("nested", ["COMPLETED", "FAILED"])
def test_forged_failed_output_cannot_adopt_verify_without_receipt(work_unit_ledger, nested) -> None:
    intent = _settled_intent(status="CLAIMED")
    unit, key = _wait_elapsed_milestone(intent, phase=LifecyclePhase.VERIFY)
    payload = json.loads(_runner_result(changed_files=(), intent_id=intent))
    payload["result_state"] = "COMPLETED"
    payload["run_result"]["status"] = nested
    payload["run_result"]["verification_commands"] = []
    payload["run_result"]["verification_output"] = ["FAILED one test; exit 1"]
    complete_dispatch_intent(intent, "DONE", result=json.dumps(payload))
    with pytest.raises(dispatch_adoption.DispatchAdoptionRefused):
        dispatch_adoption.adopt_settled_dispatch(unit, key)
    assert not repo.list_work_unit_artifacts(unit)


@pytest.mark.parametrize("attribute", ["export-ignore", "export-subst"])
def test_export_attributes_cannot_hide_failing_committed_test(
    gate_fixture, monkeypatch, attribute
) -> None:
    repository = gate_fixture.repository
    (repository / ".gitattributes").write_text(f"test_failure.py {attribute}\n")
    (repository / "test_failure.py").write_text(
        "assert False, 'committed failure must execute'\n"
        if attribute == "export-ignore"
        else "assert '$Format:%H$' != '$' + 'Format:%H' + '$', 'source substitution hid failure'\n"
    )
    for arguments in [
        ("add", "."),
        (
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@invalid",
            "commit",
            "-qm",
            "attribute",
        ),
    ]:
        subprocess.run(("git", "-C", str(repository), *arguments), check=True, capture_output=True)
    commit = subprocess.check_output(
        ("git", "-C", str(repository), "rev-parse", "HEAD"), text=True
    ).strip()
    program = "from pathlib import Path; [exec(p.read_text()) for p in Path('.').glob('test_*.py')]"
    center = host.load_project_center()
    project = replace(
        center.projects[0],
        verification_commands=[f"{shlex.quote(sys.executable)} -I -c {shlex.quote(program)}"],
    )
    monkeypatch.setattr(host, "load_project_center", lambda: replace(center, projects=(project,)))
    outcome = replace(gate_fixture, commit=commit).run()
    assert isinstance(outcome, host.VerificationFailed), outcome
    assert "AssertionError" in outcome.captures[0].stderr


@pytest.mark.parametrize("length", [41, 63])
def test_source_identity_rejects_non_git_digest_lengths(length) -> None:
    with pytest.raises(ValidationError):
        host.CommittedSource(
            commit="a" * length, tree="b" * 40, base="c" * 40, manifest_digest="d" * 64
        )
