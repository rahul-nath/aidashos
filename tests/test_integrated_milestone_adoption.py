# SPDX-FileCopyrightText: 2026 Rahul Nath <https://github.com/rahul-nath>
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from work_unit_support import compile_acceptance_doc

from local_first_agent_os.project_access import AccessMode, ProjectAccessPolicy
from local_first_agent_os.project_center import LinkedProject
from local_first_agent_os.work_units import dispatch_adoption
from local_first_agent_os.work_units import repository as repo
from local_first_agent_os.work_units.events import MilestoneTransition, WorkUnitTransition
from local_first_agent_os.work_units.lifecycle import (
    FailureClass,
    LifecyclePhase,
    MilestoneExecutionStatus,
    WorkUnitStatus,
)


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _blocked_implementation(*, failure_code: str = "USAGE_LIMIT") -> tuple[str, str]:
    compiled = compile_acceptance_doc(design_doc_id="integrated_adoption")
    assert compiled.compiled_plan_revision_id is not None
    started = repo.start_work_unit(compiled.compiled_plan_revision_id)
    work_unit_id = started.work_unit.work_unit_id
    milestone = next(
        item
        for item in repo.list_milestone_executions(work_unit_id)
        if item.phase is LifecyclePhase.IMPLEMENT
    )
    repo.record_fact(
        work_unit_id,
        WorkUnitTransition(status=WorkUnitStatus.RUNNING, current_phase=LifecyclePhase.IMPLEMENT),
    )
    for status in (
        MilestoneExecutionStatus.READY,
        MilestoneExecutionStatus.RUNNING,
        MilestoneExecutionStatus.BLOCKED,
    ):
        blocked = status is MilestoneExecutionStatus.BLOCKED
        repo.record_fact(
            work_unit_id,
            MilestoneTransition(
                phase=LifecyclePhase.IMPLEMENT,
                milestone_key=milestone.stable_key,
                status=status,
                attempt=1,
                dispatch_intent_id="intent-quota",
                failure_class=FailureClass.CORRECTABLE if blocked else None,
                failure_code=failure_code if blocked else None,
            ),
        )
    repo.record_fact(
        work_unit_id,
        WorkUnitTransition(status=WorkUnitStatus.BLOCKED, current_phase=LifecyclePhase.IMPLEMENT),
    )
    return work_unit_id, milestone.stable_key


def test_operator_can_adopt_an_integrated_ancestor_after_provider_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    _git(target, "init", "-q", "-b", "main")
    _git(target, "config", "user.email", "operator@example.com")
    _git(target, "config", "user.name", "Operator")
    (target / "feature.py").write_text("ENFORCED = True\n", encoding="utf-8")
    _git(target, "add", "feature.py")
    _git(target, "commit", "-qm", "implement existing acceptance")
    commit_sha = _git(target, "rev-parse", "HEAD")
    project = LinkedProject(
        id="local_first_agent_os",
        kind="code",
        path=target,
        status="active",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="test target",
    )
    monkeypatch.setattr(
        dispatch_adoption,
        "load_project_center",
        lambda _settings: SimpleNamespace(project_by_id=lambda _project_id: project),
    )
    work_unit_id, milestone_key = _blocked_implementation()

    first = dispatch_adoption.adopt_integrated_milestone(
        work_unit_id,
        milestone_key,
        commit_sha[:10],
        accepted_by="operator",
        acceptance_evidence="The named commit implements and tests the milestone contract.",
    )
    replay = dispatch_adoption.adopt_integrated_milestone(
        work_unit_id,
        milestone_key,
        commit_sha,
        accepted_by="operator",
        acceptance_evidence="The named commit implements and tests the milestone contract.",
    )

    milestone = next(
        item
        for item in repo.list_milestone_executions(work_unit_id)
        if item.stable_key == milestone_key
    )
    source_patch = next(
        artifact
        for artifact in repo.list_work_unit_artifacts(work_unit_id)
        if artifact.artifact_type.value == "source_patch"
    )
    assert milestone.status is MilestoneExecutionStatus.SUCCEEDED
    assert milestone.attempt == 2
    assert first.applied is True
    assert replay.applied is False
    assert source_patch.metadata["adoption_kind"] == "integrated_ancestor.v1"
    assert source_patch.metadata["integrated_commit_sha"] == commit_sha
    assert source_patch.metadata["source_dispatch_intent_id"] == "intent-quota"
    assert source_patch.metadata["changed_files"] == ["feature.py"]


def test_integrated_adoption_refuses_a_non_provider_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    _git(target, "init", "-q", "-b", "main")
    _git(target, "config", "user.email", "operator@example.com")
    _git(target, "config", "user.name", "Operator")
    (target / "feature.py").write_text("ENFORCED = True\n", encoding="utf-8")
    _git(target, "add", "feature.py")
    _git(target, "commit", "-qm", "implement existing acceptance")
    commit_sha = _git(target, "rev-parse", "HEAD")
    project = LinkedProject(
        id="local_first_agent_os",
        kind="code",
        path=target,
        status="active",
        access=ProjectAccessPolicy(mode=AccessMode.READ_WRITE),
        description="test target",
    )
    monkeypatch.setattr(
        dispatch_adoption,
        "load_project_center",
        lambda _settings: SimpleNamespace(project_by_id=lambda _project_id: project),
    )
    work_unit_id, milestone_key = _blocked_implementation(failure_code="VERIFICATION_FAILED")

    with pytest.raises(
        dispatch_adoption.DispatchAdoptionRefused,
        match="only a milestone blocked by USAGE_LIMIT",
    ):
        dispatch_adoption.adopt_integrated_milestone(
            work_unit_id,
            milestone_key,
            commit_sha,
            accepted_by="operator",
            acceptance_evidence="The named commit implements the milestone.",
        )
