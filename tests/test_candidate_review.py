# SPDX-License-Identifier: AGPL-3.0-or-later
"""Committed Git and real ledger ownership, with an explicit protected-reader fixture.

The receipt fixture models the retained successful host-reader result from the
blocked real review. It does not claim to execute a native gate or a provider.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from test_settled_dispatch_adoption import _settled_intent, _wait_elapsed_milestone

from local_first_agent_os import host_verification as host
from local_first_agent_os.coordination.contracts import SubmitArtifact, parse_coordination_result
from local_first_agent_os.coordination.execution import open_execution_lease
from local_first_agent_os.coordination.pow_wows import claim_task, create_pow_wow, submit_artifact
from local_first_agent_os.coordination.projects import create_saga
from local_first_agent_os.coordination.store import tx
from local_first_agent_os.pow_wow import candidate_review as candidate
from local_first_agent_os.pow_wow.executor import CliPowWowExecutor, StartFreshIndependent
from local_first_agent_os.pow_wow.git_ops import WorktreeAllocation
from local_first_agent_os.pow_wow.prompts import (
    build_agent_task_prompt,
    render_dependency_context_block,
)
from local_first_agent_os.pow_wow.protocol import TaskPurpose
from local_first_agent_os.pow_wow.types import (
    CommandRunCapture,
    PowWowArtifact,
    PowWowExecutionContext,
    PowWowTaskResult,
    PowWowTaskSpec,
)
from local_first_agent_os.project_access import AccessMode, ProjectAccessPolicy
from local_first_agent_os.project_center import LinkedProject


def _git(path: Path, *argv: str) -> str:
    return subprocess.run(
        ("git", "-C", str(path), *argv), check=True, capture_output=True, text=True
    ).stdout.strip()


@dataclass(frozen=True)
class CandidateFixture:
    context: PowWowExecutionContext
    allocation: WorktreeAllocation
    producer: PowWowTaskResult
    pow_wow_id: str = "standalone"
    review_task_id: str | None = None

    def prepare(self, *, producers=None, context=None):
        return candidate.prepare_candidate_review(
            context=context or self.context,
            pow_wow_id=self.pow_wow_id,
            review_task_name="staff_review",
            dependencies=producers if producers is not None else (self.producer,),
            repository=Path(self.allocation.worktree_path),
            source_repository=Path(self.allocation.source_repo_path),
        )


@pytest.fixture
def committed(tmp_path) -> CandidateFixture:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q")
    _git(source, "config", "user.name", "Fixture")
    _git(source, "config", "user.email", "fixture@example.invalid")
    (source / "label_index.py").write_text(
        "def normalize_labels(labels):\n    return sorted(set(labels))\n"
    )
    _git(source, "add", ".")
    _git(source, "commit", "-qm", "precommitted source")
    base = _git(source, "rev-parse", "HEAD")
    worktree = tmp_path / "candidate"
    _git(source, "worktree", "add", "-qb", "candidate", str(worktree), base)
    (worktree / "label_index.py").write_text(
        "def normalize_labels(labels):\n    seen = set()\n    result = []\n"
        "    for raw in labels:\n        label = raw.strip()\n"
        "        if label and label not in seen:\n            seen.add(label)\n"
        "            result.append(label)\n    return result\n"
    )
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-qm", "candidate checkpoint")
    commit = _git(worktree, "rev-parse", "HEAD")
    allocation = WorktreeAllocation(str(source), str(worktree), base, "candidate", "preserve", True)
    context = PowWowExecutionContext(
        saga_id="standalone",
        goal="Preserve first-seen label order",
        directive="Implement the change",
        target_project_id="target",
        target_project_path=str(source),
        target_project_kind="code",
        target_project_status="active",
        target_project_read_only=False,
    )
    producer = PowWowTaskResult(
        task_name="senior_implementation",
        role="implementer",
        status="completed",
        summary="The senior reports all five tests passed.",
        changed_files=("label_index.py",),
        artifacts=(
            PowWowArtifact(
                artifact_type="cli_agent_run",
                schema_version="cli_agent_run.v1",
                task_name="senior_implementation",
                content={
                    "task": {"task_name": "senior_implementation"},
                    "target_project_id": "target",
                    "is_review": False,
                    "output": "Implementation finished; all five tests passed.",
                },
            ),
            PowWowArtifact(
                artifact_type="worktree_commit_checkpoint",
                schema_version="worktree_commit_checkpoint.v1",
                task_name="senior_implementation",
                content={
                    "task_name": "senior_implementation",
                    "worktree": allocation.to_payload(),
                    "base_head_sha": base,
                    "commit_sha": commit,
                    "checkpointed_files": ("label_index.py",),
                    "error": None,
                },
            ),
        ),
    )
    run, checkpoint = producer.artifacts
    producer = replace(
        producer,
        artifacts=(
            replace(
                run,
                content={
                    **run.content,
                    "worktree": allocation.to_payload(),
                    "commit_checkpoint": {
                        key: checkpoint.content[key]
                        for key in ("base_head_sha", "commit_sha", "checkpointed_files", "error")
                    },
                },
            ),
            checkpoint,
        ),
    )
    return CandidateFixture(context, allocation, producer)


@pytest.fixture
def protected(committed, work_unit_ledger, monkeypatch) -> CandidateFixture:
    intent = _settled_intent(status="CLAIMED")
    _wait_elapsed_milestone(intent)
    subject = host.verification_subject_for_dispatch(intent)
    assert subject is not None
    saga_id = create_saga("Candidate review fixture")["saga_id"]
    pow_wow_id = create_pow_wow(saga_id, "implementation", "Review committed source", "Approved")[
        "pow_wow_id"
    ]
    task_id = claim_task(pow_wow_id, committed.producer.task_name, "Fixture implementation")[
        "task_id"
    ]
    review_id = claim_task(pow_wow_id, "staff_review", "Review candidate")["task_id"]
    opened = open_execution_lease(
        "candidate-producer",
        "fixture-worker",
        task_id=task_id,
        intent_id=intent,
        target_project_id=subject.target_project_id,
        permission_envelope_sha256="a" * 64,
    )
    assert opened["ok"], opened
    lease = opened["lease"]
    with tx() as connection:
        created = connection.execute(
            "SELECT created_at FROM agent_execution_leases WHERE lease_id=?", (lease["lease_id"],)
        ).fetchone()["created_at"]
    base = committed.allocation.head_sha
    repository = Path(committed.allocation.worktree_path)
    receipt = host.Receipt(
        receipt_id="protected-reader-fixture",
        subject=subject,
        worker_lease_id=lease["lease_id"],
        worker_id="fixture-worker",
        lease_created_at=created,
        source=host.CommittedSource(
            base=base,
            commit=_git(repository, "rev-parse", "HEAD"),
            tree=_git(repository, "rev-parse", "HEAD^{tree}"),
            manifest_digest="b" * 64,
        ),
        gate_digest="c" * 64,
        commands=("uv run --offline --no-project python -m unittest discover -s tests -v",),
        runtime_identity="explicit protected-reader fixture",
        permission_envelope_sha256="a" * 64,
        output_digest="d" * 64,
        process_outcome="passed",
        started_at=created,
        completed_at=created,
    )

    def resolve(reference, expected):
        assert reference == receipt.receipt_id and expected == subject
        return host.VerifiedReceiptReference(receipt, ())

    monkeypatch.setattr(candidate, "resolve_verification_receipt", resolve)
    artifacts = tuple(
        replace(
            a,
            content={
                **a.content,
                "target_project_id": subject.target_project_id,
                "execution_lease": {
                    "lease_id": lease["lease_id"],
                    "task_id": task_id,
                    "worker_id": "fixture-worker",
                },
            },
        )
        if a.artifact_type == "cli_agent_run"
        else a
        for a in committed.producer.artifacts
    )
    reference = PowWowArtifact(
        artifact_type="host_verification_receipt_reference",
        schema_version="host_verification_receipt_reference.v1",
        task_name=committed.producer.task_name,
        content={"receipt_id": receipt.receipt_id},
    )
    return replace(
        committed,
        pow_wow_id=pow_wow_id,
        review_task_id=review_id,
        context=replace(
            committed.context,
            saga_id=saga_id,
            dispatch_intent_id=intent,
            target_project_id=subject.target_project_id,
            task_ids_by_name={committed.producer.task_name: task_id, "staff_review": review_id},
        ),
        producer=replace(committed.producer, artifacts=(*artifacts, reference)),
    )


def _writer(command):
    assert isinstance(command, SubmitArtifact)
    return parse_coordination_result(
        command,
        submit_artifact(
            command.pow_wow_id,
            command.artifact_type,
            command.content,
            task_id=command.task_id,
            schema_version=command.schema_version,
        ),
    )


def test_retained_dependency_shape_now_supplies_exact_host_evidence(protected):
    old = render_dependency_context_block((protected.producer,))
    assert "protected-reader-fixture" not in old and "diff --git" not in old
    prepared = protected.prepare()
    assert isinstance(prepared, candidate.CandidateReviewReady), prepared
    view = prepared.view
    assert isinstance(view.verification, candidate.ProtectedVerification)
    assert view.verification.receipt_id == "protected-reader-fixture"
    assert view.source.commit == _git(Path(protected.allocation.worktree_path), "rev-parse", "HEAD")
    assert view.source.base == protected.allocation.head_sha
    assert view.source.changed_files == ("label_index.py",)
    assert "+            result.append(label)" in view.source.patch
    assert hashlib.sha256(view.source.patch.encode()).hexdigest() == view.source.patch_sha256
    task = PowWowTaskSpec(
        task_name="staff_review",
        role="reviewer",
        description="Review the candidate",
        purpose=TaskPurpose.REVIEW,
    )
    prompt = build_agent_task_prompt(
        task,
        protected.context,
        dependency_results=(replace(protected.producer, summary="untrusted " * 5000),),
        candidate_review=view,
    )
    assert prepared.text in prompt
    assert "not a completed SOURCE_PATCH milestone" in prompt
    retained = candidate.persist_candidate_review(
        prepared,
        pow_wow_id=protected.pow_wow_id,
        review_task_id=protected.review_task_id,
        coordination_command=_writer,
    )
    assert isinstance(retained, candidate.CandidateReviewReady)
    with tx() as connection:
        row = connection.execute(
            "SELECT content,task_id FROM task_artifacts WHERE artifact_id=?",
            (retained.artifact.persisted_artifact_id,),
        ).fetchone()
    assert row is not None and row["task_id"] == protected.review_task_id
    saved = json.loads(row["content"])
    assert saved["content"]["rendered_view"] == prepared.text
    assert saved["content"]["view_sha256"] == hashlib.sha256(prepared.text.encode()).hexdigest()
    assert saved["artifact_type"] == "candidate_review_evidence"


@pytest.mark.parametrize(
    "damage",
    [
        "dirty",
        "wrong_checkpoint",
        "scope",
        "missing_run",
        "malformed_run",
        "producer_task",
        "receipt_lease",
        "missing_receipt",
        "ambiguous",
    ],
)
def test_incomplete_or_contradictory_candidate_refuses_before_review(protected, damage):
    fixture = protected
    artifacts = list(fixture.producer.artifacts)
    if damage == "dirty":
        Path(fixture.allocation.worktree_path, "outside_checkpoint.txt").write_text("unverified")
    elif damage in {"wrong_checkpoint", "scope"}:
        key, value = (
            ("commit_sha", "f" * 40)
            if damage == "wrong_checkpoint"
            else ("checkpointed_files", ("other.py",))
        )
        artifacts[1] = replace(artifacts[1], content={**artifacts[1].content, key: value})
    elif damage == "missing_run":
        artifacts = artifacts[1:]
    elif damage == "malformed_run":
        artifacts[0] = replace(artifacts[0], content={**artifacts[0].content, "task": None})
    elif damage in {"producer_task", "receipt_lease"}:
        key = "task_id" if damage == "producer_task" else "lease_id"
        artifacts[0] = replace(
            artifacts[0],
            content={
                **artifacts[0].content,
                "execution_lease": {
                    **artifacts[0].content["execution_lease"],
                    key: "another-owner",
                },
            },
        )
    elif damage == "missing_receipt":
        artifacts = artifacts[:-1]
    producer = replace(fixture.producer, artifacts=tuple(artifacts))
    producers = (producer, producer) if damage == "ambiguous" else (producer,)
    result = fixture.prepare(producers=producers)
    assert isinstance(result, candidate.CandidateReviewUnavailable), result


def test_failed_protected_resolution_never_falls_back_to_legacy(protected, monkeypatch):
    monkeypatch.setattr(
        candidate,
        "resolve_verification_receipt",
        lambda *_: host.ContradictoryEvidence("fixture mismatch"),
    )
    result = protected.prepare()
    assert isinstance(result, candidate.CandidateReviewUnavailable)
    assert result.cause is candidate.CandidateUnavailableCause.VERIFICATION


def test_explicit_standalone_has_no_protected_or_durable_claim(committed):
    prepared = committed.prepare()
    assert isinstance(prepared, candidate.CandidateReviewReady)
    assert isinstance(prepared.view.verification, candidate.StandaloneVerification)
    retained = candidate.persist_candidate_review(
        prepared, pow_wow_id="standalone", review_task_id=None, coordination_command=None
    )
    assert retained is prepared
    assert isinstance(retained, candidate.CandidateReviewReady)
    assert retained.artifact.persisted_artifact_id is None
    assert "protected_passed" not in prepared.view.model_dump_json()


def test_named_missing_dispatch_does_not_become_standalone(committed, work_unit_ledger):
    result = committed.prepare(context=replace(committed.context, dispatch_intent_id="missing"))
    assert isinstance(result, candidate.CandidateReviewUnavailable)


def test_named_zero_link_dispatch_is_explicit_legacy(committed, work_unit_ledger):
    intent = _settled_intent(status="CLAIMED")
    with tx() as connection:
        row = connection.execute(
            "SELECT target_project_id FROM dispatch_intents WHERE intent_id=?", (intent,)
        ).fetchone()
    context = replace(
        committed.context, dispatch_intent_id=intent, target_project_id=row["target_project_id"]
    )
    producer = replace(
        committed.producer,
        artifacts=tuple(
            replace(a, content={**a.content, "target_project_id": context.target_project_id})
            if a.artifact_type == "cli_agent_run"
            else a
            for a in committed.producer.artifacts
        ),
    )
    result = committed.prepare(context=context, producers=(producer,))
    assert isinstance(result, candidate.CandidateReviewReady)
    assert isinstance(result.view.verification, candidate.LegacyDispatchVerification)
    missing = candidate.persist_candidate_review(
        result, pow_wow_id="missing", review_task_id=None, coordination_command=None
    )
    assert isinstance(missing, candidate.CandidateReviewUnavailable)
    assert missing.cause is candidate.CandidateUnavailableCause.PERSISTENCE


def test_multiple_authoritative_milestone_links_refuse(protected):
    with tx() as connection:
        connection.execute(
            "UPDATE milestone_executions SET dispatch_intent_id=? WHERE milestone_execution_id=("
            "SELECT milestone_execution_id FROM milestone_executions "
            "WHERE dispatch_intent_id IS NULL LIMIT 1)",
            (protected.context.dispatch_intent_id,),
        )
    result = protected.prepare()
    assert isinstance(result, candidate.CandidateReviewUnavailable)
    assert "ambiguous" in result.reason


def test_complete_output_reader_refuses_without_truncation(committed):
    with pytest.raises(ValueError, match="complete-output bound"):
        candidate._git(
            Path(committed.allocation.worktree_path), "show", "HEAD:label_index.py", limit=32
        )


def test_git_inherited_redirects_and_fsmonitor_cannot_change_observation(
    committed, monkeypatch, tmp_path
):
    marker = tmp_path / "must-not-run"
    _git(Path(committed.allocation.source_repo_path), "config", "core.fsmonitor", f"touch {marker}")
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing"))
    result = committed.prepare()
    assert isinstance(result, candidate.CandidateReviewReady), result
    assert not marker.exists()


def _executor(fixture, monkeypatch, writer):
    executor = CliPowWowExecutor(
        worktree_root=Path(fixture.allocation.worktree_path).parent,
        coordination_command=writer,
        verification_commands=(),
    )
    monkeypatch.setattr(executor, "_authorize_spawn", lambda *a, **k: None)
    monkeypatch.setattr(
        executor, "_frontier_launch_decision", lambda **k: StartFreshIndependent("fixture")
    )
    monkeypatch.setattr(executor, "_open_execution_attempt_lease", lambda **k: None)
    monkeypatch.setattr(executor, "_inspection_request", lambda **k: None)
    return executor


def _run_review(fixture, executor):
    target = LinkedProject(
        id=fixture.context.target_project_id,
        path=Path(fixture.allocation.source_repo_path),
        kind="code",
        status="active",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="fixture",
    )
    task = PowWowTaskSpec(
        task_name="staff_review",
        role="reviewer",
        description="Review the change",
        purpose=TaskPurpose.REVIEW,
    )
    return executor._run_agent_task(
        pow_wow_id=fixture.pow_wow_id,
        target_project=target,
        task=task,
        context=fixture.context,
        dependency_results=(),
        candidate_results=(fixture.producer,),
        worktree=fixture.allocation,
        cleanup_worktree=False,
    )


def test_exact_view_is_persisted_before_provider_and_returned_once(protected, monkeypatch):
    executor = _executor(protected, monkeypatch, _writer)

    def provider(command, cwd, **kwargs):
        with tx() as connection:
            rows = connection.execute(
                "SELECT content FROM task_artifacts WHERE task_id=? AND artifact_type=?",
                (protected.review_task_id, candidate.ARTIFACT_KIND),
            ).fetchall()
        assert len(rows) == 1
        view = json.loads(rows[0]["content"])["content"]["rendered_view"]
        assert view in kwargs["task_contract"]
        assert "protected-reader-fixture" in view
        return CommandRunCapture(
            command="fixture provider port",
            cwd=str(cwd),
            stdout=json.dumps({"type": "result", "result": "APPROVE: exact source reviewed"}),
            stderr="",
            exit_code=0,
        ), None

    monkeypatch.setattr(executor, "_run_frontier_command", provider)
    result = _run_review(protected, executor)
    assert result.status == "completed", result
    artifacts = [a for a in result.artifacts if a.artifact_type == candidate.ARTIFACT_KIND]
    assert len(artifacts) == 1 and artifacts[0].persisted_artifact_id
    assert not any(a.artifact_type == "source_patch" for a in result.artifacts)


@pytest.mark.parametrize(
    "failure", ["write_error", "missing_ack", "missing_receipt", "wrong_review_task"]
)
def test_host_evidence_failure_never_launches_reviewer(protected, monkeypatch, failure):
    def writer(command):
        if failure == "write_error":
            raise OSError("fixture disk refusal")
        return parse_coordination_result(command, {"ok": True})

    if failure == "missing_receipt":
        protected = replace(
            protected,
            producer=replace(protected.producer, artifacts=protected.producer.artifacts[:-1]),
        )
    if failure == "wrong_review_task":
        protected = replace(
            protected,
            context=replace(
                protected.context,
                task_ids_by_name={
                    **protected.context.task_ids_by_name,
                    "staff_review": protected.context.task_ids_by_name[
                        protected.producer.task_name
                    ],
                },
            ),
        )
    executor = _executor(protected, monkeypatch, writer)
    monkeypatch.setattr(
        executor,
        "_run_frontier_command",
        lambda *a, **k: pytest.fail("reviewer launched without evidence"),
    )
    result = _run_review(protected, executor)
    assert result.status == "failed"
    assert result.failure is not None
    assert result.failure.error_code == "REVIEW_UNAVAILABLE"
    expected = "verification_unavailable" if failure == "missing_receipt" else "persistence_failed"
    assert result.failure.operation == f"candidate_review.{expected}"
    assert not any(a.artifact_type == "review_result" for a in result.artifacts)


def test_legacy_fallback_is_a_closed_source_producer(committed):
    run = committed.producer.artifacts[0]
    fallback = replace(
        run,
        artifact_type="frontier_fallback_run",
        schema_version="frontier_fallback_run.v2",
        content={
            **run.content,
            "fallback_execution_lease": None,
            "worktree": committed.allocation.to_payload(),
        },
    )
    producer = replace(committed.producer, artifacts=(fallback, committed.producer.artifacts[1]))
    result = committed.prepare(producers=(producer,))
    assert isinstance(result, candidate.CandidateReviewReady), result
    assert isinstance(result.view.verification, candidate.StandaloneVerification)
    ambiguous = committed.prepare(
        producers=(replace(producer, artifacts=(*producer.artifacts, run)),)
    )
    assert isinstance(ambiguous, candidate.CandidateReviewUnavailable)


def test_recovery_anchor_without_protected_execution_refuses(protected):
    checkpoint = protected.producer.artifacts[1]
    anchor = PowWowArtifact(
        artifact_type="recovery_review_anchor",
        schema_version="recovery_review_anchor.v1",
        task_name=protected.producer.task_name,
        content={"verification": [{"exit_code": 0}]},
    )
    result = protected.prepare(
        producers=(replace(protected.producer, artifacts=(anchor, checkpoint)),)
    )
    assert isinstance(result, candidate.CandidateReviewUnavailable)
    assert "recovery anchors" in result.reason


def test_oversized_source_refuses_complete_candidate(committed):
    repository = Path(committed.allocation.worktree_path)
    (repository / "label_index.py").write_text("z" * 2_000_001)
    _git(repository, "add", "label_index.py")
    _git(repository, "commit", "-qm", "oversized candidate")
    checkpoint = committed.producer.artifacts[1]
    checkpoint = replace(
        checkpoint,
        content={**checkpoint.content, "commit_sha": _git(repository, "rev-parse", "HEAD")},
    )
    run = committed.producer.artifacts[0]
    run = replace(
        run,
        content={
            **run.content,
            "commit_checkpoint": {
                **run.content["commit_checkpoint"],
                "commit_sha": checkpoint.content["commit_sha"],
            },
        },
    )
    result = committed.prepare(
        producers=(replace(committed.producer, artifacts=(run, checkpoint)),)
    )
    assert isinstance(result, candidate.CandidateReviewUnavailable)
    assert "complete-output bound" in result.reason
