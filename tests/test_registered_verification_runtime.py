# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import NoReturn

import pytest
from work_unit_support import ACCEPTANCE_DESIGN_DOC, compile_acceptance_doc

from local_first_agent_os import interrupted_recovery, pairing_assignment, pairing_resolution
from local_first_agent_os.capabilities import Capability
from local_first_agent_os.coordination import dispatch
from local_first_agent_os.coordination.store import tx
from local_first_agent_os.execution_admission import (
    AuthorizedExecution,
    ExecutionAdmissionFailure,
    ExecutionContract,
    admit_execution,
)
from local_first_agent_os.project_center import load_project_center
from local_first_agent_os.spawn_authority import SpawnAuthority
from local_first_agent_os.work_units import execution, verification_sources
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.compiler import CompiledPlanOutcome, compile_design_doc
from local_first_agent_os.work_units.design_doc import ParsedPermissionEnvelope, parse_design_doc
from local_first_agent_os.work_units.execution import (
    DispatchBackedExecutorRuntime,
    MilestoneAwaitingDispatch,
    MilestoneContext,
    MilestoneFailed,
    RegisteredVerificationRuntime,
    dispatch_backed_runtime,
    resolve_dependency_base_commit,
)
from local_first_agent_os.work_units.executors import (
    EXECUTOR_REGISTRY,
    ExecutorKind,
    execution_driver_for,
)
from local_first_agent_os.work_units.lifecycle import FailureClass
from local_first_agent_os.work_units.permissions import PermissionAction
from local_first_agent_os.work_units.plan import ToolPolicy
from local_first_agent_os.work_units.verification_sources import (
    VerificationSourceFailure,
    VerificationSourceRefused,
)


def _context(*, content: str = ACCEPTANCE_DESIGN_DOC) -> MilestoneContext:
    compiled = compile_acceptance_doc(content=content)
    assert compiled.compiled_plan_revision_id is not None
    unit = repo.start_work_unit(compiled.compiled_plan_revision_id).work_unit
    plan = repo.get_compiled_plan_revision(unit.compiled_plan_revision_id).plan
    milestone = plan.milestone("d")
    return MilestoneContext(
        work_unit_id=unit.work_unit_id,
        root_workflow_id=unit.root_workflow_id,
        child_workflow_id=f"{unit.root_workflow_id}:milestone:d:1",
        milestone=milestone,
        attempt=1,
        design_doc_revision_id=unit.design_doc_revision_id,
        compiled_plan_hash=unit.compiled_plan_hash,
        target_project_id=plan.target_project_id,
    )


def _forbidden(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("model planning or interrupted code recovery was reached")


def test_registered_verify_does_not_route_to_the_model_runtime() -> None:
    runtime = dispatch_backed_runtime()
    assert (
        runtime.routes[ExecutorKind.VERIFY_TESTS]
        is not runtime.routes[ExecutorKind.IMPLEMENT_CODE_CHANGE]
    )
    assert isinstance(runtime.routes[ExecutorKind.VERIFY_TESTS], RegisteredVerificationRuntime)


def test_registered_start_skips_pairing_and_interrupted_code_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    submitted: list[dict[str, object]] = []

    def submit(*args: object, **kwargs: object) -> dict[str, object]:
        submitted.append(kwargs)
        return {"ok": True, "intent_id": "registered-gate"}

    def record(*args: object) -> None:
        return None

    monkeypatch.setattr(dispatch, "submit_dispatch_intent", submit)
    monkeypatch.setattr(pairing_assignment, "assignment_for_idempotency_key", _forbidden)
    monkeypatch.setattr(pairing_resolution, "resolve_assignment", _forbidden)
    monkeypatch.setattr(interrupted_recovery, "inspect_interrupted_attempt", _forbidden)
    monkeypatch.setattr(execution, "resolve_dependency_base_commit", lambda context: "a" * 40)
    result = RegisteredVerificationRuntime(fact_recorder=record).start(context)
    assert isinstance(result, MilestoneAwaitingDispatch)
    assert result.dispatch_intent_id == "registered-gate"
    assert len(submitted) == 1
    assert submitted[0]["pairing_assignment"] is None
    assert submitted[0]["interrupted_recovery"] is None
    assert submitted[0]["base_commit_sha"] == "a" * 40
    assert submitted[0]["permitted_capabilities"] == ("read_repository", "run_command")


@pytest.mark.parametrize(
    "capabilities",
    [
        (Capability.READ_REPOSITORY,),
        (Capability.RUN_COMMAND,),
        (Capability.READ_REPOSITORY, Capability.RUN_COMMAND, Capability.INVOKE_MODEL),
        (Capability.READ_REPOSITORY, Capability.RUN_COMMAND, Capability.WRITE_REPOSITORY),
    ],
)
def test_incompatible_old_plan_parks_before_any_effect(
    monkeypatch: pytest.MonkeyPatch, capabilities: tuple[Capability, ...]
) -> None:
    context = _context()
    narrowed = replace(
        context,
        milestone=replace(
            context.milestone,
            tool_policy=ToolPolicy(permitted_tools=tuple(cap.value for cap in capabilities)),
        ),
    )
    monkeypatch.setattr(dispatch, "submit_dispatch_intent", _forbidden)
    result = RegisteredVerificationRuntime().start(narrowed)
    assert isinstance(result, MilestoneFailed)
    assert result.failure_class is FailureClass.SCHEDULING
    assert result.failure_code == ExecutionAdmissionFailure.EXECUTOR_RUNTIME_INCOMPATIBLE.value
    assert narrowed.permitted_tools == tuple(cap.value for cap in capabilities)


def test_wrong_runtime_cannot_promote_verification_to_model_authority() -> None:
    result = DispatchBackedExecutorRuntime().start(_context())
    assert isinstance(result, MilestoneFailed)
    assert result.failure_class is FailureClass.SCHEDULING
    assert result.failure_code == ExecutionAdmissionFailure.EXECUTOR_RUNTIME_INCOMPATIBLE.value


def test_compiler_exposes_missing_read_authority_before_runtime() -> None:
    parsed = parse_design_doc(ACCEPTANCE_DESIGN_DOC, design_doc_id="read-missing")
    parsed = replace(
        parsed,
        permission_envelope=ParsedPermissionEnvelope(
            autonomous=(
                PermissionAction.TEST_COMMAND_EXECUTION,
                PermissionAction.CODE_WORKTREE_WRITE,
                PermissionAction.RUN_LOCAL_MODEL_DELEGATES,
                PermissionAction.WRITE_LEDGER_ARTIFACTS,
            ),
            requested=(),
            denied_without_approval=(PermissionAction.READ_REPO_CONTEXT,),
        ),
    )
    outcome = compile_design_doc(parsed, design_doc_revision_id="read-missing")
    assert isinstance(outcome, CompiledPlanOutcome)
    assert not outcome.runnable
    assert any(
        "verify.tests" in blocker and "read_repository" in blocker
        for blocker in outcome.execution_blockers
    )


def test_future_executor_declarations_satisfy_the_shared_driver_contract() -> None:
    for kind, declaration in EXECUTOR_REGISTRY.items():
        admission = admit_execution(
            ExecutionContract(execution_driver_for(kind)),
            SpawnAuthority.of(declaration.permitted_tools),
        )
        assert isinstance(admission, AuthorizedExecution), kind


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.strip()


def _history(tmp_path: Path) -> tuple[Path, str, str, str]:
    repository = tmp_path / "target"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "test")
    commits: list[str] = []
    for name in ("base", "first", "latest"):
        (repository / name).write_text(name)
        _git(repository, "add", name)
        _git(repository, "commit", "-m", name)
        commits.append(_git(repository, "rev-parse", "HEAD"))
    return repository, commits[0], commits[1], commits[2]


def _retain_completed_history(context: MilestoneContext, first: str, latest: str) -> None:
    """Isolated retained-history fixture, not an execution or approval simulator."""
    with tx() as connection:
        for row in repo.list_milestone_executions(context.work_unit_id):
            if row.stable_key in {"a", "b", "c"}:
                connection.execute(
                    "UPDATE milestone_executions SET status='SUCCEEDED' "
                    "WHERE milestone_execution_id=?",
                    (row.milestone_execution_id,),
                )
            if row.stable_key not in {"b", "c"}:
                continue
            commit = first if row.stable_key == "b" else latest
            key = "integrated_commit_sha" if row.stable_key == "b" else "integration_commit_sha"
            connection.execute(
                "INSERT INTO work_unit_artifacts(artifact_id, work_unit_id, "
                "milestone_execution_id, artifact_type, uri, content_hash, "
                "producer_workflow_id, metadata_json, created_at) "
                "VALUES (?, ?, ?, 'source_patch', 'fixture', ?, ?, ?, 1.0)",
                (
                    f"source-{row.stable_key}",
                    context.work_unit_id,
                    row.milestone_execution_id,
                    commit,
                    context.root_workflow_id,
                    json.dumps({key: commit}),
                ),
            )


def _register_source(
    monkeypatch: pytest.MonkeyPatch, context: MilestoneContext, repository: Path
) -> None:
    center = load_project_center()
    project = replace(center.project_by_id(context.target_project_id), path=repository)
    monkeypatch.setattr(
        verification_sources, "load_project_center", lambda: replace(center, projects=(project,))
    )


def test_source_is_recorded_integration_covering_transitive_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # D only directly depends on C. B's source must still be included.
    content = ACCEPTANCE_DESIGN_DOC.replace(
        "Depends on: A\nAcceptance: the writer", "Depends on: B\nAcceptance: the writer"
    ).replace("Depends on: B, C", "Depends on: C")
    context = _context(content=content)
    repository, base, first, latest = _history(tmp_path)
    _retain_completed_history(context, first, latest)
    _register_source(monkeypatch, context, repository)
    # A stale report checkpoint must not override the integration artifact.
    with tx() as connection:
        connection.execute(
            "INSERT INTO dispatch_intents(intent_id,tier,kind,prompt,source,status,"
            "created_at,completed_at,result) VALUES ('stale','senior','code','p',?,'DONE',1,2,?)",
            (
                f"work_unit:{context.work_unit_id}:milestone_execution:c",
                json.dumps(
                    {
                        "run_result": {
                            "artifacts": [
                                {
                                    "artifact_type": "worktree_commit_checkpoint",
                                    "content": {"commit_sha": base},
                                }
                            ]
                        }
                    }
                ),
            ),
        )
    _git(repository, "switch", "--detach", base)
    assert resolve_dependency_base_commit(context) == latest


def test_divergent_integrated_dependencies_refuse_even_if_head_contains_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context()
    repository, base, first, latest = _history(tmp_path)
    _git(repository, "switch", "-c", "other", base)
    (repository / "other").write_text("other")
    _git(repository, "add", "other")
    _git(repository, "commit", "-m", "other")
    other = _git(repository, "rev-parse", "HEAD")
    _git(repository, "merge", latest, "--no-edit")
    _retain_completed_history(context, first, other)
    _register_source(monkeypatch, context, repository)
    with pytest.raises(VerificationSourceRefused) as refused:
        resolve_dependency_base_commit(context)
    assert refused.value.code is VerificationSourceFailure.DEPENDENCY_BASES_DIVERGED


def test_verification_never_falls_back_to_unrecorded_head() -> None:
    result = RegisteredVerificationRuntime().start(_context())
    assert isinstance(result, MilestoneFailed)
    assert result.failure_code == VerificationSourceFailure.DEPENDENCY_INCOMPLETE.value


@pytest.mark.parametrize("rewrite", ["replacement_ref", "legacy_graft"])
def test_git_history_rewrites_cannot_invent_integrated_dependency_ancestry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rewrite: str
) -> None:
    context = _context()
    repository, base, first, _ = _history(tmp_path)
    _git(repository, "switch", "--detach", base)
    (repository / "other").write_text("does not include first")
    _git(repository, "add", "other")
    _git(repository, "commit", "-m", "divergent source")
    other = _git(repository, "rev-parse", "HEAD")
    if rewrite == "replacement_ref":
        _git(repository, "replace", "--graft", other, first)
    else:
        (repository / ".git" / "info" / "grafts").write_text(f"{other} {first}\n")
    _retain_completed_history(context, first, other)
    _register_source(monkeypatch, context, repository)
    with pytest.raises(VerificationSourceRefused) as refused:
        resolve_dependency_base_commit(context)
    assert refused.value.code is VerificationSourceFailure.DEPENDENCY_BASES_DIVERGED


def test_caller_git_environment_cannot_redirect_dependency_ancestry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context()
    repository, _, first, latest = _history(tmp_path)
    _retain_completed_history(context, first, latest)
    _register_source(monkeypatch, context, repository)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing.git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "foreign"))
    assert resolve_dependency_base_commit(context) == latest


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({}, VerificationSourceFailure.INTEGRATION_EVIDENCE_INVALID),
        (
            {"integration_commit_sha": "HEAD"},
            VerificationSourceFailure.INTEGRATION_EVIDENCE_INVALID,
        ),
        (
            {"integration_commit_sha": "a" * 40, "integrated_commit_sha": "b" * 40},
            VerificationSourceFailure.INTEGRATION_EVIDENCE_INVALID,
        ),
        ({"integration_commit_sha": "a" * 40}, VerificationSourceFailure.COMMIT_UNAVAILABLE),
    ],
)
def test_invalid_retained_source_cannot_fall_back_to_checkpoint_or_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata: dict[str, str],
    expected: VerificationSourceFailure,
) -> None:
    context = _context()
    repository, _, first, latest = _history(tmp_path)
    _retain_completed_history(context, first, latest)
    _register_source(monkeypatch, context, repository)
    with tx() as connection:
        connection.execute(
            "UPDATE work_unit_artifacts SET metadata_json=? WHERE artifact_id='source-c'",
            (json.dumps(metadata),),
        )
    with pytest.raises(VerificationSourceRefused) as refused:
        resolve_dependency_base_commit(context)
    assert refused.value.code is expected


def test_missing_source_artifact_blocks_even_when_dependencies_are_succeeded() -> None:
    context = _context()
    _retain_completed_history(context, "a" * 40, "b" * 40)
    with tx() as connection:
        connection.execute("DELETE FROM work_unit_artifacts WHERE artifact_id='source-c'")
    result = RegisteredVerificationRuntime().start(context)
    assert isinstance(result, MilestoneFailed)
    assert result.failure_code == VerificationSourceFailure.INTEGRATION_EVIDENCE_MISSING.value
